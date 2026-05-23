"""Unit tests for C68 TXF rollover-week back-to-front passive maker.

Patterns per .agent/skills/hft-test-hft/SKILL.md:
  - Scaled-int price assertions (CK scale = 1_000_000; TXFD6 tick = 1 pt)
  - Monotonic time via time.monotonic_ns()
  - Factory fixtures via helper functions

DA T1/T2 critical conditions verified:
  - Spread-gate boundary (sp<12 blocked; sp>=12 admits)
  - Calendar gate: rollover window enforcement (inside / outside / no-gate)
  - R47-minimal: ALL FOUR signal-layer methods ABSENT
  - Max_pos gate (per contracts) at {1, 2, 3}
  - Inventory skew LINEAR in pos (scaled-int; NOT |pos|-gated)
  - Bid/ask execution (no mid pricing)
  - TXF point value (200 NTD/pt)
  - Cost citation: inst RT 1.5 pt (NOT retail 3 pt)
  - Emergency unwind detection
  - AlphaProtocol conformance
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from research.alphas.c68_txf_rollover_back_front_maker.impl import (
    _DISABLED_SIGNAL_LAYERS,
    _ROLLOVER_WINDOW_CANONICAL_DAYS,
    _TXF_INST_RT_COST_PTS,
    _TXF_POINT_VALUE_NTD,
    _TXF_RETAIL_RT_COST_PTS,
    C68Alpha,
    C68Params,
    TxfRolloverBackFrontMaker,
    is_in_rollover_window,
)
from research.backtest.maker_engine import (
    Hold,
    PostQuote,
    TickData,
)
from research.registry.schemas import AlphaProtocol

_SCALE = 1_000_000
# Canonical rollover window for TXFE6 becoming new-front (post-TXFD6 expiry)
_WINDOW_START = date(2026, 4, 13)
_WINDOW_END = date(2026, 4, 15)
_INSIDE_WINDOW = date(2026, 4, 14)
_BEFORE_WINDOW = date(2026, 4, 10)
_AFTER_WINDOW = date(2026, 4, 18)


def _bidask(
    bid_pts: int,
    ask_pts: int,
    bid_qty: int = 10,
    ask_qty: int = 10,
    ts_ns: int | None = None,
) -> TickData:
    return TickData(
        exch_ts=ts_ns if ts_ns is not None else time.monotonic_ns(),
        bid_price=bid_pts * _SCALE,
        ask_price=ask_pts * _SCALE,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        trade_price=0,
        trade_volume=0,
        is_trade=False,
        scale=_SCALE,
    )


def _trade(price_pts: int, volume: int = 1, ts_ns: int | None = None) -> TickData:
    return TickData(
        exch_ts=ts_ns if ts_ns is not None else time.monotonic_ns(),
        bid_price=0,
        ask_price=0,
        bid_qty=0,
        ask_qty=0,
        trade_price=price_pts * _SCALE,
        trade_volume=volume,
        is_trade=True,
        scale=_SCALE,
    )


def _maker_with_window(
    params_override: dict | None = None,
    inside: bool = True,
) -> TxfRolloverBackFrontMaker:
    kw = {
        "rollover_window_start_date": _WINDOW_START,
        "rollover_window_end_date": _WINDOW_END,
    }
    if params_override:
        kw.update(params_override)
    m = TxfRolloverBackFrontMaker(params=C68Params(**kw))
    m.set_session_date(_INSIDE_WINDOW if inside else _BEFORE_WINDOW)
    return m


# ----------------------------------------------------------------------------
# Calendar gate (NEW to C68 vs C33/C63)
# ----------------------------------------------------------------------------


def test_is_in_rollover_window_inside() -> None:
    assert is_in_rollover_window(_INSIDE_WINDOW, _WINDOW_START, _WINDOW_END)


def test_is_in_rollover_window_first_day_inclusive() -> None:
    assert is_in_rollover_window(_WINDOW_START, _WINDOW_START, _WINDOW_END)


def test_is_in_rollover_window_last_day_inclusive() -> None:
    assert is_in_rollover_window(_WINDOW_END, _WINDOW_START, _WINDOW_END)


def test_is_in_rollover_window_before() -> None:
    assert not is_in_rollover_window(_BEFORE_WINDOW, _WINDOW_START, _WINDOW_END)


def test_is_in_rollover_window_after() -> None:
    assert not is_in_rollover_window(_AFTER_WINDOW, _WINDOW_START, _WINDOW_END)


def test_calendar_gate_blocks_quote_outside_window() -> None:
    """Outside rollover window -> Hold, no quote post."""
    m = _maker_with_window(inside=False)
    actions = m.on_tick(_bidask(17500, 17513))  # 13 pt > 12 threshold
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []
    assert m.rollover_gate_blocked == 1
    assert m.quotes_posted == 0


def test_calendar_gate_admits_quote_inside_window() -> None:
    """Inside rollover window -> quotes post normally."""
    m = _maker_with_window(inside=True)
    actions = m.on_tick(_bidask(17500, 17513))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    assert m.rollover_gate_blocked == 0


def test_calendar_gate_open_when_window_unset() -> None:
    """If rollover_window_*_date is None, gate is OPEN (for tests without
    calendar context)."""
    m = TxfRolloverBackFrontMaker()  # no window
    actions = m.on_tick(_bidask(17500, 17513))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    assert m.rollover_gate_blocked == 0


def test_calendar_gate_blocked_counter_advances_on_each_tick() -> None:
    m = _maker_with_window(inside=False)
    # Different prices to defeat price-move gate (though that doesn't even
    # matter here because rollover gate fires first).
    m.on_tick(_bidask(17500, 17513))
    m.on_tick(_bidask(17501, 17514))
    m.on_tick(_bidask(17502, 17515))
    assert m.rollover_gate_blocked == 3
    assert m.quotes_posted == 0


# ----------------------------------------------------------------------------
# Spread-gate boundary (TXFE6 narrow window: 12 pt canonical)
# ----------------------------------------------------------------------------


def test_spread_threshold_default_is_12() -> None:
    """C68 canonical threshold = 12 pt (TXFD6 Feb analog median 12-16 pt)."""
    assert C68Params().spread_threshold_pts == 12


def test_spread_gate_blocks_at_sp5() -> None:
    m = _maker_with_window()
    actions = m.on_tick(_bidask(17500, 17505))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []


def test_spread_gate_blocks_at_sp11() -> None:
    m = _maker_with_window()
    actions = m.on_tick(_bidask(17500, 17511))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []


def test_spread_gate_admits_at_sp12() -> None:
    m = _maker_with_window()
    actions = m.on_tick(_bidask(17500, 17512))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_spread_gate_admits_at_sp16() -> None:
    """sp=16 = top of TXFD6 Feb analog range."""
    m = _maker_with_window()
    actions = m.on_tick(_bidask(17500, 17516))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_spread_gate_admits_at_sp50() -> None:
    m = _maker_with_window()
    actions = m.on_tick(_bidask(17500, 17550))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_spread_gate_counter_advances_on_block() -> None:
    m = _maker_with_window()
    m.on_tick(_bidask(17500, 17510))
    m.on_tick(_bidask(17501, 17510))
    assert m.spread_blocked == 2


def test_spread_gate_fires_after_calendar_gate_passes() -> None:
    """Inside window with narrow spread -> spread_blocked counter advances."""
    m = _maker_with_window(inside=True)
    m.on_tick(_bidask(17500, 17505))  # sp=5, below threshold 12
    assert m.rollover_gate_blocked == 0
    assert m.spread_blocked == 1


def test_calendar_gate_preempts_spread_gate() -> None:
    """Outside window -> rollover gate fires; spread gate never evaluated."""
    m = _maker_with_window(inside=False)
    m.on_tick(_bidask(17500, 17505))
    assert m.rollover_gate_blocked == 1
    assert m.spread_blocked == 0


# ----------------------------------------------------------------------------
# Scaled-int price arithmetic
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bid_pts,ask_pts,expected_bid,expected_ask",
    [
        (17500, 17512, 17_500_000_000, 17_512_000_000),
        (17500, 17514, 17_500_000_000, 17_514_000_000),
        (18000, 18016, 18_000_000_000, 18_016_000_000),
        (19500, 19520, 19_500_000_000, 19_520_000_000),
    ],
)
def test_posts_at_scaled_int_prices(
    bid_pts: int,
    ask_pts: int,
    expected_bid: int,
    expected_ask: int,
) -> None:
    m = _maker_with_window()
    actions = m.on_tick(_bidask(bid_pts, ask_pts))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    prices = {a.side: a.price for a in posts}
    assert prices["buy"] == expected_bid
    assert prices["sell"] == expected_ask
    for a in posts:
        assert isinstance(a.price, int)


# ----------------------------------------------------------------------------
# R47-minimal: ALL four signal layers disabled
# ----------------------------------------------------------------------------


def test_no_signal_layer_methods_exposed() -> None:
    m = _maker_with_window()
    for attr in _DISABLED_SIGNAL_LAYERS:
        assert not hasattr(m, attr), (
            f"R47-minimal violation: {attr} found on strategy"
        )


def test_signal_layer_params_default_to_false() -> None:
    p = C68Params()
    assert p.enable_pe_layer is False
    assert p.enable_queue_layer is False
    assert p.enable_mfg_layer is False
    assert p.enable_qi_layer is False


def test_impl_does_not_import_signal_state_classes() -> None:
    import research.alphas.c68_txf_rollover_back_front_maker.impl as mod
    src = mod.__file__
    assert src is not None
    with open(src) as f:
        source = f.read()
    for sym in ("_PEState", "_QueueState", "_MFGState", "_QIState"):
        assert sym not in source, (
            f"R47-minimal violation: impl references {sym}"
        )


# ----------------------------------------------------------------------------
# Max-pos gate (default 1 per data-constrained sample)
# ----------------------------------------------------------------------------


def test_max_pos_default_is_1_data_constrained() -> None:
    """C68 default mp=1 per 3-day analog data constraint."""
    assert C68Params().max_pos == 1


def test_stops_buying_at_max_pos_1() -> None:
    m = _maker_with_window({"max_pos": 1})
    m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    assert m.position == 1
    actions = m.on_tick(_bidask(17500, 17512))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides
    assert "sell" in sides


def test_stops_selling_at_negative_max_pos_1() -> None:
    m = _maker_with_window({"max_pos": 1})
    m.on_fill("sell", 17_512 * _SCALE, 17506.0)
    assert m.position == -1
    actions = m.on_tick(_bidask(17500, 17512))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "sell" not in sides
    assert "buy" in sides


def test_stops_buying_at_max_pos_2() -> None:
    m = _maker_with_window({"max_pos": 2})
    for _ in range(2):
        m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    assert m.position == 2
    actions = m.on_tick(_bidask(17500, 17512))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides


def test_stops_buying_at_max_pos_3() -> None:
    m = _maker_with_window({"max_pos": 3})
    for _ in range(3):
        m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    assert m.position == 3
    actions = m.on_tick(_bidask(17500, 17512))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides


def test_max_pos_blocked_counter_advances() -> None:
    m = _maker_with_window({"max_pos": 1})
    m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    m.on_tick(_bidask(17500, 17512))
    m.on_tick(_bidask(17501, 17513))
    assert m.max_pos_blocked >= 1


# ----------------------------------------------------------------------------
# Inventory skew (LINEAR in pos; NOT |pos|-gated)
# ----------------------------------------------------------------------------


def test_inventory_skew_none_at_pos_zero() -> None:
    m = _maker_with_window()
    actions = m.on_tick(_bidask(17500, 17512))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 17500 * _SCALE
    assert posts["sell"] == 17512 * _SCALE


def test_inventory_skew_lowers_both_quotes_when_long() -> None:
    m = _maker_with_window({"max_pos": 3, "inventory_skew_tenths": 2})
    m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    actions = m.on_tick(_bidask(17500, 17512))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    # skew = 1 * 2 * scale // 10 = 200_000
    assert posts["buy"] == 17500 * _SCALE - 200_000
    assert posts["sell"] == 17512 * _SCALE - 200_000


def test_inventory_skew_raises_both_quotes_when_short() -> None:
    m = _maker_with_window({"max_pos": 3, "inventory_skew_tenths": 2})
    m.on_fill("sell", 17_512 * _SCALE, 17506.0)
    actions = m.on_tick(_bidask(17500, 17512))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 17500 * _SCALE + 200_000
    assert posts["sell"] == 17512 * _SCALE + 200_000


def test_inventory_skew_scales_linearly_with_pos() -> None:
    m = _maker_with_window({"max_pos": 5, "inventory_skew_tenths": 2})
    for _ in range(3):
        m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    actions = m.on_tick(_bidask(17500, 17520))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 17500 * _SCALE - 600_000  # 3x pos=1 skew
    assert posts["sell"] == 17520 * _SCALE - 600_000


# ----------------------------------------------------------------------------
# Price-movement gate (ROD anti-stack)
# ----------------------------------------------------------------------------


def test_does_not_restack_same_price() -> None:
    m = _maker_with_window()
    first = m.on_tick(_bidask(17500, 17512))
    second = m.on_tick(_bidask(17500, 17512))
    assert len([a for a in first if isinstance(a, PostQuote)]) == 2
    assert second == [Hold()] or all(
        not isinstance(a, PostQuote) for a in second
    )


def test_reposts_when_price_moves() -> None:
    m = _maker_with_window()
    m.on_tick(_bidask(17500, 17512))
    second = m.on_tick(_bidask(17501, 17513))
    assert len([a for a in second if isinstance(a, PostQuote)]) == 2


# ----------------------------------------------------------------------------
# Bid/ask execution (no mid)
# ----------------------------------------------------------------------------


def test_posts_at_best_bid_not_mid() -> None:
    m = _maker_with_window()
    actions = m.on_tick(_bidask(17500, 17512))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 17500 * _SCALE
    assert posts["buy"] != 17_506_000_000  # mid


def test_posts_at_best_ask_not_mid() -> None:
    m = _maker_with_window()
    actions = m.on_tick(_bidask(17500, 17512))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["sell"] == 17512 * _SCALE


# ----------------------------------------------------------------------------
# TXF point value & inst RT citation
# ----------------------------------------------------------------------------


def test_txf_point_value_is_200_ntd() -> None:
    assert _TXF_POINT_VALUE_NTD == 200


def test_txf_inst_rt_cost_is_1_5pt() -> None:
    assert _TXF_INST_RT_COST_PTS == 1.5


def test_txf_retail_rt_cost_is_3pt() -> None:
    assert _TXF_RETAIL_RT_COST_PTS == 3.0


def test_rollover_window_canonical_3_days() -> None:
    assert _ROLLOVER_WINDOW_CANONICAL_DAYS == 3


def test_c68_structural_viability_at_inst_rt() -> None:
    """Narrow-window edge: 12 pt gross - 1.5 pt per-cycle RT > 0."""
    p = C68Params()
    # Edge at threshold: 2 x half_spread = threshold_pts.
    # Hedge framing REJECTED per T1: both legs passive => only ONE RT per
    # cycle (1.5 pt), not two (3 pt). See module docstring.
    gross_at_threshold = float(p.spread_threshold_pts)
    per_cycle_rt = _TXF_INST_RT_COST_PTS
    margin = gross_at_threshold - per_cycle_rt
    assert margin >= 10.0  # 12 - 1.5 = 10.5 viable


# ----------------------------------------------------------------------------
# Emergency unwind detection
# ----------------------------------------------------------------------------


def test_emergency_unwind_false_when_flat() -> None:
    m = _maker_with_window()
    assert m.emergency_unwind_required() is False


def test_emergency_unwind_false_inside_window_with_position() -> None:
    m = _maker_with_window()
    m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    assert m.position == 1
    assert m.emergency_unwind_required() is False


def test_emergency_unwind_true_after_window_with_position() -> None:
    """Window closes (session_date > window_end) with |pos|>0 -> unwind required."""
    m = _maker_with_window()
    m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    m.set_session_date(_AFTER_WINDOW)
    assert m.emergency_unwind_required() is True


def test_emergency_unwind_false_after_window_when_flat() -> None:
    m = _maker_with_window()
    m.set_session_date(_AFTER_WINDOW)
    assert m.position == 0
    assert m.emergency_unwind_required() is False


def test_emergency_unwind_false_when_no_window_configured() -> None:
    m = TxfRolloverBackFrontMaker()
    m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    assert m.emergency_unwind_required() is False


# ----------------------------------------------------------------------------
# Monotonic time ordering
# ----------------------------------------------------------------------------


def test_monotonic_timestamp_ordering_preserved() -> None:
    m = _maker_with_window()
    t0 = time.monotonic_ns()
    m.on_tick(_bidask(17500, 17512, ts_ns=t0))
    t1 = time.monotonic_ns()
    m.on_tick(_bidask(17501, 17513, ts_ns=t1))
    assert t1 > t0
    EPOCH_THRESHOLD_NS = 100_000_000_000_000_000
    assert t0 < EPOCH_THRESHOLD_NS
    assert t1 < EPOCH_THRESHOLD_NS


# ----------------------------------------------------------------------------
# Degenerate books
# ----------------------------------------------------------------------------


def test_trade_event_returns_hold() -> None:
    m = _maker_with_window()
    actions = m.on_tick(_trade(17506, volume=3))
    assert actions == [Hold()]


def test_rejects_crossed_book() -> None:
    m = _maker_with_window()
    actions = m.on_tick(_bidask(17512, 17500))  # crossed
    assert [a for a in actions if isinstance(a, PostQuote)] == []


def test_rejects_zero_priced_book() -> None:
    m = _maker_with_window()
    bad = TickData(
        exch_ts=1,
        bid_price=0,
        ask_price=17_512 * _SCALE,
        bid_qty=1,
        ask_qty=1,
        trade_price=0,
        trade_volume=0,
        is_trade=False,
        scale=_SCALE,
    )
    actions = m.on_tick(bad)
    assert [a for a in actions if isinstance(a, PostQuote)] == []


# ----------------------------------------------------------------------------
# Gap / reset
# ----------------------------------------------------------------------------


def test_on_gap_clears_transient_quote_state() -> None:
    m = _maker_with_window()
    m.on_tick(_bidask(17500, 17512))
    m.on_gap()
    actions = m.on_tick(_bidask(17500, 17512))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_on_gap_preserves_position() -> None:
    m = _maker_with_window()
    m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    assert m.position == 1
    m.on_gap()
    assert m.position == 1


def test_on_gap_preserves_session_date() -> None:
    m = _maker_with_window()
    m.on_gap()
    assert m.current_session_date == _INSIDE_WINDOW


def test_reset_clears_position_quotes_and_session() -> None:
    m = _maker_with_window({"max_pos": 3})
    m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    m.on_tick(_bidask(17500, 17512))
    assert m.position == 1
    assert m.quotes_posted == 2
    m.reset()
    assert m.position == 0
    assert m.quotes_posted == 0
    assert m.current_session_date is None


# ----------------------------------------------------------------------------
# AlphaProtocol conformance
# ----------------------------------------------------------------------------


def test_c68_alpha_conforms_to_alpha_protocol() -> None:
    alpha = C68Alpha()
    assert isinstance(alpha, AlphaProtocol)
    assert alpha.manifest.alpha_id == "c68_txf_rollover_back_front_maker"
    assert alpha.manifest.strategy_type == "maker"
    sig = alpha.update(foo=1)
    assert isinstance(sig, float)
    assert alpha.reset() is None


def test_c68_manifest_declares_latency_profile() -> None:
    alpha = C68Alpha()
    assert alpha.manifest.latency_profile


def test_c68_manifest_documents_instrument_txfe6() -> None:
    alpha = C68Alpha()
    assert alpha.manifest.instrument == "TXFE6"


def test_c68_hypothesis_cites_rollover_window() -> None:
    alpha = C68Alpha()
    h = alpha.manifest.hypothesis.lower()
    assert "rollover" in h or "transition" in h


def test_c68_hypothesis_cites_inst_rt() -> None:
    alpha = C68Alpha()
    h = alpha.manifest.hypothesis
    assert "1.5" in h or "3 pt" in h  # inst RT appears


def test_c68_hypothesis_rejects_hedge_pair_framing() -> None:
    """Must document rejection of task-brief hedge-pair framing."""
    alpha = C68Alpha()
    h = alpha.manifest.hypothesis.lower()
    assert "reject" in h  # "REJECTED: TAKE hedge leg inverts edge"


def test_c68_reset_clears_position() -> None:
    alpha = C68Alpha()
    alpha.maker.on_fill("buy", 17_500 * _SCALE, 17506.0)
    assert alpha.maker.position == 1
    alpha.reset()
    assert alpha.maker.position == 0


# ----------------------------------------------------------------------------
# Cycle / position accounting
# ----------------------------------------------------------------------------


def test_position_decreases_on_sell_fill() -> None:
    m = _maker_with_window({"max_pos": 3})
    m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    assert m.position == 2
    m.on_fill("sell", 17_520 * _SCALE, 17512.0)
    assert m.position == 1
    m.on_fill("sell", 17_520 * _SCALE, 17512.0)
    assert m.position == 0


def test_position_resets_last_quote_on_fill() -> None:
    m = _maker_with_window({"max_pos": 3})
    first = m.on_tick(_bidask(17500, 17512))
    assert len([a for a in first if isinstance(a, PostQuote)]) == 2
    m.on_fill("buy", 17_500 * _SCALE, 17506.0)
    second = m.on_tick(_bidask(17500, 17512))
    buy_posts = [
        a for a in second if isinstance(a, PostQuote) and a.side == "buy"
    ]
    assert len(buy_posts) == 1


# ----------------------------------------------------------------------------
# Cross-candidate distinctions
# ----------------------------------------------------------------------------


def test_c68_default_threshold_higher_than_c63_and_c33() -> None:
    """C68 = 12 (narrow rollover window). C33 = 5. C63 = 3."""
    assert C68Params().spread_threshold_pts > 5
    assert C68Params().spread_threshold_pts > 3


def test_c68_is_txf_family_200_ntd_not_10_tmf() -> None:
    """TXF family => 200 NTD/pt (not 10 like TMF)."""
    assert _TXF_POINT_VALUE_NTD == 200


def test_c68_instrument_is_txfe6_back_month_not_txfd6() -> None:
    """C68 targets back-month TXFE6, NOT the current front TXFD6 (C33)."""
    alpha = C68Alpha()
    assert alpha.manifest.instrument == "TXFE6"
