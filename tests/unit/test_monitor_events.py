"""Tests for event detection, opportunity scoring, and dominant-alpha labelling."""

from __future__ import annotations

import pytest

pytest.importorskip("rich")

from hft_platform.monitor._events import (
    compute_opportunity_score,
    detect_events,
    dominant_alpha_label,
    format_event_label,
    snapshot_prev,
)
from hft_platform.monitor._types import AlphaState, EventFlag, SymbolState, WatchlistSymbol


def _ss(composite: float = 0.0, spread_bps: float = 10.0) -> SymbolState:
    return SymbolState(
        symbol=WatchlistSymbol(
            code="2330",
            name="台積電",
            product_type="stock",
            alpha_ids=("alpha_1", "alpha_2"),
        ),
        tick_count=64,
        composite=composite,
        spread_bps=spread_bps,
    )


def test_snapshot_prev_copies_current_values() -> None:
    ss = _ss(composite=1.5, spread_bps=20.0)
    ss.alpha_states = {
        "alpha_1": AlphaState(alpha_id="alpha_1", signal=0.5),
        "alpha_2": AlphaState(alpha_id="alpha_2", signal=0.3),
    }
    snapshot_prev(ss)
    assert ss.prev_composite == 1.5
    assert ss.prev_spread_bps == 20.0
    assert ss.prev_agree_direction == 1  # both positive


def test_detect_composite_cross() -> None:
    ss = _ss(composite=-0.5)
    ss.prev_composite = 0.8
    detect_events(ss, now_ns=100)
    assert ss.event_flags & EventFlag.COMPOSITE_CROSS


def test_detect_sigma_break_up() -> None:
    ss = _ss(composite=1.5)
    ss.prev_composite = 0.8
    detect_events(ss, now_ns=200)
    assert ss.event_flags & EventFlag.SIGMA_BREAK_UP


def test_detect_sigma_break_down() -> None:
    ss = _ss(composite=0.5)
    ss.prev_composite = 1.5
    detect_events(ss, now_ns=200)
    assert ss.event_flags & EventFlag.SIGMA_BREAK_DOWN


def test_detect_agree_flip() -> None:
    ss = _ss(composite=-0.5)
    ss.prev_agree_direction = 1
    ss.alpha_states = {
        "alpha_1": AlphaState(alpha_id="alpha_1", signal=-0.5),
        "alpha_2": AlphaState(alpha_id="alpha_2", signal=-0.3),
    }
    detect_events(ss, now_ns=300)
    assert ss.event_flags & EventFlag.AGREE_FLIP


def test_detect_spread_converge_and_widen() -> None:
    ss = _ss(spread_bps=5.0)
    ss.prev_spread_bps = 20.0
    detect_events(ss, now_ns=400)
    assert ss.event_flags & EventFlag.SPREAD_CONVERGE

    ss2 = _ss(spread_bps=40.0)
    ss2.prev_spread_bps = 20.0
    detect_events(ss2, now_ns=400)
    assert ss2.event_flags & EventFlag.SPREAD_WIDEN


def test_detect_stale_enter_and_resolve() -> None:
    ss = _ss()
    ss.is_stale = True
    ss.prev_is_stale = False
    detect_events(ss, now_ns=500)
    assert ss.event_flags & EventFlag.STALE_ENTER

    ss2 = _ss()
    ss2.is_stale = False
    ss2.prev_is_stale = True
    detect_events(ss2, now_ns=600)
    assert ss2.event_flags & EventFlag.STALE_RESOLVE


def test_dominant_alpha_label_returns_top2() -> None:
    ss = _ss(composite=2.0)
    ss.alpha_states = {
        "alpha_1": AlphaState(alpha_id="alpha_1", signal=0.5, z_score=2.1),
        "alpha_2": AlphaState(alpha_id="alpha_2", signal=0.3, z_score=1.4),
    }
    label = dominant_alpha_label(ss)
    assert "+" in label  # top 2 alphas joined


def test_dominant_alpha_label_returns_all_when_all_aligned() -> None:
    ss = SymbolState(
        symbol=WatchlistSymbol(
            code="2330",
            name="台積電",
            product_type="stock",
            alpha_ids=("alpha_1", "alpha_2", "alpha_3"),
        ),
        tick_count=64,
        composite=2.0,
    )
    ss.alpha_states = {
        "alpha_1": AlphaState(alpha_id="alpha_1", signal=0.5, z_score=2.0),
        "alpha_2": AlphaState(alpha_id="alpha_2", signal=0.3, z_score=1.5),
        "alpha_3": AlphaState(alpha_id="alpha_3", signal=0.2, z_score=1.0),
    }
    assert dominant_alpha_label(ss) == "all"


def test_opportunity_score_closed_and_stale() -> None:
    ss = _ss(composite=2.0)
    ss.is_closed = True
    assert compute_opportunity_score(ss, warmup_ticks=64) == -1000.0

    ss2 = _ss(composite=2.0)
    ss2.is_stale = True
    assert compute_opportunity_score(ss2, warmup_ticks=64) == -500.0

    ss3 = _ss(composite=2.0)
    ss3.tick_count = 10
    assert compute_opportunity_score(ss3, warmup_ticks=64) == -100.0


def test_opportunity_score_positive_for_live_symbols() -> None:
    ss = _ss(composite=2.5, spread_bps=10.0)
    ss.alpha_states = {
        "alpha_1": AlphaState(alpha_id="alpha_1", signal=0.5),
        "alpha_2": AlphaState(alpha_id="alpha_2", signal=0.3),
    }
    score = compute_opportunity_score(ss, warmup_ticks=64)
    assert score > 0


def test_snapshot_prev_saves_prev_poll_price() -> None:
    """S2: snapshot_prev copies last_price to prev_poll_price."""
    ss = _ss(composite=1.0)
    ss.last_price = 210.5
    snapshot_prev(ss)
    assert ss.prev_poll_price == 210.5


def test_format_event_label() -> None:
    ss = _ss(composite=-1.5)
    assert "crossed 0" in format_event_label(EventFlag.COMPOSITE_CROSS, ss)
    assert "broke" in format_event_label(EventFlag.SIGMA_BREAK_UP, ss)
    assert "agree flip" in format_event_label(EventFlag.AGREE_FLIP, ss)
