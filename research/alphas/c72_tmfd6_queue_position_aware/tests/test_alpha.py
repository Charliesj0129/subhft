"""Unit tests for C72 TMFD6 queue-position-aware maker.

Patterns per .agent/skills/hft-test-hft/SKILL.md:
  - Scaled-int price assertions (CK scale = 1_000_000)
  - Monotonic time via time.monotonic_ns()

DA T1/T2 critical conditions verified:
  - Spread-gate boundary at 5 pt (TMFD6 C60 baseline)
  - R47-minimal: D1/D2/D3 signal-layer methods ABSENT
  - D4 QI skew preserved (inherited from C60)
  - NEW L0 queue-depth gate: per-side independence, boundary, disable path
  - Gate disabled -> behavior exactly matches C60
  - Linear inventory skew (not |pos|-gated)
  - max_pos {1, 2, 3}
  - Bid/ask execution
  - AlphaProtocol conformance
  - Dominance-check placeholder (counter-based)
"""

from __future__ import annotations

import time

import pytest

from research.alphas.c72_tmfd6_queue_position_aware.impl import (
    _DISABLED_SIGNAL_LAYERS_MOST,
    _TMF_INST_RT_COST_PTS,
    _TMF_POINT_VALUE_NTD,
    _TMF_RETAIL_RT_COST_PTS,
    C72Alpha,
    C72Params,
    TmfD6QueuePositionAwareMaker,
)
from research.backtest.maker_engine import (
    Hold,
    PostQuote,
    TickData,
)
from research.registry.schemas import AlphaProtocol

_SCALE = 1_000_000


def _bidask(
    bid_pts: int,
    ask_pts: int,
    bid_qty: int = 3,       # default THIN (admits default gate bid_qty<=5)
    ask_qty: int = 3,       # default THIN
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


# ----------------------------------------------------------------------------
# C60 baseline inheritance (spread gate, D4 QI, max_pos default)
# ----------------------------------------------------------------------------


def test_spread_threshold_default_matches_c60() -> None:
    assert C72Params().spread_threshold_pts == 5


def test_max_pos_default_matches_c60() -> None:
    assert C72Params().max_pos == 2


def test_inventory_skew_tenths_default_matches_c60() -> None:
    assert C72Params().inventory_skew_tenths == 2


def test_qi_skew_threshold_default_matches_c60() -> None:
    assert C72Params().qi_skew_threshold == 0.10


def test_qi_skew_widen_ticks_default_matches_c60() -> None:
    assert C72Params().qi_skew_widen_ticks == 1


def test_qi_enabled_by_default_matches_c60() -> None:
    assert C72Params().enable_qi_layer is True


def test_d1_d2_d3_disabled_by_default_matches_c60() -> None:
    p = C72Params()
    assert p.enable_pe_layer is False
    assert p.enable_queue_layer is False
    assert p.enable_mfg_layer is False


# ----------------------------------------------------------------------------
# NEW C72-specific params
# ----------------------------------------------------------------------------


def test_queue_depth_gate_enabled_by_default() -> None:
    assert C72Params().enable_queue_depth_gate is True


def test_queue_depth_max_bid_default_is_5() -> None:
    assert C72Params().queue_depth_max_bid == 5


def test_queue_depth_max_ask_default_is_5() -> None:
    assert C72Params().queue_depth_max_ask == 5


# ----------------------------------------------------------------------------
# Spread gate boundary
# ----------------------------------------------------------------------------


def test_spread_gate_blocks_below_threshold_at_sp4() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22504))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []
    assert maker.spread_blocked == 1


def test_spread_gate_admits_at_sp5() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False, enable_queue_depth_gate=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


# ----------------------------------------------------------------------------
# Queue-depth gate boundary (NEW)
# ----------------------------------------------------------------------------


def test_queue_depth_gate_admits_thin_bid_and_ask() -> None:
    """bid_qty=3 <=5, ask_qty=3 <=5 -> both gates open, both quotes post."""
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=3, ask_qty=3))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    assert maker.queue_depth_blocked_bid == 0
    assert maker.queue_depth_blocked_ask == 0


def test_queue_depth_gate_blocks_thick_bid() -> None:
    """bid_qty=10 >5 -> buy-gate closed, only sell posts."""
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=10, ask_qty=3))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides
    assert "sell" in sides
    assert maker.queue_depth_blocked_bid == 1
    assert maker.queue_depth_blocked_ask == 0


def test_queue_depth_gate_blocks_thick_ask() -> None:
    """ask_qty=10 >5 -> sell-gate closed, only buy posts."""
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=3, ask_qty=10))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" in sides
    assert "sell" not in sides
    assert maker.queue_depth_blocked_bid == 0
    assert maker.queue_depth_blocked_ask == 1


def test_queue_depth_gate_blocks_both_thick() -> None:
    """Both sides thick -> both gates closed, no quotes."""
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=10, ask_qty=10))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []
    assert maker.queue_depth_blocked_bid == 1
    assert maker.queue_depth_blocked_ask == 1


def test_queue_depth_gate_exact_threshold_is_admitted() -> None:
    """bid_qty=5 (== threshold) -> admitted (<=, not <)."""
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=5, ask_qty=5))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    assert maker.queue_depth_blocked_bid == 0
    assert maker.queue_depth_blocked_ask == 0


def test_queue_depth_gate_just_above_threshold_is_blocked() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=6, ask_qty=3))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides
    assert maker.queue_depth_blocked_bid == 1


def test_queue_depth_gate_thresholds_independent_per_side() -> None:
    """Set different thresholds for bid vs ask."""
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(
            enable_qi_layer=False,
            queue_depth_max_bid=2,
            queue_depth_max_ask=100,
        )
    )
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=5, ask_qty=50))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides  # bid_qty=5 >2 -> blocked
    assert "sell" in sides     # ask_qty=50 <=100 -> admitted


def test_queue_depth_gate_disabled_behaves_like_c60() -> None:
    """When gate disabled, strategy ignores queue depth entirely."""
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(
            enable_qi_layer=False,
            enable_queue_depth_gate=False,
        )
    )
    # Thick queue -> with gate OFF, both sides should still post.
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=9999, ask_qty=9999))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    assert maker.queue_depth_blocked_bid == 0
    assert maker.queue_depth_blocked_ask == 0


def test_queue_depth_gate_blocked_counter_accumulates() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    # 3 thick-bid ticks at distinct prices
    maker.on_tick(_bidask(22500, 22505, bid_qty=10))
    maker.on_tick(_bidask(22501, 22506, bid_qty=10))
    maker.on_tick(_bidask(22502, 22507, bid_qty=10))
    assert maker.queue_depth_blocked_bid == 3


# ----------------------------------------------------------------------------
# R47-minimal: D1/D2/D3 NOT exposed
# ----------------------------------------------------------------------------


def test_no_d1_d2_d3_signal_layer_methods_exposed() -> None:
    maker = TmfD6QueuePositionAwareMaker()
    for attr in _DISABLED_SIGNAL_LAYERS_MOST:
        assert not hasattr(maker, attr), (
            f"R47-minimal violation: {attr} on C72"
        )


def test_d4_qi_compute_method_present() -> None:
    """C72 inherits D4 QI layer from C60 — must still be active."""
    maker = TmfD6QueuePositionAwareMaker()
    assert hasattr(maker, "_compute_qi_skew")


def test_c72_new_queue_depth_gate_method_present() -> None:
    maker = TmfD6QueuePositionAwareMaker()
    assert hasattr(maker, "_queue_depth_gate_check")


# ----------------------------------------------------------------------------
# Max-pos gate (inherited from C60)
# ----------------------------------------------------------------------------


def test_stops_buying_at_max_pos_1() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(max_pos=1, enable_qi_layer=False)
    )
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert maker.position == 1
    actions = maker.on_tick(_bidask(22500, 22505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides
    assert "sell" in sides


def test_stops_buying_at_max_pos_2() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(max_pos=2, enable_qi_layer=False)
    )
    for _ in range(2):
        maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert maker.position == 2
    actions = maker.on_tick(_bidask(22500, 22505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides


def test_stops_buying_at_max_pos_3() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(max_pos=3, enable_qi_layer=False)
    )
    for _ in range(3):
        maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert maker.position == 3
    actions = maker.on_tick(_bidask(22500, 22505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides


def test_stops_selling_at_negative_max_pos() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(max_pos=1, enable_qi_layer=False)
    )
    maker.on_fill("sell", 22_505 * _SCALE, 22502.5)
    assert maker.position == -1
    actions = maker.on_tick(_bidask(22500, 22505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "sell" not in sides


def test_max_pos_blocked_counter_advances() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(max_pos=1, enable_qi_layer=False)
    )
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    maker.on_tick(_bidask(22500, 22505))
    maker.on_tick(_bidask(22501, 22506))
    assert maker.max_pos_blocked >= 1


# ----------------------------------------------------------------------------
# Linear inventory skew (NOT |pos|-gated)
# ----------------------------------------------------------------------------


def test_inventory_skew_none_at_pos_zero() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 22500 * _SCALE
    assert posts["sell"] == 22505 * _SCALE


def test_inventory_skew_long() -> None:
    params = C72Params(max_pos=3, inventory_skew_tenths=2, enable_qi_layer=False)
    maker = TmfD6QueuePositionAwareMaker(params=params)
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 22500 * _SCALE - 200_000
    assert posts["sell"] == 22505 * _SCALE - 200_000


def test_inventory_skew_short() -> None:
    params = C72Params(max_pos=3, inventory_skew_tenths=2, enable_qi_layer=False)
    maker = TmfD6QueuePositionAwareMaker(params=params)
    maker.on_fill("sell", 22_505 * _SCALE, 22502.5)
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 22500 * _SCALE + 200_000
    assert posts["sell"] == 22505 * _SCALE + 200_000


# ----------------------------------------------------------------------------
# D4 QI skew (inherited from C60)
# ----------------------------------------------------------------------------


def test_qi_skew_widens_ask_when_bid_heavy() -> None:
    """D4 QI active per C60; NOT |pos|-gated."""
    params = C72Params(enable_qi_layer=True)
    maker = TmfD6QueuePositionAwareMaker(params=params)
    # Both sides thin so queue-depth gate admits; imbalance 0.8 triggers QI.
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=3, ask_qty=1))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    # QI imbalance = (3-1)/4 = 0.5 > 0.10 -> ask widens up 1 tick
    assert posts["sell"] == 22505 * _SCALE + _SCALE
    assert maker.qi_widen_events == 1


def test_qi_and_queue_depth_gate_interact_correctly() -> None:
    """Queue-depth gate and QI skew are ORTHOGONAL — both can fire together."""
    params = C72Params(enable_qi_layer=True)
    maker = TmfD6QueuePositionAwareMaker(params=params)
    # Ask side THIN (<=5), bid side same. Imbalance strong -> QI triggers.
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=4, ask_qty=1))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    assert maker.qi_widen_events == 1
    assert maker.queue_depth_blocked_bid == 0
    assert maker.queue_depth_blocked_ask == 0


# ----------------------------------------------------------------------------
# Price-movement gate (ROD anti-stack)
# ----------------------------------------------------------------------------


def test_does_not_restack_same_price() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    first = maker.on_tick(_bidask(22500, 22505))
    second = maker.on_tick(_bidask(22500, 22505))
    assert len([a for a in first if isinstance(a, PostQuote)]) == 2
    assert all(not isinstance(a, PostQuote) for a in second)


def test_reposts_when_price_moves() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    maker.on_tick(_bidask(22500, 22505))
    second = maker.on_tick(_bidask(22501, 22506))
    assert len([a for a in second if isinstance(a, PostQuote)]) == 2


# ----------------------------------------------------------------------------
# Bid/ask execution (no mid)
# ----------------------------------------------------------------------------


def test_posts_at_best_bid_not_mid() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 22500 * _SCALE
    assert posts["buy"] != 22_502_500_000  # mid


def test_posts_at_best_ask_not_mid() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["sell"] == 22505 * _SCALE


# ----------------------------------------------------------------------------
# Cost citation / instrument
# ----------------------------------------------------------------------------


def test_tmf_point_value_is_10_ntd() -> None:
    assert _TMF_POINT_VALUE_NTD == 10


def test_tmf_inst_rt_cost_is_1_5pt() -> None:
    assert _TMF_INST_RT_COST_PTS == 1.5


def test_tmf_retail_rt_cost_is_4pt_reference() -> None:
    assert _TMF_RETAIL_RT_COST_PTS == 4.0


# ----------------------------------------------------------------------------
# Monotonic time
# ----------------------------------------------------------------------------


def test_monotonic_timestamp_ordering_preserved() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    t0 = time.monotonic_ns()
    maker.on_tick(_bidask(22500, 22505, ts_ns=t0))
    t1 = time.monotonic_ns()
    maker.on_tick(_bidask(22501, 22506, ts_ns=t1))
    assert t1 > t0
    EPOCH_NS = 100_000_000_000_000_000
    assert t0 < EPOCH_NS
    assert t1 < EPOCH_NS


# ----------------------------------------------------------------------------
# Degenerate books
# ----------------------------------------------------------------------------


def test_trade_event_returns_hold() -> None:
    maker = TmfD6QueuePositionAwareMaker()
    actions = maker.on_tick(_trade(22501))
    assert actions == [Hold()]


def test_rejects_crossed_book() -> None:
    maker = TmfD6QueuePositionAwareMaker()
    actions = maker.on_tick(_bidask(22505, 22500))  # crossed
    assert [a for a in actions if isinstance(a, PostQuote)] == []


def test_rejects_zero_priced_book() -> None:
    maker = TmfD6QueuePositionAwareMaker()
    bad = TickData(
        exch_ts=1,
        bid_price=0,
        ask_price=22_505 * _SCALE,
        bid_qty=1,
        ask_qty=1,
        trade_price=0,
        trade_volume=0,
        is_trade=False,
        scale=_SCALE,
    )
    actions = maker.on_tick(bad)
    assert [a for a in actions if isinstance(a, PostQuote)] == []


# ----------------------------------------------------------------------------
# Gap / reset
# ----------------------------------------------------------------------------


def test_on_gap_clears_transient_quote_state() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(enable_qi_layer=False)
    )
    maker.on_tick(_bidask(22500, 22505))
    maker.on_gap()
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_on_gap_preserves_position() -> None:
    maker = TmfD6QueuePositionAwareMaker()
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert maker.position == 1
    maker.on_gap()
    assert maker.position == 1


def test_reset_clears_all_counters_including_queue_depth() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(max_pos=3, enable_qi_layer=False)
    )
    maker.on_tick(_bidask(22500, 22505, bid_qty=10))  # blocks bid
    assert maker.queue_depth_blocked_bid == 1
    maker.reset()
    assert maker.queue_depth_blocked_bid == 0
    assert maker.queue_depth_blocked_ask == 0
    assert maker.position == 0
    assert maker.quotes_posted == 0


# ----------------------------------------------------------------------------
# AlphaProtocol conformance
# ----------------------------------------------------------------------------


def test_c72_alpha_conforms_to_alpha_protocol() -> None:
    alpha = C72Alpha()
    assert isinstance(alpha, AlphaProtocol)
    assert alpha.manifest.alpha_id == "c72_tmfd6_queue_position_aware"
    assert alpha.manifest.strategy_type == "maker"
    assert alpha.reset() is None
    sig = alpha.update(foo=1)
    assert isinstance(sig, float)


def test_c72_manifest_declares_latency_profile() -> None:
    alpha = C72Alpha()
    assert alpha.manifest.latency_profile


def test_c72_manifest_documents_instrument_tmfd6() -> None:
    alpha = C72Alpha()
    assert alpha.manifest.instrument == "TMFD6"


def test_c72_hypothesis_mentions_queue_depth() -> None:
    alpha = C72Alpha()
    h = alpha.manifest.hypothesis.lower()
    assert "queue" in h and "depth" in h


def test_c72_hypothesis_cites_c60_overlay() -> None:
    alpha = C72Alpha()
    h = alpha.manifest.hypothesis.lower()
    assert "c60" in h or "overlay" in h


def test_c72_hypothesis_flags_dominance_risk() -> None:
    """T1 flagged dominance risk vs C60 baseline."""
    alpha = C72Alpha()
    h = alpha.manifest.hypothesis.lower()
    assert "dominance" in h or "arbiter" in h or "beat" in h


def test_c72_hypothesis_cites_non_pos_gated() -> None:
    alpha = C72Alpha()
    h = alpha.manifest.hypothesis.lower()
    assert "pos" in h and ("non" in h or "avoid" in h or "not" in h)


def test_c72_reset_clears_position() -> None:
    alpha = C72Alpha()
    alpha.maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert alpha.maker.position == 1
    alpha.reset()
    assert alpha.maker.position == 0


# ----------------------------------------------------------------------------
# Fill tracking
# ----------------------------------------------------------------------------


def test_position_increments_on_buy_fill() -> None:
    maker = TmfD6QueuePositionAwareMaker(params=C72Params(max_pos=3))
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert maker.position == 1


def test_position_decrements_on_sell_fill() -> None:
    maker = TmfD6QueuePositionAwareMaker(params=C72Params(max_pos=3))
    maker.on_fill("sell", 22_505 * _SCALE, 22502.5)
    assert maker.position == -1


def test_position_resets_last_quote_on_fill() -> None:
    maker = TmfD6QueuePositionAwareMaker(
        params=C72Params(max_pos=3, enable_qi_layer=False)
    )
    first = maker.on_tick(_bidask(22500, 22505))
    assert len([a for a in first if isinstance(a, PostQuote)]) == 2
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    second = maker.on_tick(_bidask(22500, 22505))
    buy_posts = [
        a for a in second if isinstance(a, PostQuote) and a.side == "buy"
    ]
    assert len(buy_posts) == 1  # last_bid cleared on fill; re-posts


# ----------------------------------------------------------------------------
# Cross-candidate distinction
# ----------------------------------------------------------------------------


def test_c72_default_spread_matches_c60() -> None:
    """C72 inherits C60's threshold; gate is the only difference."""
    p = C72Params()
    assert p.spread_threshold_pts == 5


def test_c72_default_mp_matches_c60() -> None:
    p = C72Params()
    assert p.max_pos == 2


def test_c72_instrument_is_tmfd6() -> None:
    alpha = C72Alpha()
    assert alpha.manifest.instrument == "TMFD6"


def test_c72_params_exposes_threshold_sweep_range() -> None:
    """T5 sweeps {2, 5, 10, 20}. Verify all four are acceptable values."""
    for bid_max, ask_max in [(2, 2), (5, 5), (10, 10), (20, 20)]:
        p = C72Params(
            queue_depth_max_bid=bid_max, queue_depth_max_ask=ask_max
        )
        assert p.queue_depth_max_bid == bid_max
        assert p.queue_depth_max_ask == ask_max


@pytest.mark.parametrize(
    "bid_qty,ask_qty,threshold,expected_buy_ok,expected_sell_ok",
    [
        (1, 1, 2, True, True),     # both thin, both admit
        (3, 1, 2, False, True),    # bid over, sell OK
        (1, 3, 2, True, False),    # sell over, bid OK
        (3, 3, 2, False, False),   # both over, both block
        (2, 2, 2, True, True),     # exact threshold both admit
    ],
)
def test_queue_depth_gate_parametrized(
    bid_qty: int,
    ask_qty: int,
    threshold: int,
    expected_buy_ok: bool,
    expected_sell_ok: bool,
) -> None:
    p = C72Params(
        enable_qi_layer=False,
        queue_depth_max_bid=threshold,
        queue_depth_max_ask=threshold,
    )
    maker = TmfD6QueuePositionAwareMaker(params=p)
    actions = maker.on_tick(
        _bidask(22500, 22505, bid_qty=bid_qty, ask_qty=ask_qty)
    )
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert ("buy" in sides) == expected_buy_ok
    assert ("sell" in sides) == expected_sell_ok
