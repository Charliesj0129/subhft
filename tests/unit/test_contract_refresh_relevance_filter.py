"""B+C regression tests for the contract-refresh resubscribe storm fix.

Failure mode this guards against:

On 2026-05-12 the live engine (HFT_CONTRACT_REFRESH_S=3600 +
HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY=diff) emitted 102 Solace
"Max Num Subscriptions Exceeded" rejections at 01:05 UTC when a
broker-side TXO weekly chain expiry cleanup removed 680 catalog
codes. Most removed codes weren't in our 478-symbol subscribe set,
but the diff-gate fired ``_resubscribe_all`` on the entire universe
anyway, blowing past the ~250 per-session topic budget because the
broker hadn't freed slots from the now-expired prior subscriptions.

Two fixes are tested here:

- **B** (relevance filter): ``contract_refresh_diff`` must record
  the intersection of ``(added | removed)`` with
  ``c.subscribed_codes`` *before* the per-list ``[:200]`` log
  truncation, and the resubscribe gate must read that field
  (``relevant_count``). Catalog rotations of codes we never
  subscribe to are now no-ops.

- **C** (settle delay): ``_resubscribe_all`` must sleep between
  the unsubscribe sweep and the subscribe sweep (default 200 ms,
  configurable via ``HFT_RESUBSCRIBE_UNSUBSCRIBE_SETTLE_S``) so
  Solace can free per-session topic slots before the resubscribe
  burst hits the cap.
"""

from __future__ import annotations

from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter import shioaji_client as mod


def _bare_client() -> mod.ShioajiClient:
    """Build a ShioajiClient bypassing __init__ (mirrors test_contract_refresh.py)."""
    client = object.__new__(mod.ShioajiClient)
    client.api = MagicMock()
    client.metrics = MagicMock()
    client.metrics.stormguard_mode = MagicMock()
    client.metrics.feed_resubscribe_skipped_concurrent_total = MagicMock()
    client.metrics.feed_resubscribe_total = MagicMock()
    client.metrics.feed_subscription_truncate_total = MagicMock()
    client.allow_synthetic_contracts = False
    client.MAX_SUBSCRIPTIONS = 200
    client.subscribed_codes = set()
    client.subscribed_count = 0
    client._failed_sub_symbols = deque()
    client._sub_retry_running = False
    client._sub_retry_thread = None
    client._contract_retry_s = 60.0
    client._contract_refresh_s = 86400.0
    client._contract_cache_path = "config/contracts.json"
    client._contract_refresh_running = False
    client._contract_refresh_thread = None
    client.logged_in = True
    client._callbacks_registered = True
    client._event_callback_registered = True
    client.tick_callback = MagicMock()
    client.symbols = []
    client.resubscribe_cooldown = 0.0
    client._last_resubscribe_ts = 0.0
    import threading as _t

    client._resubscribe_lock = _t.Lock()
    client._refresh_quote_routes = MagicMock()
    client._ensure_callbacks = MagicMock()
    client._start_sub_retry_thread = MagicMock()
    client._quote_api = MagicMock(return_value=MagicMock(subscribe=MagicMock(), unsubscribe=MagicMock()))
    return client


# ---------------------------------------------------------------------------
# B: relevance filter in the contract-refresh diff
# ---------------------------------------------------------------------------


class TestRelevanceFilterInDiff:
    """Diff builder must record ``relevant_count`` (intersection of
    ``added | removed`` with currently-subscribed codes), computed before
    the per-list ``[:200]`` truncation."""

    def test_diff_records_zero_relevant_when_universe_disjoint(self) -> None:
        from hft_platform.feed_adapter.shioaji.contracts_runtime import _compute_diff_payload

        before = {f"TXO{i:04d}E6" for i in range(100, 300)}
        after = {f"TXO{i:04d}F6" for i in range(100, 300)}
        subscribed = {"TMFE6", "TXFE6", "2330"}

        diff = _compute_diff_payload(
            version=1,
            codes_before=before,
            codes_after=after,
            subscribed=subscribed,
        )

        assert diff["relevant_count"] == 0, (
            f"Catalog rotation of unrelated codes must not look relevant. got diff={diff!r}"
        )

    def test_diff_records_relevant_overlap_for_subscribed_removed_code(self) -> None:
        from hft_platform.feed_adapter.shioaji.contracts_runtime import _compute_diff_payload

        before = {"TMFE6", "TXFE6"}
        after = {"TMFF6", "TXFF6"}
        subscribed = {"TMFE6"}

        diff = _compute_diff_payload(
            version=2,
            codes_before=before,
            codes_after=after,
            subscribed=subscribed,
        )

        assert diff["relevant_count"] == 1
        assert "TMFE6" in diff["relevant_codes"]

    def test_diff_relevance_pre_truncation_catches_large_removed_sets(self) -> None:
        """The ``added_codes`` / ``removed_codes`` log lists cap at 200, but
        the relevance check must scan the full ``before - after`` /
        ``after - before`` sets — otherwise a 680-removed cleanup whose
        relevant code sorts at position 250 looks irrelevant."""
        from hft_platform.feed_adapter.shioaji.contracts_runtime import _compute_diff_payload

        before = {f"TXO{i:04d}E6" for i in range(680)} | {"ZZZZ_RELEVANT"}
        after = set()
        subscribed = {"ZZZZ_RELEVANT", "TMFE6"}

        diff = _compute_diff_payload(
            version=3,
            codes_before=before,
            codes_after=after,
            subscribed=subscribed,
        )

        assert diff["relevant_count"] == 1, (
            f"Relevance scan must run on full add/remove sets, not the truncated log lists. got diff={diff!r}"
        )
        assert "ZZZZ_RELEVANT" in diff["relevant_codes"]
        assert len(diff["removed_codes"]) == 200


class TestResubscribeGateUsesRelevantCount:
    """The gate at the diff-policy branch must use ``relevant_count``,
    not the raw ``added_codes`` / ``removed_codes`` lists."""

    def test_diff_with_zero_relevant_does_not_resubscribe(self) -> None:
        from hft_platform.feed_adapter.shioaji.contracts_runtime import _diff_should_resubscribe

        diff = {
            "added_codes": ["TXO99999A1", "TXO99999A2"],
            "removed_codes": ["TXO00000Z9"],
            "relevant_count": 0,
            "relevant_codes": [],
        }
        assert _diff_should_resubscribe(diff) is False

    def test_diff_with_positive_relevant_triggers_resubscribe(self) -> None:
        from hft_platform.feed_adapter.shioaji.contracts_runtime import _diff_should_resubscribe

        diff = {
            "added_codes": ["TMFF6"],
            "removed_codes": [],
            "relevant_count": 1,
            "relevant_codes": ["TMFF6"],
        }
        assert _diff_should_resubscribe(diff) is True

    def test_legacy_diff_without_relevant_count_falls_back_to_old_gate(self) -> None:
        """Backwards compat: a diff missing ``relevant_count`` (e.g. produced
        by an older code path or unit-test fixture) falls back to the
        pre-fix behavior of treating any add/remove as relevant."""
        from hft_platform.feed_adapter.shioaji.contracts_runtime import _diff_should_resubscribe

        legacy = {"added_codes": ["TMFF6"], "removed_codes": []}
        assert _diff_should_resubscribe(legacy) is True

        empty_legacy = {"added_codes": [], "removed_codes": []}
        assert _diff_should_resubscribe(empty_legacy) is False


# ---------------------------------------------------------------------------
# C: settle delay in _resubscribe_all
# ---------------------------------------------------------------------------


class TestResubscribeSettleDelay:
    """Between the unsubscribe sweep and the subscribe sweep in
    ``_resubscribe_all`` we must sleep for ``HFT_RESUBSCRIBE_UNSUBSCRIBE_SETTLE_S``
    seconds so Solace can free per-session topic slots before the resubscribe
    burst overshoots the 250-topic cap."""

    def _setup_client_with_one_subscribed_symbol(self):
        client = _bare_client()
        client.symbols = [{"code": "TMFE6", "exchange": "TAIFEX", "product_type": "future"}]
        client.subscribed_codes = {"TMFE6"}
        client.subscribed_count = 1
        return client

    def test_sleep_called_between_unsub_and_sub_with_default_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_RESUBSCRIBE_UNSUBSCRIBE_SETTLE_S", raising=False)
        from hft_platform.feed_adapter.shioaji.subscription_manager import SubscriptionManager

        client = self._setup_client_with_one_subscribed_symbol()
        sub_mgr = SubscriptionManager(client)
        call_order: list[str] = []

        def _record_unsub(_self, sym):
            call_order.append(f"unsub:{sym.get('code')}")

        def _record_sub(_self, sym, cb):
            call_order.append(f"sub:{sym.get('code')}")
            return True

        def _record_sleep(seconds):
            call_order.append(f"sleep:{seconds}")

        with (
            patch.object(SubscriptionManager, "_unsubscribe_symbol", autospec=True, side_effect=_record_unsub),
            patch.object(SubscriptionManager, "_subscribe_symbol", autospec=True, side_effect=_record_sub),
            patch("hft_platform.feed_adapter.shioaji.subscription_manager.time.sleep", side_effect=_record_sleep),
        ):
            sub_mgr._resubscribe_all()

        assert call_order == [
            "unsub:TMFE6",
            "sleep:0.2",
            "sub:TMFE6",
        ], f"unexpected call order: {call_order!r}"

    def test_settle_delay_honors_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_RESUBSCRIBE_UNSUBSCRIBE_SETTLE_S", "0.05")
        from hft_platform.feed_adapter.shioaji.subscription_manager import SubscriptionManager

        client = self._setup_client_with_one_subscribed_symbol()
        sub_mgr = SubscriptionManager(client)
        sleeps: list[float] = []

        with (
            patch.object(SubscriptionManager, "_unsubscribe_symbol", autospec=True),
            patch.object(SubscriptionManager, "_subscribe_symbol", autospec=True, return_value=True),
            patch(
                "hft_platform.feed_adapter.shioaji.subscription_manager.time.sleep",
                side_effect=lambda s: sleeps.append(s),
            ),
        ):
            sub_mgr._resubscribe_all()

        assert sleeps == [0.05], f"expected exactly one settle sleep at 0.05, got {sleeps!r}"

    def test_zero_settle_disables_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Operators can disable the settle delay by setting the env var to 0."""
        monkeypatch.setenv("HFT_RESUBSCRIBE_UNSUBSCRIBE_SETTLE_S", "0")
        from hft_platform.feed_adapter.shioaji.subscription_manager import SubscriptionManager

        client = self._setup_client_with_one_subscribed_symbol()
        sub_mgr = SubscriptionManager(client)
        sleeps: list[float] = []

        with (
            patch.object(SubscriptionManager, "_unsubscribe_symbol", autospec=True),
            patch.object(SubscriptionManager, "_subscribe_symbol", autospec=True, return_value=True),
            patch(
                "hft_platform.feed_adapter.shioaji.subscription_manager.time.sleep",
                side_effect=lambda s: sleeps.append(s),
            ),
        ):
            sub_mgr._resubscribe_all()

        assert sleeps == [], f"settle_s=0 must skip sleep entirely, got {sleeps!r}"
