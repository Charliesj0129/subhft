"""Tests for opportunity scoring and sort ordering (Phase 2)."""

from __future__ import annotations

import pytest

pytest.importorskip("rich")

from hft_platform.monitor._events import compute_opportunity_score
from hft_platform.monitor._types import AlphaState, SymbolState, WatchlistSymbol


def _ss(code: str, composite: float, spread_bps: float = 10.0, is_closed: bool = False) -> SymbolState:
    ss = SymbolState(
        symbol=WatchlistSymbol(code=code, name=code, product_type="stock", alpha_ids=("qi",)),
        tick_count=64,
        composite=composite,
        spread_bps=spread_bps,
    )
    ss.is_closed = is_closed
    ss.alpha_states = {"qi": AlphaState(alpha_id="qi", signal=composite)}
    return ss


def test_higher_composite_yields_higher_score() -> None:
    low = _ss("A", 0.5)
    high = _ss("B", 2.5)
    assert compute_opportunity_score(high, 64) > compute_opportunity_score(low, 64)


def test_closed_symbols_score_below_live() -> None:
    live = _ss("A", 0.5)
    closed = _ss("B", 3.0, is_closed=True)
    assert compute_opportunity_score(live, 64) > compute_opportunity_score(closed, 64)


def test_sort_order_matches_opportunity_score() -> None:
    states = [
        _ss("C", 0.5),
        _ss("A", 3.0),
        _ss("B", 1.5),
    ]
    for s in states:
        s.opportunity_score = compute_opportunity_score(s, 64)
    sorted_states = sorted(states, key=lambda s: s.opportunity_score, reverse=True)
    codes = [s.symbol.code for s in sorted_states]
    assert codes == ["A", "B", "C"]


def test_high_spread_reduces_score() -> None:
    normal = _ss("A", 2.0, spread_bps=5.0)
    wide = _ss("B", 2.0, spread_bps=80.0)
    assert compute_opportunity_score(normal, 64) > compute_opportunity_score(wide, 64)
