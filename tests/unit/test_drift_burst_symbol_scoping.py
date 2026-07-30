"""The drift-burst detector must be fed one instrument, not whichever iterates first.

``HFTSystem`` fed a single ``DriftBurstDetector`` from
``lob_engine.books.items()`` with a ``break`` — the first symbol with a valid
mid. ``books`` is a plain dict that evicts stale symbols on a TTL, so the
"first" entry rotated between contracts over time and the detector's ring
buffer ended up holding log-returns interleaved across instruments. Its
t-statistic then measured the price gap *between contracts*, not drift in
either: production logged the same detector reporting spreads of 3.75 and
48.45 points, 405 "bursts" in 24 h, none attributable to a symbol because
``symbol`` was accepted but never logged.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import structlog.testing

from hft_platform.risk.drift_burst_detector import DriftBurstDetector
from hft_platform.services.system import HFTSystem


def _book(mid_x2: int, spread: int = 100, imbalance: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(mid_price_x2=mid_x2, spread=spread, imbalance=imbalance)


def _system() -> HFTSystem:
    system = HFTSystem.__new__(HFTSystem)
    system._drift_burst_symbol = ""
    return system


# --------------------------------------------------------------------------- #
# Reference-symbol latch                                                       #
# --------------------------------------------------------------------------- #


def test_drift_burst_symbol_latch_survives_book_iteration_order():
    """A reordered dict must not move the reference instrument."""
    system = _system()
    lob = SimpleNamespace(books={"TXFH6": _book(200_000_00), "TMFH6": _book(199_000_00)})

    assert system._drift_burst_book(lob) is lob.books["TXFH6"]
    assert system._drift_burst_symbol == "TXFH6"

    # Same symbols, different insertion order — the latch must hold.
    lob.books = {"TMFH6": _book(199_000_00), "TXFH6": _book(200_100_00)}
    assert system._drift_burst_book(lob) is lob.books["TXFH6"]
    assert system._drift_burst_symbol == "TXFH6"


def test_drift_burst_reference_symbol_repicked_when_evicted():
    """TTL eviction of the reference symbol is the one time it may change."""
    system = _system()
    lob = SimpleNamespace(books={"TXFH6": _book(200_000_00), "TMFH6": _book(199_000_00)})
    system._drift_burst_book(lob)

    del lob.books["TXFH6"]  # evicted by the LOB engine's TTL sweep
    assert system._drift_burst_book(lob) is lob.books["TMFH6"]
    assert system._drift_burst_symbol == "TMFH6"


def test_drift_burst_reference_symbol_repicked_when_mid_goes_invalid():
    system = _system()
    lob = SimpleNamespace(books={"TXFH6": _book(200_000_00), "TMFH6": _book(199_000_00)})
    system._drift_burst_book(lob)

    lob.books["TXFH6"] = _book(0)  # one-sided / empty book
    assert system._drift_burst_book(lob) is lob.books["TMFH6"]
    assert system._drift_burst_symbol == "TMFH6"


def test_drift_burst_reference_symbol_change_is_logged():
    """An unattributable escalation is an undiagnosable one."""
    system = _system()
    lob = SimpleNamespace(books={"TXFH6": _book(200_000_00)})

    with structlog.testing.capture_logs() as logs:
        system._drift_burst_book(lob)

    events = [e for e in logs if e.get("event") == "drift_burst_reference_symbol_changed"]
    assert events
    assert events[0]["symbol"] == "TXFH6"
    assert events[0]["previous"] is None


def test_drift_burst_book_returns_none_when_no_book_has_a_valid_mid():
    system = _system()
    lob = SimpleNamespace(books={"TXFH6": _book(0), "TMFH6": _book(0)})
    assert system._drift_burst_book(lob) is None
    assert system._drift_burst_symbol == ""


def test_drift_burst_book_returns_none_when_books_are_empty():
    system = _system()
    assert system._drift_burst_book(SimpleNamespace(books={})) is None


# --------------------------------------------------------------------------- #
# Reference-symbol *selection* (the latch fixed rotation, not choice)          #
# --------------------------------------------------------------------------- #


def test_drift_burst_reference_prefers_the_tightest_spread_over_dict_order():
    """Latching held one symbol, but it was still whichever iterated first.

    In production that was ``EXFH6`` — a far-month contract quoting a 48-point
    spread — so the thinnest book on the system drove a platform-wide toxicity
    gate and ~346 bursts a day.
    """
    system = _system()
    lob = SimpleNamespace(
        books={
            "EXFH6": _book(200_000_00, spread=487_500),  # 48.75 pt, first in dict
            "TXFH6": _book(200_000_00, spread=10_000),  # 1 pt, front month
        }
    )

    assert system._drift_burst_book(lob) is lob.books["TXFH6"]
    assert system._drift_burst_symbol == "TXFH6"


def test_drift_burst_reference_compares_spread_relative_to_price():
    """A cheap contract's small absolute spread is not automatically tighter."""
    system = _system()
    lob = SimpleNamespace(
        books={
            "CHEAP": _book(1_000_00, spread=500),  # 0.5% of mid
            "RICH": _book(200_000_00, spread=20_000),  # 0.1% of mid
        }
    )

    assert system._drift_burst_book(lob) is lob.books["RICH"]
    assert system._drift_burst_symbol == "RICH"


def test_drift_burst_reference_does_not_treat_a_locked_book_as_most_liquid():
    """A zero spread is a locked or half-built book, not a liquidity win."""
    system = _system()
    lob = SimpleNamespace(
        books={
            "LOCKED": _book(200_000_00, spread=0),
            "TXFH6": _book(200_000_00, spread=10_000),
        }
    )

    assert system._drift_burst_book(lob) is lob.books["TXFH6"]


def test_drift_burst_reference_falls_back_when_every_book_is_locked():
    """Returning None here would silently disable the toxicity gate entirely."""
    system = _system()
    lob = SimpleNamespace(
        books={
            "AXFH6": _book(200_000_00, spread=0),
            "BXFH6": _book(199_000_00, spread=0),
        }
    )

    assert system._drift_burst_book(lob) is lob.books["AXFH6"]
    assert system._drift_burst_symbol == "AXFH6"


def test_drift_burst_reference_log_carries_the_chosen_spread():
    """The spread is what makes a bad reference choice visible in the logs."""
    system = _system()
    lob = SimpleNamespace(books={"TXFH6": _book(200_000_00, spread=10_000)})

    with structlog.testing.capture_logs() as logs:
        system._drift_burst_book(lob)

    events = [e for e in logs if e.get("event") == "drift_burst_reference_symbol_changed"]
    assert events
    assert events[0]["spread_scaled"] == 10_000


# --------------------------------------------------------------------------- #
# Detector symbol scoping                                                      #
# --------------------------------------------------------------------------- #


def test_drift_burst_detector_resets_when_reference_symbol_changes():
    """A window built from one contract must not carry into the next."""
    detector = DriftBurstDetector(window_size=10, burst_threshold=3.0)
    for i in range(10):
        detector.evaluate(mid_price_x2=200_000_00 + i * 100, ts=i * 1_000_000, symbol="TXFH6")
    assert detector._count > 0

    detector.evaluate(mid_price_x2=40_000_00, ts=11_000_000, symbol="TMFH6")

    assert detector._symbol == "TMFH6"
    # Fresh window: only the first post-reset sample seeded _last_mid_x2.
    assert detector._count == 0
    assert detector._drift_sum == 0.0


def test_drift_burst_detector_keeps_its_window_while_the_symbol_holds():
    detector = DriftBurstDetector(window_size=10, burst_threshold=3.0)
    for i in range(6):
        detector.evaluate(mid_price_x2=200_000_00 + i * 100, ts=i * 1_000_000, symbol="TXFH6")
    count_before = detector._count
    assert count_before > 0

    detector.evaluate(mid_price_x2=200_000_00 + 700, ts=7_000_000, symbol="TXFH6")
    assert detector._count == count_before + 1


def test_drift_burst_detector_does_not_reset_on_the_first_symbol():
    """Latching the very first symbol is not a change and must not reset."""
    detector = DriftBurstDetector(window_size=10, burst_threshold=3.0)
    detector.evaluate(mid_price_x2=200_000_00, ts=1_000_000, symbol="TXFH6")
    detector.evaluate(mid_price_x2=200_010_00, ts=2_000_000, symbol="TXFH6")
    assert detector._symbol == "TXFH6"
    assert detector._last_mid_x2 == 200_010_00


def test_drift_burst_log_includes_symbol():
    """The burst log must name the instrument it fired on."""
    detector = DriftBurstDetector(window_size=10, burst_threshold=0.5, min_bpv=0.0)

    with structlog.testing.capture_logs() as logs:
        # A steady one-directional ramp drives |T| past a deliberately low
        # threshold without needing a synthetic jump.
        for i in range(60):
            detector.evaluate(
                mid_price_x2=200_000_00 + i * 200,
                spread_scaled=100,
                ts=(i + 1) * 1_000_000,
                symbol="TXFH6",
            )

    bursts = [e for e in logs if e.get("event") == "drift_burst_detected"]
    assert bursts, "expected at least one burst from a monotonic ramp"
    assert bursts[0]["symbol"] == "TXFH6"


# --------------------------------------------------------------------------- #
# StormGuard passes the symbol through                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("symbol", ["TXFH6", "TMFH6"])
def test_storm_guard_forwards_symbol_to_the_detector(symbol):
    from hft_platform.risk.storm_guard import StormGuard

    seen: list[str] = []

    class _Detector:
        def evaluate(self, mid_price_x2, spread_scaled=0, imbalance=0.0, ts=0, symbol=""):
            seen.append(symbol)
            return SimpleNamespace(burst_detected=False, toxicity_score=0.0, burst_event=None)

    guard = StormGuard(drift_burst_detector=_Detector())
    guard.update_with_lob(mid_price_x2=200_000_00, spread_scaled=100, ts=1, symbol=symbol)

    assert seen == [symbol]
