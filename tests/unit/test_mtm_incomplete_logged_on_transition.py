"""An unpriceable book is a condition, not an event stream.

Production shape these are written from (THESHOW, ``docker logs hft-engine``):
one stale ``TMFI6`` position could not be priced from 2026-09-07T00:31Z to
2026-09-19T15:18Z. The supervisor runs at 1 Hz and both halves of the
mark-to-market path logged on every tick::

    48h window                                       lines
    -----------------------------------------------  -------
    mid_price_unavailable      (execution/mtm.py)      97,508
    mark-to-market incomplete  (services/system.py)    97,497
    everything else                                   106,333
    -----------------------------------------------  -------
    total engine log                                  301,338   -> 65% noise

248,724 ``mid_price_unavailable`` lines were written in total, every one of
them identical. The condition was real and worth knowing -- it holds the daily
stop latched -- but saying it 86,400 times a day is what made it unfindable,
and ``mark-to-market incomplete`` never named the instrument, so the log could
not answer the only question an operator actually has: *which one?*

These tests pin the two halves of the remedy: the calculator reports what it
could not price instead of logging it, and the supervisor speaks on the edges.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
import structlog
import structlog.testing

import hft_platform.services.system as system_module
from hft_platform.execution.mtm import MarkToMarketCalculator, mtm_unpriced_positions
from hft_platform.execution.positions import Position, PositionStore
from hft_platform.services.system import HFTSystem

SCALE = 10_000


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _store(*positions: Position) -> PositionStore:
    store = PositionStore.__new__(PositionStore)
    store.positions = {f"{p.account_id}:{p.strategy_id}:{p.symbol}": p for p in positions}
    store._fill_lock = threading.Lock()
    return store


def _pos(symbol: str, qty: int, strategy: str = "R47_MAKER_TMF", avg: int = 100) -> Position:
    pos = Position("acc", strategy, symbol)
    pos.net_qty = qty
    pos.avg_price_scaled = avg * SCALE
    return pos


def _calc(store: PositionStore, mids: dict[str, int]) -> MarkToMarketCalculator:
    return MarkToMarketCalculator(store, lambda s: mids.get(s))


def _gauge() -> float:
    return mtm_unpriced_positions._value.get()


# --------------------------------------------------------------------------- #
# The calculator reports, it does not narrate                                  #
# --------------------------------------------------------------------------- #


class TestTheCalculatorNamesWhatItCouldNotPrice:
    def test_a_fully_priced_book_names_nothing(self) -> None:
        calc = _calc(_store(_pos("TMFJ6", 1)), {"TMFJ6": 105 * SCALE})
        snap = calc.snapshot()
        assert snap.complete is True
        assert snap.unpriced_symbols == ()

    def test_the_unpriceable_symbol_is_named(self) -> None:
        """The 2026-09-07 shape: one position, no mid, book worth 0 but unknown."""
        calc = _calc(_store(_pos("TMFI6", 1)), {})
        snap = calc.snapshot()
        assert snap.complete is False
        assert snap.unpriced == 1
        assert snap.unpriced_symbols == ("TMFI6",)
        assert snap.priced == 0

    def test_two_strategies_on_one_symbol_name_it_once(self) -> None:
        """``unpriced`` counts positions; ``unpriced_symbols`` counts instruments."""
        calc = _calc(_store(_pos("TMFI6", 1, "R47_A"), _pos("TMFI6", -2, "R47_B")), {})
        snap = calc.snapshot()
        assert snap.unpriced == 2
        assert snap.unpriced_symbols == ("TMFI6",)

    def test_several_unpriceable_symbols_are_sorted(self) -> None:
        """Sorted so an operator diffing two dumps sees a real change, not a reorder."""
        calc = _calc(_store(_pos("TXFI6", 1), _pos("EXFI6", 1), _pos("TMFI6", 1)), {})
        assert _calc(_store(), {}).snapshot().unpriced_symbols == ()
        assert calc.snapshot().unpriced_symbols == ("EXFI6", "TMFI6", "TXFI6")

    def test_a_flat_position_is_priced_not_unpriced(self) -> None:
        """net_qty == 0 needs no mid; it must not hold the valuation open."""
        calc = _calc(_store(_pos("TMFI6", 0)), {})
        snap = calc.snapshot()
        assert snap.complete is True
        assert snap.unpriced_symbols == ()

    def test_a_partially_priced_book_names_only_the_gap(self) -> None:
        calc = _calc(_store(_pos("TMFJ6", 1), _pos("TMFI6", 1)), {"TMFJ6": 105 * SCALE})
        snap = calc.snapshot()
        assert snap.unpriced_symbols == ("TMFI6",)
        assert snap.priced == 1
        assert snap.total_scaled == 5 * SCALE

    def test_the_gap_is_exported_as_a_series(self) -> None:
        """13 days of a condition needs a series, not a line."""
        _calc(_store(_pos("TMFI6", 1), _pos("TXFI6", 1)), {}).snapshot()
        assert _gauge() == 2.0
        _calc(_store(_pos("TMFJ6", 1)), {"TMFJ6": 105 * SCALE}).snapshot()
        assert _gauge() == 0.0

    def test_repeated_snapshots_of_a_dead_book_write_no_log_lines(self) -> None:
        """The 1 Hz half of the 248,724 lines: the calculator must be silent."""
        calc = _calc(_store(_pos("TMFI6", 1)), {})
        with structlog.testing.capture_logs() as logs:
            # Canary: capture_logs swaps the global processor chain and goes
            # blind against loggers cached by an earlier test. Without this the
            # assertion below would pass for the wrong reason.
            structlog.get_logger("mtm_capture_canary").warning("capture_canary")
            for _ in range(100):
                calc.snapshot()
        assert any(e.get("event") == "capture_canary" for e in logs), "log capture went blind"
        assert [e for e in logs if e.get("event") == "mid_price_unavailable"] == []


# --------------------------------------------------------------------------- #
# The supervisor speaks on the edges                                           #
# --------------------------------------------------------------------------- #


def _snap(unpriced: tuple[str, ...], priced: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        total_scaled=0,
        priced=priced,
        unpriced=len(unpriced),
        unpriced_symbols=unpriced,
        complete=not unpriced,
    )


def _system(repeat_ticks: int = 3600) -> HFTSystem:
    system = HFTSystem.__new__(HFTSystem)
    system._mtm_last_incomplete = None
    system._mtm_last_unpriced = ()
    system._mtm_incomplete_ticks = 0
    system._mtm_last_log_tick = 0
    system._mtm_repeat_ticks = repeat_ticks
    return system


@pytest.fixture()
def sink(module_log_sink):
    return module_log_sink(system_module)


class TestTheSupervisorSpeaksOnTheEdges:
    def test_entering_the_state_is_said_once_with_the_symbol(self, sink: list[dict]) -> None:
        system = _system()
        system._log_mtm_completeness(_snap(("TMFI6",)))
        assert [e["event"] for e in sink] == ["mtm_incomplete"]
        assert sink[0]["symbols"] == ["TMFI6"]
        assert sink[0]["unpriced"] == 1
        assert sink[0]["log_level"] == "warning"

    def test_an_hour_of_the_same_dead_book_is_one_line_not_3600(self, sink: list[dict]) -> None:
        """The whole point: 3600 ticks, 1 line, until the reminder falls due."""
        system = _system(repeat_ticks=3600)
        for _ in range(3599):
            system._log_mtm_completeness(_snap(("TMFI6",)))
        assert [e["event"] for e in sink] == ["mtm_incomplete"]

    def test_the_reminder_falls_due_at_the_interval(self, sink: list[dict]) -> None:
        """A state that blocks the daily stop must not vanish from `logs --since 1h`."""
        system = _system(repeat_ticks=10)
        for _ in range(21):
            system._log_mtm_completeness(_snap(("TMFI6",)))
        assert [e["event"] for e in sink] == [
            "mtm_incomplete",
            "mtm_still_incomplete",
            "mtm_still_incomplete",
        ]
        assert [e["ticks_incomplete"] for e in sink] == [1, 11, 21]
        assert sink[-1]["symbols"] == ["TMFI6"]

    def test_a_new_unpriceable_symbol_speaks_immediately(self, sink: list[dict]) -> None:
        """A second instrument going dark is news, not a repeat."""
        system = _system(repeat_ticks=3600)
        system._log_mtm_completeness(_snap(("TMFI6",)))
        system._log_mtm_completeness(_snap(("TMFI6",)))
        system._log_mtm_completeness(_snap(("EXFI6", "TMFI6")))
        assert [e["event"] for e in sink] == ["mtm_incomplete", "mtm_incomplete"]
        assert sink[-1]["symbols"] == ["EXFI6", "TMFI6"]

    def test_the_recovery_is_an_event_too(self, sink: list[dict]) -> None:
        system = _system()
        system._log_mtm_completeness(_snap(("TMFI6",)))
        system._log_mtm_completeness(_snap((), priced=1))
        assert [e["event"] for e in sink] == ["mtm_incomplete", "mtm_complete"]
        assert sink[-1]["recovered_symbols"] == ["TMFI6"]
        assert sink[-1]["log_level"] == "info"

    def test_a_healthy_book_is_never_mentioned(self, sink: list[dict]) -> None:
        system = _system()
        for _ in range(500):
            system._log_mtm_completeness(_snap((), priced=3))
        assert sink == []

    def test_recovery_is_said_once_not_on_every_healthy_tick(self, sink: list[dict]) -> None:
        system = _system()
        system._log_mtm_completeness(_snap(("TMFI6",)))
        for _ in range(500):
            system._log_mtm_completeness(_snap((), priced=1))
        assert [e["event"] for e in sink] == ["mtm_incomplete", "mtm_complete"]

    def test_a_second_outage_starts_its_own_clock(self, sink: list[dict]) -> None:
        """The duration reported must be this outage's, not a running total.

        With ``repeat_ticks=5`` the first reminder lands 5 ticks after the
        opening line, i.e. on tick 6, and every later one 5 ticks after that.
        """
        system = _system(repeat_ticks=5)
        for _ in range(12):
            system._log_mtm_completeness(_snap(("TMFI6",)))
        system._log_mtm_completeness(_snap((), priced=1))
        for _ in range(6):
            system._log_mtm_completeness(_snap(("TMFI6",)))
        reminders = [e for e in sink if e["event"] == "mtm_still_incomplete"]
        assert [e["ticks_incomplete"] for e in reminders] == [6, 11, 6]

    def test_reminders_can_be_turned_off_leaving_only_the_edges(self, sink: list[dict]) -> None:
        system = _system(repeat_ticks=0)
        for _ in range(10_000):
            system._log_mtm_completeness(_snap(("TMFI6",)))
        system._log_mtm_completeness(_snap((), priced=1))
        assert [e["event"] for e in sink] == ["mtm_incomplete", "mtm_complete"]

    def test_a_snapshot_without_symbol_names_still_reports_the_state(self, sink: list[dict]) -> None:
        """An older snapshot shape must not crash the supervisor tick."""
        system = _system()
        system._log_mtm_completeness(SimpleNamespace(priced=0, unpriced=1, complete=False))
        assert [e["event"] for e in sink] == ["mtm_incomplete"]
        assert sink[0]["symbols"] == []


class TestTheRepeatIntervalIsConfigurable:
    def test_the_default_is_hourly_at_one_hertz(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_MTM_INCOMPLETE_REPEAT_TICKS", raising=False)
        assert system_module._mtm_repeat_ticks_from_env() == 3600

    def test_an_explicit_interval_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_MTM_INCOMPLETE_REPEAT_TICKS", "60")
        assert system_module._mtm_repeat_ticks_from_env() == 60

    def test_zero_means_edges_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_MTM_INCOMPLETE_REPEAT_TICKS", "0")
        assert system_module._mtm_repeat_ticks_from_env() == 0

    def test_a_negative_interval_is_clamped_to_edges_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_MTM_INCOMPLETE_REPEAT_TICKS", "-1")
        assert system_module._mtm_repeat_ticks_from_env() == 0

    def test_a_junk_interval_falls_back_to_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_MTM_INCOMPLETE_REPEAT_TICKS", "hourly")
        assert system_module._mtm_repeat_ticks_from_env() == 3600
