"""A strategy follows the front month across a delivery, not only across a connect.

THESHOW, 2026-09-15 to 2026-09-19::

    09-15 06:33Z  engine boots, connect  ->  populator: TMF/R1 = TMFI6
    09-16 13:30   TMFI6 final session closes (Taipei), TMFJ6 is the front month
                  X  nothing re-evaluates the binding: it was decided at connect
    09-16 onward  R47.symbols = {TMFI6}  ->  no quotes, no orders, for days,
                  while TMFJ6 quoted normally on the same feed

Two defects, both pinned here:

1. The populator decided "expired" on the UTC *date*. A contract is live
   until 13:30 exchange time on its delivery date and dead after it, so the
   date alone is wrong for the rest of the delivery day, including its night
   session.
2. The binding was only ever computed at connect. ``ShioajiFamilyPopulator``
   re-evaluates the table it last read whenever the delivery cutoff moves
   past a listed delivery date.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from hft_platform.contracts.family_resolver import ContractFamilyResolver
from hft_platform.contracts.ref import ContractFamily, FamilyCode, Product
from hft_platform.core.market_calendar import taifex_delivery_cutoff, taifex_monthly_delivery_date
from hft_platform.feed_adapter.shioaji import family_populator
from hft_platform.feed_adapter.shioaji.family_populator import (
    ShioajiFamilyPopulator,
    populate_resolver_from_shioaji,
)

_TAIPEI = ZoneInfo("Asia/Taipei")
_TMF_R1 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
_TMF_R2 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R2)


def _ns(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=_TAIPEI).timestamp()) * 1_000_000_000


class _Futures:
    """``api.Contracts.Futures``: iterable roots, indexable by root."""

    def __init__(self, roots: dict[str, list]) -> None:
        self._roots = roots

    def keys(self):
        return self._roots.keys()

    def __getitem__(self, root: str) -> list:
        return self._roots[root]


def _api_on_2026_09_15() -> SimpleNamespace:
    """The TMF listing the engine read at its 09-15 connect."""
    listed = [
        ("TMFI6", "2026/09/16"),
        ("TMFJ6", "2026/10/21"),
        ("TMFK6", "2026/11/18"),
        ("TMFL6", "2026/12/16"),
        ("TMFR1", "2026/09/16"),  # alias rows carry a date too; they must be ignored
    ]
    contracts = [SimpleNamespace(code=c, delivery_date=d, delivery_month=None) for c, d in listed]
    return SimpleNamespace(Contracts=SimpleNamespace(Futures=_Futures({"TMF": contracts})))


def _runner_with_r47(resolver: ContractFamilyResolver):
    from hft_platform.strategy.runner import StrategyRunner

    strategy = SimpleNamespace(strategy_id="R47_MAKER_TMF", symbols=set(), contract_families=(_TMF_R1,))
    runner = StrategyRunner.__new__(StrategyRunner)
    runner.strategies = [strategy]
    runner._family_resolver = None
    runner.set_family_resolver(resolver)
    return strategy


# --------------------------------------------------------------------------- #
# 1. The cutoff is exchange time, and it moves at 13:30, not at midnight       #
# --------------------------------------------------------------------------- #


class TestDeliveryCutoff:
    def test_contract_delivering_today_is_live_one_minute_before_its_final_close(self) -> None:
        assert taifex_delivery_cutoff(_ns(2026, 9, 16, 13, 29)) == date(2026, 9, 16)

    def test_contract_delivering_today_is_settled_at_its_final_close(self) -> None:
        assert taifex_delivery_cutoff(_ns(2026, 9, 16, 13, 30)) == date(2026, 9, 17)

    def test_delivery_day_night_session_already_belongs_to_the_next_month(self) -> None:
        """16:00 on the delivery day is 08:00Z the same day, where a UTC-date rule still said 09-16."""
        assert taifex_delivery_cutoff(_ns(2026, 9, 16, 16, 0)) == date(2026, 9, 17)

    def test_cutoff_does_not_move_again_at_midnight(self) -> None:
        assert taifex_delivery_cutoff(_ns(2026, 9, 17, 0, 30)) == date(2026, 9, 17)

    def test_early_morning_taipei_is_the_taipei_date_not_the_utc_one(self) -> None:
        """05:00 Taipei is 21:00Z the previous day."""
        assert taifex_delivery_cutoff(_ns(2026, 9, 17, 5, 0)) == date(2026, 9, 17)

    @pytest.mark.parametrize(
        ("year", "month", "expected"),
        [(2026, 9, date(2026, 9, 16)), (2026, 10, date(2026, 10, 21)), (2026, 7, date(2026, 7, 15))],
    )
    def test_monthly_delivery_date_is_the_third_wednesday(self, year: int, month: int, expected: date) -> None:
        """Checked against the broker's own delivery dates for TMFI6, TMFJ6, TMFG6."""
        assert taifex_monthly_delivery_date(year, month) == expected


# --------------------------------------------------------------------------- #
# 2. A connect on the delivery day binds by the clock, not the date            #
# --------------------------------------------------------------------------- #


class TestPopulateOnDeliveryDay:
    @pytest.mark.parametrize(
        ("when", "front", "second"),
        [
            (_ns(2026, 9, 16, 13, 29), "TMFI6", "TMFJ6"),
            (_ns(2026, 9, 16, 13, 31), "TMFJ6", "TMFK6"),
            (_ns(2026, 9, 17, 9, 0), "TMFJ6", "TMFK6"),
        ],
        ids=["before-final-close", "after-final-close", "next-day"],
    )
    def test_front_month_follows_the_final_session_close(self, when: int, front: str, second: str) -> None:
        resolver = ContractFamilyResolver()

        ShioajiFamilyPopulator(resolver).populate(_api_on_2026_09_15(), now_ns=when)

        assert resolver.resolve_family(_TMF_R1).display() == front
        assert resolver.resolve_family(_TMF_R2).display() == second

    def test_the_legacy_function_uses_the_same_cutoff_when_no_date_is_given(self, monkeypatch) -> None:
        monkeypatch.setattr(family_populator.timebase, "now_ns", lambda: _ns(2026, 9, 16, 20, 0))
        resolver = ContractFamilyResolver()

        populate_resolver_from_shioaji(resolver, _api_on_2026_09_15())

        assert resolver.resolve_family(_TMF_R1).display() == "TMFJ6"


# --------------------------------------------------------------------------- #
# 3. The binding moves when the contract settles, with no connect in between   #
# --------------------------------------------------------------------------- #


class TestRollWithoutReconnect:
    def test_r47_is_rebound_to_the_next_month_when_its_contract_settles(self) -> None:
        """The production sequence: connect 09-15, then only the clock moves."""
        resolver = ContractFamilyResolver()
        r47 = _runner_with_r47(resolver)
        populator = ShioajiFamilyPopulator(resolver)
        populator.populate(_api_on_2026_09_15(), now_ns=_ns(2026, 9, 15, 14, 33))
        assert r47.symbols == {"TMFI6"}

        rolled = populator.roll_if_due(now_ns=_ns(2026, 9, 16, 13, 31))

        assert rolled == 2  # R1 and R2 both moved
        assert r47.symbols == {"TMFJ6"}

    def test_nothing_moves_before_the_final_close(self) -> None:
        resolver = ContractFamilyResolver()
        r47 = _runner_with_r47(resolver)
        populator = ShioajiFamilyPopulator(resolver)
        populator.populate(_api_on_2026_09_15(), now_ns=_ns(2026, 9, 15, 14, 33))

        assert populator.roll_if_due(now_ns=_ns(2026, 9, 16, 13, 29)) == 0
        assert r47.symbols == {"TMFI6"}

    def test_a_day_on_which_nothing_delivers_does_not_rebuild_the_table(self) -> None:
        """The cutoff advances daily at 13:30; a rebuild is only due when a listed delivery date passed."""
        resolver = ContractFamilyResolver()
        populator = ShioajiFamilyPopulator(resolver)
        populator.populate(_api_on_2026_09_15(), now_ns=_ns(2026, 9, 17, 9, 0))
        installed = resolver.snapshot

        assert populator.roll_if_due(now_ns=_ns(2026, 9, 17, 13, 31)) == 0
        assert resolver.snapshot is installed
        assert populator.delivery_cutoff == date(2026, 9, 18)

    def test_a_second_roll_check_after_the_roll_fires_no_hook(self) -> None:
        resolver = ContractFamilyResolver()
        fired: list[object] = []
        resolver.add_hook(fired.append)
        populator = ShioajiFamilyPopulator(resolver)
        populator.populate(_api_on_2026_09_15(), now_ns=_ns(2026, 9, 15, 14, 33))
        populator.roll_if_due(now_ns=_ns(2026, 9, 16, 13, 31))
        fired.clear()

        assert populator.roll_if_due(now_ns=_ns(2026, 9, 16, 13, 32)) == 0
        assert fired == []

    def test_a_repeat_connect_with_the_same_table_fires_no_hook(self) -> None:
        resolver = ContractFamilyResolver()
        populator = ShioajiFamilyPopulator(resolver)
        populator.populate(_api_on_2026_09_15(), now_ns=_ns(2026, 9, 17, 9, 0))
        fired: list[object] = []
        resolver.add_hook(fired.append)

        populator.populate(_api_on_2026_09_15(), now_ns=_ns(2026, 9, 17, 10, 0))

        assert fired == []

    def test_a_clock_that_steps_backwards_does_not_rebind_to_the_settled_month(self) -> None:
        resolver = ContractFamilyResolver()
        r47 = _runner_with_r47(resolver)
        populator = ShioajiFamilyPopulator(resolver)
        populator.populate(_api_on_2026_09_15(), now_ns=_ns(2026, 9, 16, 13, 31))

        assert populator.roll_if_due(now_ns=_ns(2026, 9, 16, 13, 0)) == 0
        assert r47.symbols == {"TMFJ6"}

    def test_no_roll_is_attempted_before_the_first_connect_has_read_a_table(self) -> None:
        populator = ShioajiFamilyPopulator(ContractFamilyResolver())

        assert populator.roll_if_due(now_ns=_ns(2026, 9, 16, 13, 31)) == 0
        assert populator.delivery_cutoff is None

    def test_a_family_with_nothing_left_to_roll_to_is_reported_not_silently_kept(self, module_log_sink) -> None:
        only_sept = SimpleNamespace(code="TMFI6", delivery_date="2026/09/16", delivery_month=None)
        api = SimpleNamespace(Contracts=SimpleNamespace(Futures=_Futures({"TMF": [only_sept]})))
        resolver = ContractFamilyResolver()
        populator = ShioajiFamilyPopulator(resolver)
        populator.populate(api, now_ns=_ns(2026, 9, 15, 14, 33))

        logs = module_log_sink(family_populator)
        populator.roll_if_due(now_ns=_ns(2026, 9, 16, 13, 31))

        unbound = [e for e in logs if e["event"] == "contract_family_roll_left_unbound"]
        assert len(unbound) == 1
        assert unbound[0]["log_level"] == "warning"
        assert unbound[0]["families"] == [str(_TMF_R1)]

    def test_each_rebind_is_logged_with_the_contract_it_left(self, module_log_sink) -> None:
        resolver = ContractFamilyResolver()
        populator = ShioajiFamilyPopulator(resolver)
        populator.populate(_api_on_2026_09_15(), now_ns=_ns(2026, 9, 15, 14, 33))

        logs = module_log_sink(family_populator)
        populator.roll_if_due(now_ns=_ns(2026, 9, 16, 13, 31))

        rolled = {e["old_ref"]: e["new_ref"] for e in logs if e["event"] == "contract_family_rolled"}
        assert rolled == {"TMFI6": "TMFJ6", "TMFJ6": "TMFK6"}


# --------------------------------------------------------------------------- #
# 4. The clock keeps running through a failing pass                             #
# --------------------------------------------------------------------------- #


def test_roll_clock_survives_a_failing_check(module_log_sink) -> None:
    """One exception ended the quote watchdog once; this loop must not share that fate."""
    calls: list[int] = []

    class _Flaky(ShioajiFamilyPopulator):
        __slots__ = ()

        def roll_if_due(self, *, now_ns: int | None = None) -> int:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return 0

    async def _run() -> None:
        task = asyncio.create_task(_Flaky(ContractFamilyResolver()).run_roll_clock(interval_s=0.001))
        for _ in range(500):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    logs = module_log_sink(family_populator)
    asyncio.run(_run())

    assert len(calls) >= 3
    assert any(e["event"] == "contract_family_roll_check_failed" for e in logs)


def test_bootstrap_schedules_the_roll_clock_for_shioaji() -> None:
    """The clock only helps if something starts it; pin the wiring."""
    import inspect

    from hft_platform.services import bootstrap

    source = inspect.getsource(bootstrap)

    assert "shioaji_family_populator.populate(" in source
    assert "deferred_tasks.append(shioaji_family_populator.run_roll_clock())" in source
