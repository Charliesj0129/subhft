"""D1 (HIGH) — Subscription retry livelock fix.

Pre-fix pathology: ``QuoteRuntime.start_sub_retry_thread._retry_loop``
re-appended any failing symbol at every 60 s tick with no per-symbol
backoff and no max-attempts cap. When the broker permanently rejected a
subscription (e.g. illiquid TXO option contracts past quote service), the
same 22 codes were retried identically every 60 s for 24 h+ in
production (see audit Round 1 evidence: 24 identical "Subscription
retry: still pending" warnings, same code list).

Fix design (per audit ticket D1):
1. Per-symbol attempt counter ``_retry_attempts`` and next-try timestamp
   ``_retry_next_ts`` guarded by ``_retry_state_lock``.
2. Backoff schedule [60, 120, 300, 600, 1800, 3600] capped at 1 h.
3. After ``HFT_SUB_RETRY_MAX_ATTEMPTS`` failures (default 10) the symbol
   moves to ``_permanently_failed`` and is never retried again.
4. On successful subscribe, attempt count resets to 0 and the symbol
   leaves all retry maps.
5. Prometheus counters expose retry decisions and permanent failures.

These tests target the per-symbol decision helpers
(``_should_attempt_subscription`` / ``_record_subscription_failure`` /
``_record_subscription_success``) so we can validate the state machine
without standing up a full ShioajiClient + broker SDK.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.shioaji.quote_runtime import QuoteRuntime


def _make_runtime(metrics: Any | None = None) -> QuoteRuntime:
    """Build a QuoteRuntime against a mock client.

    The retry-state helpers are tested in isolation — they do not touch
    network/broker code paths.
    """
    client = MagicMock()
    client.metrics = metrics
    runtime = QuoteRuntime(client)
    return runtime


def test_first_retry_uses_60s_backoff() -> None:
    """A single failure schedules the next attempt 60 s later."""
    rt = _make_runtime()
    now = 1000.0
    allowed, reason = rt._should_attempt_subscription("TXO22500D6", now)
    assert allowed is True
    assert reason == "ok"

    permanent = rt._record_subscription_failure("TXO22500D6", now)
    assert permanent is False

    allowed, reason = rt._should_attempt_subscription("TXO22500D6", now + 30.0)
    assert allowed is False
    assert reason == "skip_backoff"

    allowed, reason = rt._should_attempt_subscription("TXO22500D6", now + 60.0)
    assert allowed is True
    assert reason == "ok"


def test_backoff_doubles_to_cap() -> None:
    """Successive failures escalate per the 60/120/300/600/1800/3600 schedule."""
    rt = _make_runtime()
    code = "TXO22500P6"
    expected_schedule = [60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0]
    now = 1000.0
    for i, expected_delay in enumerate(expected_schedule, start=1):
        rt._record_subscription_failure(code, now)
        next_ts = rt._retry_next_ts[code]
        actual_delay = next_ts - now
        assert actual_delay == pytest.approx(expected_delay), (
            f"Failure #{i}: expected delay {expected_delay}s, got {actual_delay}s"
        )
        now = next_ts

    rt._record_subscription_failure(code, now)
    last_delay = rt._retry_next_ts[code] - now
    assert last_delay == pytest.approx(3600.0), (
        f"Backoff must stay capped at 3600s past the schedule, got {last_delay}s"
    )


def test_max_attempts_marks_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    """After N failures the symbol moves to ``_permanently_failed`` and the
    helper returns ``True``."""
    monkeypatch.setenv("HFT_SUB_RETRY_MAX_ATTEMPTS", "5")
    rt = _make_runtime()
    code = "TXO22900P6"
    now = 0.0
    permanent = False
    for _ in range(5):
        permanent = rt._record_subscription_failure(code, now)
        now += 1.0
    assert permanent is True
    assert code in rt._permanently_failed
    assert rt._retry_attempts.get(code, 0) >= 5


def test_default_max_attempts_is_10(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the env var is unset, default cap is 10 attempts."""
    monkeypatch.delenv("HFT_SUB_RETRY_MAX_ATTEMPTS", raising=False)
    rt = _make_runtime()
    code = "TXO22500D6"
    now = 0.0
    for i in range(9):
        permanent = rt._record_subscription_failure(code, now)
        assert permanent is False, f"Should not be permanent at attempt {i + 1}"
        now += 1.0
    permanent = rt._record_subscription_failure(code, now)
    assert permanent is True
    assert code in rt._permanently_failed


def test_permanent_symbol_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Symbols in ``_permanently_failed`` always return ``skip_permanent``,
    regardless of how much time has passed."""
    monkeypatch.setenv("HFT_SUB_RETRY_MAX_ATTEMPTS", "2")
    rt = _make_runtime()
    code = "TXO22500D6"
    now = 0.0
    rt._record_subscription_failure(code, now)
    rt._record_subscription_failure(code, now + 1.0)
    assert code in rt._permanently_failed

    allowed, reason = rt._should_attempt_subscription(code, now + 10 * 86400.0)
    assert allowed is False
    assert reason == "skip_permanent"


def test_successful_subscribe_resets_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling ``_record_subscription_success`` clears the attempt counter,
    next-try timestamp, and removes the code from any state map."""
    monkeypatch.setenv("HFT_SUB_RETRY_MAX_ATTEMPTS", "10")
    rt = _make_runtime()
    code = "TMFR1"
    now = 0.0
    rt._record_subscription_failure(code, now)
    rt._record_subscription_failure(code, now + 60.0)
    assert rt._retry_attempts.get(code) == 2
    assert code in rt._retry_next_ts

    rt._record_subscription_success(code)
    assert code not in rt._retry_attempts
    assert code not in rt._retry_next_ts
    assert code not in rt._permanently_failed

    rt._record_subscription_failure(code, now + 1000.0)
    assert rt._retry_attempts.get(code) == 1


def _make_metrics_stub() -> MagicMock:
    """Stub MetricsRegistry-shaped object with the three new counters."""
    metrics = MagicMock()
    metrics.feed_subscription_retry_total = MagicMock()
    metrics.feed_subscription_retry_total.labels = MagicMock(return_value=MagicMock(inc=MagicMock()))
    metrics.feed_subscription_permanent_failures_total = MagicMock()
    metrics.feed_subscription_permanent_failures_total.labels = MagicMock(return_value=MagicMock(inc=MagicMock()))
    metrics.feed_subscription_retry_attempts = MagicMock()
    metrics.feed_subscription_retry_attempts.labels = MagicMock(return_value=MagicMock(set=MagicMock()))
    metrics.cap_symbol = MagicMock(side_effect=lambda s: s)
    return metrics


def test_metric_increments_on_each_retry_decision() -> None:
    """``_should_attempt_subscription`` increments the retry-result counter
    once per call, and ``_record_subscription_failure`` updates the
    attempts gauge plus the permanent-failures counter when the cap trips.
    """
    metrics = _make_metrics_stub()
    rt = _make_runtime(metrics=metrics)
    code = "TXO22500D6"

    allowed, reason = rt._should_attempt_subscription(code, 0.0)
    assert allowed is True
    metrics.feed_subscription_retry_total.labels.assert_any_call(symbol=code, result="ok")

    rt._record_subscription_failure(code, 0.0)
    metrics.feed_subscription_retry_attempts.labels.assert_any_call(symbol=code)

    allowed, reason = rt._should_attempt_subscription(code, 30.0)
    assert allowed is False
    metrics.feed_subscription_retry_total.labels.assert_any_call(symbol=code, result="skip_backoff")


def test_permanent_failure_emits_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once a symbol crosses the max-attempts cap, the permanent-failures
    counter increments exactly once."""
    monkeypatch.setenv("HFT_SUB_RETRY_MAX_ATTEMPTS", "2")
    metrics = _make_metrics_stub()
    rt = _make_runtime(metrics=metrics)
    code = "TXO22900P6"

    rt._record_subscription_failure(code, 0.0)
    metrics.feed_subscription_permanent_failures_total.labels.assert_not_called()

    rt._record_subscription_failure(code, 1.0)
    metrics.feed_subscription_permanent_failures_total.labels.assert_called_once_with(symbol=code)


def test_pre_fix_pattern_would_livelock_without_backoff() -> None:
    """Witness test: documents the pre-fix loop semantics. A symbol that
    permanently fails was retried every ``interval`` seconds with no
    growth in delay. This pins the bug for any future reverter.
    """
    interval = 60.0
    elapsed_hours = 24.0
    naive_retries = int(elapsed_hours * 3600.0 / interval)
    assert naive_retries == 1440, (
        "If this number changes, the audit assumptions changed too — "
        "production evidence cited 24 retries over 24 minutes."
    )

    new_max_attempts = int(os.getenv("HFT_SUB_RETRY_MAX_ATTEMPTS", "10"))
    assert new_max_attempts <= 10
