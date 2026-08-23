"""Six latent defects found 2026-08-23, none of them on a default-on path.

They are grouped because they share a shape: each is correct-looking code
whose wrongness is invisible until a non-default configuration, a different
host timezone, or a different market makes it reachable.
"""

import asyncio
import datetime as dt
import os
from unittest.mock import patch

import pytest

from hft_platform.core.session_hooks import SessionHookManager


class TestMultiWriterPublishWakesConsumers:
    """`consume()` awaits its OWN Event from `_consumer_signals`; the
    multi-writer branch only set the legacy shared `self.signal`, so every
    consumer parked forever. A hang, not a slowdown."""

    @staticmethod
    def _bus():
        from hft_platform.engine.event_bus import RingBufferBus

        bus = RingBufferBus(size=64)
        bus.single_writer = False  # HFT_BUS_SINGLE_WRITER=0
        return bus

    def test_publish_wakes_a_waiting_consumer(self) -> None:
        async def run() -> list:
            bus = self._bus()
            got: list = []

            async def reader() -> None:
                async for ev in bus.consume(consumer_name="t"):
                    got.append(ev)
                    return

            task = asyncio.create_task(reader())
            await asyncio.sleep(0)  # let the consumer register its signal
            await bus.publish(("tick", 1))
            await asyncio.wait_for(task, timeout=2.0)
            return got

        assert asyncio.run(run()) == [("tick", 1)]

    def test_publish_many_wakes_a_waiting_consumer(self) -> None:
        async def run() -> list:
            bus = self._bus()
            got: list = []

            async def reader() -> None:
                async for ev in bus.consume(consumer_name="t"):
                    got.append(ev)
                    return

            task = asyncio.create_task(reader())
            await asyncio.sleep(0)
            await bus.publish_many([("tick", 1), ("tick", 2)])
            await asyncio.wait_for(task, timeout=2.0)
            return got

        assert asyncio.run(run())


class TestBackfillLogReportsOnlyNewIds:
    def test_only_newly_written_ids_are_reported(self) -> None:
        """The old expression recomputed the list AFTER the mutations, using a
        predicate (`order_id_map.get(i) == order_key`) that every
        already-correct entry satisfies too — so ids that were already mapped
        were reported as new and the log could not show what a backfill did."""
        import threading
        import types

        from hft_platform.execution.router import ExecutionRouter

        class Resolver:
            def __init__(self) -> None:
                self.order_id_map = {"ALREADY": "K1"}
                self.lock = threading.Lock()

            def normalize_order_key(self, key):
                return key

            def set_order_id_mapping(self, token, order_key, source=""):
                self.order_id_map[token] = order_key

        resolver = Resolver()
        router = ExecutionRouter.__new__(ExecutionRouter)
        router.normalizer = types.SimpleNamespace(order_id_resolver=resolver)

        raw = types.SimpleNamespace(data={"id": "ALREADY", "ordno": "FRESH"})

        records: list = []
        with patch("hft_platform.execution.router.logger") as log:
            log.debug.side_effect = lambda *a, **k: records.append(k)
            router._backfill_order_id_map(raw)

        backfill = [r for r in records if r.get("new_ids") is not None]
        assert backfill, f"no backfill record emitted; got {records}"
        assert backfill[-1]["new_ids"] == ["FRESH"]
        assert resolver.order_id_map["FRESH"] == "K1"


class TestEvictionUsesTheTradingDate:
    def test_trading_date_is_the_taipei_date_not_the_host_date(self) -> None:
        """`date.today()` reads the SYSTEM-local date; every expiry in the
        registry is a Taiwan trading date. Prod containers set TZ=Asia/Taipei
        so the two coincide there, but CI runners default to UTC, where the UTC
        date lags the TW date for the 8 hours of the TW morning."""
        from hft_platform.core import timebase
        from hft_platform.core.instrument_registry import _trading_date_today

        expected = dt.datetime.fromtimestamp(timebase.now_ns() / 1e9, tz=timebase.TZINFO).date()
        assert _trading_date_today() == expected

    def test_the_trading_date_can_differ_from_the_host_date(self) -> None:
        """Documents the gap the fix closes, without depending on the host TZ."""
        from hft_platform.core import timebase

        taipei = dt.datetime.fromtimestamp(timebase.now_ns() / 1e9, tz=timebase.TZINFO)
        utc = dt.datetime.fromtimestamp(timebase.now_ns() / 1e9, tz=dt.timezone.utc)
        # Same instant; the DATE differs for 8 hours out of every 24.
        assert (taipei.date() != utc.date()) == (taipei.hour < 8)


class TestSessionHooksAskAboutFutures:
    def test_the_default_product_is_futures(self) -> None:
        """The platform is TAIFEX-primary; `is_trading_hours(now)` with no
        product_type answers about the TWSE stock day session (09:00-13:30)."""
        mgr = SessionHookManager()
        assert mgr._product_type == "future"

    def test_the_product_is_configurable(self) -> None:
        with patch.dict(os.environ, {"HFT_SESSION_HOOKS_PRODUCT": "stock"}):
            assert SessionHookManager()._product_type == "stock"

    def test_the_futures_night_session_reads_as_market_open(self) -> None:
        """23:00 TST: TAIFEX night session is trading, TWSE closed 9.5h ago.
        With the old default this returned POST_MARKET for the whole night, so
        neither night boundary ever fired a hook."""
        mgr = SessionHookManager()
        cal = mgr._get_calendar()
        night = dt.datetime(2026, 5, 13, 23, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        if not cal.is_trading_day(night.date()):
            pytest.skip("chosen date is not a trading day in this calendar build")

        assert cal.is_trading_hours(night, product_type="future") is True
        assert cal.is_trading_hours(night) is False  # the old, wrong question


class TestFamilyResolverRolloverStillDiffs:
    def test_removing_the_dead_set_did_not_change_the_diff(self) -> None:
        """Guard for the dead-code deletion: `seen` was populated and never
        read, but the diff it sat next to must still be produced."""
        from hft_platform.contracts.family_resolver import ContractFamilyResolver

        resolver = ContractFamilyResolver.__new__(ContractFamilyResolver)
        resolver._hooks = []

        class Snap:
            def __init__(self, mapping):
                self.family_map = mapping
                self.snapshot_ns = 1

        resolver._snapshot = Snap({"F1": "OLD", "F2": "SAME"})
        changes = resolver.swap_snapshot(Snap({"F1": "NEW", "F2": "SAME", "F3": "ADDED"}))

        changed = {c.family: (c.old_ref, c.new_ref) for c in changes}
        assert changed == {"F1": ("OLD", "NEW"), "F3": (None, "ADDED")}


class TestMonotonicSentinelsAreNegativeInfinity:
    def test_the_checkpoint_and_sweep_stamps_start_at_negative_infinity(self) -> None:
        """0.0 is "long ago" on a wall clock and "at boot" on a monotonic one.
        The fill-dedup twin was fixed in PR #446; these two were left behind."""
        from hft_platform.order.adapter import OrderAdapter

        src = __import__("inspect").getsource(OrderAdapter.__init__)
        assert '_order_id_map_last_persist_s: float = float("-inf")' in src
        assert '_live_orders_last_sweep_s: float = float("-inf")' in src
