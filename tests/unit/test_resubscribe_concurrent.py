"""D2 (HIGH): ``_resubscribe_all`` racy reassignment + cooldown RMW.

The pre-fix bug:
``subscription_manager.py:_resubscribe_all`` is called from 4 different
threads (watchdog daemon, schedule_resubscribe daemon, Shioaji SDK event
thread on event_13/event_4, and ``MarketDataService._attempt_resubscribe``
via ``asyncio.to_thread``). The body had two unsafe patterns:

1. Cooldown check via ``last = c._last_resubscribe_ts; if now - last <
   cooldown: return; c._last_resubscribe_ts = now`` — unguarded read-
   modify-write. Two threads simultaneously past the cooldown both pass
   the gate, both run the unsubscribe + resubscribe loop, and both
   reassign ``subscribed_codes``.
2. ``c.subscribed_codes = set()`` (and ``c.subscribed_codes = set(new_map)``
   in ``contracts_runtime.refresh_symbols``, ``c.subscribed_codes = set()``
   in ``client.recreate_api`` and ``reconnect_orchestrator``) — atomic
   rebind orphans any peer-thread reader iterating the OLD set, mirroring
   the L2 ``_failed_sub_symbols`` pattern.

Fix:
- Add ``_resubscribe_lock: threading.Lock`` on ``ShioajiClient``.
- Wrap entire ``_resubscribe_all`` body in
  ``acquire(blocking=False)``; concurrent callers no-op and bump
  ``feed_resubscribe_skipped_concurrent_total``.
- Replace every ``c.subscribed_codes = <new_set>`` with in-place
  ``clear() + update(...)``. Identity is preserved across all reset
  paths so concurrent readers always see a consistent live object.

These tests assert:

1. Two threads calling ``_resubscribe_all`` concurrently → only one body
   executes, the other returns early after bumping the
   ``feed_resubscribe_skipped_concurrent_total`` metric.
2. ``c.subscribed_codes`` object identity is preserved across
   ``_resubscribe_all`` (and across ``recreate_api`` /
   ``reconnect_orchestrator``).
3. The cooldown read-modify-write of ``_last_resubscribe_ts`` is atomic
   under the new lock; second concurrent caller observes the updated
   timestamp.
4. The metric increments on the skip path.
"""

from __future__ import annotations

import threading
import unittest.mock as mock

import pytest

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_client() -> mock.MagicMock:
    """Build a mock client with the fields ``_resubscribe_all`` reads."""
    pytest.importorskip("prometheus_client")
    c = mock.MagicMock()
    c.api = object()  # truthy
    c.logged_in = True
    c.tick_callback = lambda *a, **k: None
    c._callbacks_registered = True
    c._event_callback_registered = True
    c.MAX_SUBSCRIPTIONS = 200
    c.symbols = [{"code": f"SYM{i}", "exchange": "OPT"} for i in range(3)]
    c.subscribed_count = 0
    c.subscribed_codes = set()
    c._last_resubscribe_ts = 0.0
    c.resubscribe_cooldown = 1.5
    c._failed_sub_symbols = __import__("collections").deque()
    quote_api = mock.MagicMock()
    quote_api.subscribe = mock.MagicMock()
    c._quote_api.return_value = quote_api
    c._ensure_callbacks = mock.MagicMock()
    c._refresh_quote_routes = mock.MagicMock()
    c._start_sub_retry_thread = mock.MagicMock()
    return c


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class TestResubscribeLock:
    """Concurrency contract for ``_resubscribe_all``."""

    def test_concurrent_resubscribe_serializes(self) -> None:
        """Two threads in ``_resubscribe_all`` simultaneously: only one body
        executes the broker-SDK subscribe loop; the other returns early.
        """
        pytest.importorskip("yaml")
        from hft_platform.feed_adapter.shioaji.subscription_manager import (
            SubscriptionManager,
        )

        c = _make_client()
        # Add the lock the production code is expected to own.
        c._resubscribe_lock = threading.Lock()

        # Slow each subscribe call so the two threads overlap inside the body.
        body_entered = threading.Event()
        proceed = threading.Event()

        def slow_subscribe(*args: object, **kwargs: object) -> None:
            body_entered.set()
            # Hold the lock on first thread until the other has tried.
            proceed.wait(timeout=2.0)

        c._quote_api.return_value.subscribe.side_effect = slow_subscribe

        mgr = SubscriptionManager(c)
        # SubscriptionManager has __slots__ so we can't setattr a counter on it.
        # Track calls via the underlying broker subscribe mock instead — every
        # successful _subscribe_symbol pass invokes ``quote_api.subscribe``
        # (Tick + BidAsk = 2 calls per symbol). The slow_subscribe side_effect
        # blocks on ``proceed``, so we infer "body entered" from body_entered.
        results: list[str] = []

        def worker(name: str) -> None:
            mgr._resubscribe_all()
            results.append(name)

        t1 = threading.Thread(target=worker, args=("t1",), daemon=True)
        t2 = threading.Thread(target=worker, args=("t2",), daemon=True)
        t1.start()
        # Wait until t1 is inside the body (lock held, doing slow subscribe).
        assert body_entered.wait(timeout=2.0), "first worker never entered body"
        t2.start()
        # Give t2 time to attempt and bounce off the lock.
        t2.join(timeout=1.0)
        # t2 must have returned without blocking on t1's slow subscribe.
        assert not t2.is_alive(), "second worker blocked instead of skipping"
        # Now release t1.
        proceed.set()
        t1.join(timeout=2.0)
        assert not t1.is_alive(), "first worker never finished"

        # t2 took the lock-skip path. t1 ran the body and made >=1 broker
        # subscribe call (slow_subscribe was invoked at least once).
        assert c._quote_api.return_value.subscribe.call_count >= 1, "t1 did not invoke broker subscribe at all"

    def test_subscribed_codes_clear_in_place(self) -> None:
        """Object identity of ``subscribed_codes`` is preserved across a
        ``_resubscribe_all`` cycle, so concurrent readers holding a
        reference always see a consistent live object.
        """
        pytest.importorskip("yaml")
        from hft_platform.feed_adapter.shioaji.subscription_manager import (
            SubscriptionManager,
        )

        c = _make_client()
        c._resubscribe_lock = threading.Lock()
        # Pre-seed the set with stale entries.
        c.subscribed_codes.update({"OLD_A", "OLD_B"})
        codes_id_before = id(c.subscribed_codes)
        codes_ref = c.subscribed_codes  # capture reference like a peer reader

        mgr = SubscriptionManager(c)
        mgr._resubscribe_all()

        # The SAME set object — never reassigned.
        assert id(c.subscribed_codes) == codes_id_before, (
            "subscribed_codes was rebound; peer threads holding the old "
            "reference would see stale state. Use clear() + add(), never "
            "``c.subscribed_codes = set()``."
        )
        # The captured reference must reflect the new state.
        assert codes_ref is c.subscribed_codes
        # Stale entries must be gone (cleared in place).
        assert "OLD_A" not in c.subscribed_codes
        assert "OLD_B" not in c.subscribed_codes

    def test_cooldown_under_lock(self) -> None:
        """The cooldown read-modify-write of ``_last_resubscribe_ts`` is
        guarded by the same lock as the body. Second caller sees the
        updated timestamp.
        """
        pytest.importorskip("yaml")
        from hft_platform.feed_adapter.shioaji.subscription_manager import (
            SubscriptionManager,
        )

        c = _make_client()
        c._resubscribe_lock = threading.Lock()
        # Force cooldown to trip on the second call (1.5s default), so the
        # second caller would early-return on cooldown — but only if the
        # first thread successfully wrote the ts under the lock.
        c.resubscribe_cooldown = 60.0

        mgr = SubscriptionManager(c)
        mgr._resubscribe_all()
        ts_after_first = c._last_resubscribe_ts
        assert ts_after_first > 0.0, "first call did not write _last_resubscribe_ts"

        # Second call must early-return on cooldown.
        prev_calls = c._quote_api.return_value.subscribe.call_count
        mgr._resubscribe_all()
        post_calls = c._quote_api.return_value.subscribe.call_count
        assert post_calls == prev_calls, "cooldown gate did not block second call"

    def test_metric_increments_on_skip(self) -> None:
        """When a concurrent caller bounces off the lock, the
        ``feed_resubscribe_skipped_concurrent_total`` metric increments.
        """
        pytest.importorskip("yaml")
        prom = pytest.importorskip("prometheus_client")
        from hft_platform.feed_adapter.shioaji.subscription_manager import (
            SubscriptionManager,
        )
        from hft_platform.observability.metrics import MetricsRegistry

        metrics = MetricsRegistry.get()
        # The counter must exist.
        counter = getattr(metrics, "feed_resubscribe_skipped_concurrent_total", None)
        assert counter is not None, "expected MetricsRegistry to expose feed_resubscribe_skipped_concurrent_total"

        # Snapshot current value (the registry is process-singleton so we
        # need to diff rather than assume zero).
        def _value() -> float:
            for sample in counter.collect()[0].samples:
                # Default Counter exposes both ``_total`` and ``_created``;
                # we want the cumulative value (no suffix or ``_total``).
                if sample.name.endswith("_total") or sample.name == counter._name:
                    return float(sample.value)
            return 0.0

        before = _value()

        c = _make_client()
        c._resubscribe_lock = threading.Lock()
        c.metrics = metrics

        body_entered = threading.Event()
        proceed = threading.Event()

        def slow_subscribe(*args: object, **kwargs: object) -> None:
            body_entered.set()
            proceed.wait(timeout=2.0)

        c._quote_api.return_value.subscribe.side_effect = slow_subscribe
        mgr = SubscriptionManager(c)

        def worker() -> None:
            mgr._resubscribe_all()

        t1 = threading.Thread(target=worker, daemon=True)
        t1.start()
        assert body_entered.wait(timeout=2.0)
        # Second caller must skip and bump metric.
        mgr._resubscribe_all()
        after_skip = _value()
        assert after_skip >= before + 1, f"metric did not increment on skip: before={before} after={after_skip}"

        proceed.set()
        t1.join(timeout=2.0)

        # Reference prom to silence unused-import lints.
        assert prom is not None


class TestSubscribedCodesIdentityAcrossResetPaths:
    """``subscribed_codes`` identity must also be preserved across the
    other reset paths that previously rebound it (recreate_api, reconnect
    orchestrator, contracts_runtime.refresh_symbols, options_refresh).
    """

    def test_no_rebind_in_source(self) -> None:
        """Static check: no module under ``feed_adapter/shioaji/`` rebinds
        ``subscribed_codes`` (i.e. no ``subscribed_codes = set(...)`` or
        ``subscribed_codes = <expr>``). All reset paths must mutate in
        place.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[2] / "src" / "hft_platform" / "feed_adapter" / "shioaji"
        assert root.exists(), f"expected feed_adapter/shioaji dir at {root}"
        offenders: list[tuple[str, int, str]] = []
        # Match ``[anything].subscribed_codes = ...`` and bare
        # ``subscribed_codes = ...`` at module/method scope, but skip
        # type-annotation forms like ``self.subscribed_codes: set[str] = set()``
        # in ``__init__`` (initial allocation is fine).
        rebind_pattern = re.compile(r"\bsubscribed_codes\s*=\s*set\(")
        annot_pattern = re.compile(r"subscribed_codes\s*:\s*set")
        for py in root.rglob("*.py"):
            for lineno, line in enumerate(py.read_text().splitlines(), start=1):
                if not rebind_pattern.search(line):
                    continue
                # Allow the annotated initial allocation in __init__.
                if annot_pattern.search(line):
                    continue
                offenders.append((str(py), lineno, line.strip()))
        assert not offenders, (
            "subscribed_codes is rebound (rather than mutated in place). "
            "Use ``subscribed_codes.clear()`` + ``subscribed_codes.update(...)`` "
            "to preserve object identity for concurrent readers. "
            f"Offenders: {offenders}"
        )

    def test_failed_sub_symbols_no_rebind_in_source(self) -> None:
        """Sibling check (L2 fix invariant): no module rebinds
        ``_failed_sub_symbols`` outside ``__init__``. Mutate the deque
        in place via ``clear()`` + ``extend()``.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[2] / "src" / "hft_platform" / "feed_adapter" / "shioaji"
        assert root.exists()
        offenders: list[tuple[str, int, str]] = []
        rebind_pattern = re.compile(r"_failed_sub_symbols\s*=\s*[^d]")
        # Allow only ``self._failed_sub_symbols: deque[...] = deque()`` and
        # ``self._failed_sub_symbols = deque(...)`` initial allocation.
        annot_or_deque_pattern = re.compile(r"_failed_sub_symbols\s*(?::\s*deque[^=]*)?\s*=\s*deque\b")
        for py in root.rglob("*.py"):
            for lineno, line in enumerate(py.read_text().splitlines(), start=1):
                stripped = line.strip()
                if "_failed_sub_symbols" not in stripped or "=" not in stripped:
                    continue
                # Skip comment lines (documentation often quotes forbidden patterns).
                if stripped.startswith("#"):
                    continue
                if rebind_pattern.search(stripped) and not annot_or_deque_pattern.search(stripped):
                    offenders.append((str(py), lineno, stripped))
        assert not offenders, f"_failed_sub_symbols is rebound (regression of the L2 fix). Offenders: {offenders}"
