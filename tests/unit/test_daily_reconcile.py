"""Unit tests for daily_reconcile.py comparison logic.

Tests exercise the pure ``compare_positions`` function in isolation — no
broker API calls or ClickHouse connections are made.
"""

from __future__ import annotations

import os
import sys

# Allow importing the script as a module (scripts/ is not a package)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from daily_reconcile import compare_positions  # type: ignore[import]

# Tolerance used across tests: 100_000 scaled units = 10 NTD
_TOLERANCE = 100_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pos(qty: int, pnl: int) -> dict:
    return {"qty": qty, "pnl": pnl}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_matching_positions_returns_true() -> None:
    """Same qty and PnL on all three sides → MATCH."""
    broker = {"2330": _pos(100, 5_000_000)}
    platform = {"2330": _pos(100, 5_000_000)}
    ch = {"2330": _pos(100, 5_000_000)}

    is_match, mismatches = compare_positions(broker, platform, ch, tolerance=_TOLERANCE)

    assert is_match is True
    assert mismatches == []


def test_qty_mismatch_detected() -> None:
    """Different qty between broker and platform → MISMATCH."""
    broker = {"2330": _pos(100, 5_000_000)}
    platform = {"2330": _pos(99, 5_000_000)}  # one lot short
    ch = {"2330": _pos(100, 5_000_000)}

    is_match, mismatches = compare_positions(broker, platform, ch, tolerance=_TOLERANCE)

    assert is_match is False
    assert len(mismatches) == 1
    mm = mismatches[0]
    assert mm["symbol"] == "2330"
    assert mm["qty_match"] is False
    assert mm["broker_qty"] == 100
    assert mm["platform_qty"] == 99


def test_pnl_within_tolerance_matches() -> None:
    """PnL diff of 5 NTD (50_000 scaled) → within ±10 NTD tolerance → MATCH."""
    pnl_base = 10_000_000
    pnl_diff = 50_000  # 5 NTD, well within 10 NTD tolerance

    broker = {"2330": _pos(0, pnl_base)}
    platform = {"2330": _pos(0, pnl_base + pnl_diff)}
    ch = {"2330": _pos(0, pnl_base)}

    is_match, mismatches = compare_positions(broker, platform, ch, tolerance=_TOLERANCE)

    assert is_match is True
    assert mismatches == []


def test_pnl_exceeding_tolerance_mismatches() -> None:
    """PnL diff of 15 NTD (150_000 scaled) → exceeds ±10 NTD tolerance → MISMATCH."""
    pnl_base = 10_000_000
    pnl_diff = 150_000  # 15 NTD, just over the 10 NTD tolerance

    broker = {"2330": _pos(0, pnl_base)}
    platform = {"2330": _pos(0, pnl_base + pnl_diff)}
    ch = {"2330": _pos(0, pnl_base)}

    is_match, mismatches = compare_positions(broker, platform, ch, tolerance=_TOLERANCE)

    assert is_match is False
    assert len(mismatches) == 1
    mm = mismatches[0]
    assert mm["pnl_match"] is False
    assert mm["symbol"] == "2330"


def test_empty_positions_match() -> None:
    """No positions on any side → MATCH (flat end-of-day)."""
    is_match, mismatches = compare_positions({}, {}, {}, tolerance=_TOLERANCE)

    assert is_match is True
    assert mismatches == []


def test_all_mismatches_reported() -> None:
    """Two symbols both mismatch → both appear in the mismatches list."""
    broker = {
        "2330": _pos(100, 5_000_000),
        "TXFD6": _pos(2, 200_000_000),
    }
    platform = {
        "2330": _pos(90, 5_000_000),  # qty differs
        "TXFD6": _pos(2, 198_500_000),  # pnl differs by 1,500,000 (150 NTD)
    }
    ch = {
        "2330": _pos(100, 5_000_000),
        "TXFD6": _pos(2, 200_000_000),
    }

    is_match, mismatches = compare_positions(broker, platform, ch, tolerance=_TOLERANCE)

    assert is_match is False
    assert len(mismatches) == 2

    symbols_reported = {mm["symbol"] for mm in mismatches}
    assert symbols_reported == {"2330", "TXFD6"}


def test_symbol_missing_from_broker_is_mismatch() -> None:
    """Symbol present in platform but absent from broker → treated as 0 qty/pnl on broker side."""
    # Platform has an open position, broker has nothing (symbol never reported)
    broker: dict = {}
    platform = {"2330": _pos(50, 1_000_000)}
    ch = {"2330": _pos(50, 1_000_000)}

    is_match, mismatches = compare_positions(broker, platform, ch, tolerance=_TOLERANCE)

    # Qty: 0 (broker) vs 50 (platform) vs 50 (ch) → mismatch
    assert is_match is False
    assert any(mm["symbol"] == "2330" for mm in mismatches)


def test_tolerance_boundary_exact_match() -> None:
    """PnL diff exactly equal to tolerance → MATCH (inclusive boundary)."""
    pnl_base = 5_000_000
    pnl_diff = _TOLERANCE  # exactly at tolerance

    broker = {"2330": _pos(0, pnl_base)}
    platform = {"2330": _pos(0, pnl_base + pnl_diff)}
    ch = {"2330": _pos(0, pnl_base)}

    is_match, mismatches = compare_positions(broker, platform, ch, tolerance=_TOLERANCE)

    assert is_match is True
    assert mismatches == []


def test_tolerance_boundary_one_over_fails() -> None:
    """PnL diff of tolerance + 1 → MISMATCH."""
    pnl_base = 5_000_000
    pnl_diff = _TOLERANCE + 1

    broker = {"2330": _pos(0, pnl_base)}
    platform = {"2330": _pos(0, pnl_base + pnl_diff)}
    ch = {"2330": _pos(0, pnl_base)}

    is_match, mismatches = compare_positions(broker, platform, ch, tolerance=_TOLERANCE)

    assert is_match is False
    assert len(mismatches) == 1


def test_multiple_symbols_partial_mismatch() -> None:
    """Three symbols: two match, one mismatches — only the mismatch is reported."""
    broker = {
        "2330": _pos(10, 1_000_000),
        "2317": _pos(20, 2_000_000),
        "TXFD6": _pos(1, 50_000_000),
    }
    platform = {
        "2330": _pos(10, 1_000_000),  # match
        "2317": _pos(20, 2_000_000),  # match
        "TXFD6": _pos(2, 50_000_000),  # qty mismatch
    }
    ch = {
        "2330": _pos(10, 1_000_000),
        "2317": _pos(20, 2_000_000),
        "TXFD6": _pos(1, 50_000_000),
    }

    is_match, mismatches = compare_positions(broker, platform, ch, tolerance=_TOLERANCE)

    assert is_match is False
    assert len(mismatches) == 1
    assert mismatches[0]["symbol"] == "TXFD6"
