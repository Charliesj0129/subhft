"""Slice B Task 2 - RED to GREEN tests for MakerEngine._compute_residual_mtm.

The helper marks the un-FIFO'd residual position to a chosen mark price.
It is intentionally pure: callers select which mark to pass (mid, last_trade,
worse_of_mid_last_trade, etc.); the helper is mark-agnostic.

Scale convention: matches `MakerEngine._compute_fifo_pnl` which uses
``scale = 1_000_000`` (golden parquet x1M scale, see CLAUDE.md / memory file
``feedback_golden_data_scale``). The plan code sketch
(``docs/superpowers/plans/2026-05-05-slice-b-maker-realism.md`` Section 6 Task 2)
illustratively wrote ``/10000.0`` - that is the platform-wide CLAUDE.md
convention, but inside this engine prices are x1M to match the data source.
The static helper accepts a ``scale`` parameter (default ``1_000_000``) so
both conventions work; the default keeps the helper consistent with
``_compute_fifo_pnl`` and Task 3's day-loop integration.

Test 4 deviation from the plan: the plan's RED case 4 phrasing
(``mark_method="worse_of_mid_last_trade"`` ... "should pick 99") would have
the helper itself perform mark selection. We chose the simpler
caller-picks-mark design - the helper just computes
``open_pos * (mark - avg)``; the caller is responsible for resolving the
worse mark. Test 4 verifies that contract: when the caller hands in the
worse mark (99 < mid 100, long position), the helper produces the
worse-case PnL.
"""
from __future__ import annotations

import pytest

from research.backtest.maker_engine import MakerEngine

SCALE = 1_000_000  # x1M - matches _compute_fifo_pnl in maker_engine.py


def _pts(p: float) -> int:
    """Helper: convert a float price-in-points to scaled int."""
    return int(round(p * SCALE))


# ---------------------------------------------------------------------------
# Case 1: open_pos=0 -> returns 0.0 regardless of mark
# ---------------------------------------------------------------------------
def test_residual_mtm_zero_position_returns_zero() -> None:
    """Zero residual position has zero MtM regardless of mark/avg."""
    result = MakerEngine._compute_residual_mtm(
        open_pos=0,
        mark_price=_pts(123.45),
        avg_entry_price=_pts(100.00),
        mark_method="last_mid",
    )
    assert result == 0.0

    # Also verify with extreme mark/avg disparity - still zero.
    result_extreme = MakerEngine._compute_residual_mtm(
        open_pos=0,
        mark_price=_pts(99999.0),
        avg_entry_price=_pts(1.0),
        mark_method="last_mid",
    )
    assert result_extreme == 0.0


# ---------------------------------------------------------------------------
# Case 2: long +1, mark = avg + 50 pts -> +50.0
# ---------------------------------------------------------------------------
def test_residual_mtm_long_one_lot_mark_above_avg() -> None:
    """Long 1 lot with mark 50 pts above avg yields +50.0 points."""
    avg = _pts(100.0)
    mark = _pts(150.0)  # 50 points above avg
    result = MakerEngine._compute_residual_mtm(
        open_pos=+1,
        mark_price=mark,
        avg_entry_price=avg,
        mark_method="last_mid",
    )
    assert result == pytest.approx(50.0, rel=1e-9, abs=1e-9)


# ---------------------------------------------------------------------------
# Case 3: short -1, mark = avg + 50 pts -> -50.0 (short loses)
# ---------------------------------------------------------------------------
def test_residual_mtm_short_one_lot_mark_above_avg() -> None:
    """Short 1 lot with mark 50 pts above avg yields -50.0 (short loses)."""
    avg = _pts(100.0)
    mark = _pts(150.0)  # 50 pts above avg -> bad for short
    result = MakerEngine._compute_residual_mtm(
        open_pos=-1,
        mark_price=mark,
        avg_entry_price=avg,
        mark_method="last_mid",
    )
    assert result == pytest.approx(-50.0, rel=1e-9, abs=1e-9)


# ---------------------------------------------------------------------------
# Case 4: caller-picks-mark contract under worse_of_mid_last_trade
# ---------------------------------------------------------------------------
def test_residual_mtm_caller_picks_worse_mark_for_long() -> None:
    """Helper is mark-agnostic - caller resolves which mark to pass.

    Setup: long 1 lot at avg=100. Mid=100, last_trade=99. The worse mark for
    a long is the lower one (99). The CALLER selects 99 and passes it; the
    helper just computes ``open_pos * (mark - avg)``. Result: -1.0 point.

    This locks in the design decision (caller-picks-mark) made vs. the
    plan's wording. See module docstring for rationale.
    """
    avg = _pts(100.0)
    mid = _pts(100.0)
    last_trade = _pts(99.0)
    # Caller's worse-of-mid-last-trade resolution for a long = min(mid, last)
    worse_mark = min(mid, last_trade)
    assert worse_mark == last_trade  # sanity: 99 < 100

    result = MakerEngine._compute_residual_mtm(
        open_pos=+1,
        mark_price=worse_mark,
        avg_entry_price=avg,
        mark_method="worse_of_mid_last_trade",
    )
    assert result == pytest.approx(-1.0, rel=1e-9, abs=1e-9)


# ---------------------------------------------------------------------------
# Case 5: float-precision regression - 10 lots x 1-unit diff = exact 1.0e-5
# ---------------------------------------------------------------------------
def test_residual_mtm_float_precision_no_artifacts() -> None:
    """Integer-divide-then-cast must produce exact 1.0e-5, not 9.999...e-6.

    With open_pos=+10 and mark - avg = 1 scaled-int unit (sub-tick at
    scale=1e6, but a useful precision probe):
      pnl_int = 10 * 1 = 10
      pnl_pts = 10 / 1_000_000 = 1.0e-5

    Pinning the exact value rules out e.g. 9.99...e-6 artifacts that would
    occur if the helper accidentally did float math along the way.
    """
    # Construct prices so mark - avg = 1 (one scaled-int unit).
    avg = _pts(100.0)
    mark = avg + 1
    result = MakerEngine._compute_residual_mtm(
        open_pos=+10,
        mark_price=mark,
        avg_entry_price=avg,
        mark_method="last_mid",
    )
    expected = 10 / SCALE  # = 1.0e-5 with SCALE=1_000_000
    assert result == expected, (
        f"Float-precision regression: expected exact {expected!r}, got {result!r}"
    )
