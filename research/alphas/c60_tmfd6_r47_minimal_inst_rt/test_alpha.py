"""Unit tests for C60 TMFD6 R47-minimal maker under institutional RT.

Patterns per .agent/skills/hft-test-hft/SKILL.md:
  - Scaled-int price assertions (CK scale = 1_000_000; TMFD6 tick = 1 pt)
  - Monotonic time via time.monotonic_ns(), no wall-clock deps
  - Factory fixtures via helper functions

DA T2 required conditions verified:
  - Spread-gate boundary (sp=2, sp=3, sp=4 blocked at threshold=5;
    sp=5 admits; sp=6 admits)
  - R47-minimal: D1/D2/D3 signal-layer methods ABSENT on strategy
  - Max_pos gate (per contracts) at {1, 2, 3} (DA flag #4)
  - Inventory skew applied correctly (scaled-int)
  - Bid/ask execution (post at best bid / best ask; no mid pricing)
  - TMFD6 point value (10 NTD/pt; NOT 200 like TXF)
  - Cost citation: inst RT 1.5 pt (NOT retail 4 pt)
  - D4 QI skew: bid-heavy widens ask; ask-heavy widens bid; |pos|-independent
  - AlphaProtocol conformance
"""

from __future__ import annotations

import time

import pytest

from research.alphas.c60_tmfd6_r47_minimal_inst_rt.impl import (
    _DISABLED_SIGNAL_LAYERS_MOST,
    _TMF_INST_RT_COST_PTS,
    _TMF_POINT_VALUE_NTD,
    _TMF_RETAIL_RT_COST_PTS,
    C60Alpha,
    C60Params,
    TmfD6SoloMakerMinimal,
)
from research.backtest.maker_engine import (
    Hold,
    PostQuote,
    TickData,
)
from research.registry.schemas import AlphaProtocol

_SCALE = 1_000_000  # CK convention


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


# ----------------------------------------------------------------------------
# Spread-gate boundary (DA-mandated edge cases: TMFD6 median 2 pt, p75 3 pt)
# ----------------------------------------------------------------------------


def test_spread_gate_blocks_below_threshold_at_sp2() -> None:
    """Default spread_threshold_pts=5. Spread=2 (TMFD6 median) -> Hold."""
    maker = TmfD6SoloMakerMinimal()
    # disable QI so quotes post at raw bid/ask when gate passes; here it blocks
    actions = maker.on_tick(_bidask(22500, 22502))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []
    assert actions == [Hold()]


def test_spread_gate_blocks_below_threshold_at_sp3() -> None:
    """Spread=3 (TMFD6 p75) still below threshold=5 -> Hold."""
    maker = TmfD6SoloMakerMinimal()
    actions = maker.on_tick(_bidask(22500, 22503))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []


def test_spread_gate_blocks_below_threshold_at_sp4() -> None:
    """Spread=4 still below threshold=5 -> Hold."""
    maker = TmfD6SoloMakerMinimal()
    actions = maker.on_tick(_bidask(22500, 22504))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []


def test_spread_gate_admits_at_sp5_with_qi_off() -> None:
    """Spread=5 at threshold -> quotes post (strict >= comparison).
    QI disabled to isolate gate pass/fail from skew."""
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_spread_gate_admits_at_sp6_with_qi_off() -> None:
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22506))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_spread_gate_counter_advances_on_block() -> None:
    maker = TmfD6SoloMakerMinimal()
    maker.on_tick(_bidask(22500, 22504))
    maker.on_tick(_bidask(22501, 22504))  # diff levels, avoid price-move gate
    assert maker.spread_blocked == 2


# ----------------------------------------------------------------------------
# Scaled-int price arithmetic
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bid_pts,ask_pts,expected_bid,expected_ask",
    [
        (22500, 22505, 22_500_000_000, 22_505_000_000),
        (23000, 23006, 23_000_000_000, 23_006_000_000),
        (24500, 24505, 24_500_000_000, 24_505_000_000),
    ],
)
def test_posts_at_scaled_int_prices(
    bid_pts: int,
    ask_pts: int,
    expected_bid: int,
    expected_ask: int,
) -> None:
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(bid_pts, ask_pts))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    prices = {a.side: a.price for a in posts}
    assert prices["buy"] == expected_bid
    assert prices["sell"] == expected_ask
    for a in posts:
        assert isinstance(a.price, int)


# ----------------------------------------------------------------------------
# R47-minimal: D1/D2/D3 signal layers disabled (attribute absence)
# ----------------------------------------------------------------------------


def test_no_d1_d2_d3_signal_layer_methods_exposed() -> None:
    """R47-minimal: strategy must NOT expose PE/Queue/MFG signal methods.
    D4 QI skew IS retained (deployed config)."""
    maker = TmfD6SoloMakerMinimal()
    for attr_name in _DISABLED_SIGNAL_LAYERS_MOST:
        assert not hasattr(maker, attr_name), (
            f"R47-minimal violation: {attr_name} found on strategy"
        )


def test_d1_d2_d3_params_default_to_false() -> None:
    params = C60Params()
    assert params.enable_pe_layer is False
    assert params.enable_queue_layer is False
    assert params.enable_mfg_layer is False


def test_d4_qi_layer_enabled_by_default() -> None:
    """Deployed TMFD6 config retains D4 QI skew."""
    params = C60Params()
    assert params.enable_qi_layer is True
    assert params.qi_skew_threshold == 0.10
    assert params.qi_skew_widen_ticks == 1


def test_signal_layer_params_exposed_for_future() -> None:
    """Params exist for future enabling after TMFD6-specific calibration."""
    assert hasattr(C60Params(), "enable_pe_layer")
    assert hasattr(C60Params(), "enable_queue_layer")
    assert hasattr(C60Params(), "enable_mfg_layer")
    assert hasattr(C60Params(), "enable_qi_layer")


# ----------------------------------------------------------------------------
# Max-pos gate (DA flag #4: scorecard splits {1, 2, 3})
# ----------------------------------------------------------------------------


def test_max_pos_1_default_canonical() -> None:
    """Canonical C60 config for drop-in replacement on deployed TMFD6."""
    assert C60Params().max_pos == 1


def test_stops_buying_at_max_pos_1() -> None:
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(max_pos=1, enable_qi_layer=False)
    )
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert maker.position == 1
    actions = maker.on_tick(_bidask(22500, 22505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides
    assert "sell" in sides


def test_stops_selling_at_negative_max_pos_1() -> None:
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(max_pos=1, enable_qi_layer=False)
    )
    maker.on_fill("sell", 22_500 * _SCALE, 22502.5)
    assert maker.position == -1
    actions = maker.on_tick(_bidask(22500, 22505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "sell" not in sides
    assert "buy" in sides


def test_stops_buying_at_max_pos_2() -> None:
    """T5 bracket: max_pos=2."""
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(max_pos=2, enable_qi_layer=False)
    )
    for _ in range(2):
        maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert maker.position == 2
    actions = maker.on_tick(_bidask(22500, 22505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides


def test_stops_buying_at_max_pos_3() -> None:
    """T5 bracket: max_pos=3."""
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(max_pos=3, enable_qi_layer=False)
    )
    for _ in range(3):
        maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert maker.position == 3
    actions = maker.on_tick(_bidask(22500, 22505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides


def test_max_pos_blocked_counter_advances() -> None:
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(max_pos=1, enable_qi_layer=False)
    )
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    # Different price each tick to clear price-movement gate.
    maker.on_tick(_bidask(22500, 22505))
    maker.on_tick(_bidask(22501, 22506))
    assert maker.max_pos_blocked >= 1


# ----------------------------------------------------------------------------
# Inventory skew (NOT |pos|-gated; linear in pos -> not C22-class)
# ----------------------------------------------------------------------------


def test_inventory_skew_none_at_pos_zero() -> None:
    """At pos=0 skew = 0; quotes exactly at bid/ask."""
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 22500 * _SCALE
    assert posts["sell"] == 22505 * _SCALE


def test_inventory_skew_lowers_both_quotes_when_long() -> None:
    """Long -> skew negative (bid DOWN, ask DOWN) to encourage sells."""
    params = C60Params(max_pos=3, inventory_skew_tenths=2, enable_qi_layer=False)
    maker = TmfD6SoloMakerMinimal(params=params)
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    # skew = 1 * 2 * scale // 10 = 200_000 (0.2 pt)
    assert posts["buy"] == 22500 * _SCALE - 200_000
    assert posts["sell"] == 22505 * _SCALE - 200_000


def test_inventory_skew_raises_both_quotes_when_short() -> None:
    """Short -> skew positive (bid UP, ask UP) to encourage buys."""
    params = C60Params(max_pos=3, inventory_skew_tenths=2, enable_qi_layer=False)
    maker = TmfD6SoloMakerMinimal(params=params)
    maker.on_fill("sell", 22_500 * _SCALE, 22502.5)
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 22500 * _SCALE + 200_000
    assert posts["sell"] == 22505 * _SCALE + 200_000


# ----------------------------------------------------------------------------
# D4 QI skew layer (top-of-book imbalance; NOT |pos|-modulated)
# ----------------------------------------------------------------------------


def test_qi_skew_widens_ask_when_bid_heavy() -> None:
    """bid_qty >> ask_qty -> imbalance > threshold -> widen ASK by 1 tick."""
    params = C60Params(enable_qi_layer=True, qi_skew_threshold=0.10)
    maker = TmfD6SoloMakerMinimal(params=params)
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=90, ask_qty=10))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    # imbalance = (90-10)/100 = 0.80 > 0.10 -> ask widens UP by 1 tick
    assert posts["buy"] == 22500 * _SCALE
    assert posts["sell"] == 22505 * _SCALE + _SCALE
    assert maker.qi_widen_events == 1


def test_qi_skew_widens_bid_when_ask_heavy() -> None:
    """ask_qty >> bid_qty -> imbalance < -threshold -> widen BID by 1 tick."""
    params = C60Params(enable_qi_layer=True, qi_skew_threshold=0.10)
    maker = TmfD6SoloMakerMinimal(params=params)
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=10, ask_qty=90))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    # imbalance = (10-90)/100 = -0.80 < -0.10 -> bid widens DOWN by 1 tick
    assert posts["buy"] == 22500 * _SCALE - _SCALE
    assert posts["sell"] == 22505 * _SCALE
    assert maker.qi_widen_events == 1


def test_qi_skew_neutral_within_threshold() -> None:
    """|imbalance| <= threshold -> no widen."""
    params = C60Params(enable_qi_layer=True, qi_skew_threshold=0.10)
    maker = TmfD6SoloMakerMinimal(params=params)
    actions = maker.on_tick(_bidask(22500, 22505, bid_qty=52, ask_qty=48))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    # imbalance = (52-48)/100 = 0.04 < 0.10 -> no widen
    assert posts["buy"] == 22500 * _SCALE
    assert posts["sell"] == 22505 * _SCALE
    assert maker.qi_widen_events == 0


def test_qi_skew_is_not_pos_gated() -> None:
    """QI skew must NOT modulate by |pos| (C22-class meta-kill avoidance)."""
    params = C60Params(
        enable_qi_layer=True, qi_skew_threshold=0.10, max_pos=3
    )
    maker = TmfD6SoloMakerMinimal(params=params)
    # pos=0 state
    maker.on_tick(_bidask(22500, 22505, bid_qty=90, ask_qty=10))
    widen_at_pos0 = maker.qi_widen_events
    # drive pos up and re-quote on identical-shape book (different price to
    # clear price-move gate).
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert maker.position == 2
    maker.on_tick(_bidask(22501, 22506, bid_qty=90, ask_qty=10))
    widen_at_pos2 = maker.qi_widen_events - widen_at_pos0
    # QI skew must trigger at both |pos|=0 and |pos|=2 for same book shape.
    assert widen_at_pos0 == 1
    assert widen_at_pos2 == 1


def test_qi_skew_disabled_by_param() -> None:
    params = C60Params(enable_qi_layer=False)
    maker = TmfD6SoloMakerMinimal(params=params)
    maker.on_tick(_bidask(22500, 22505, bid_qty=90, ask_qty=10))
    assert maker.qi_widen_events == 0


# ----------------------------------------------------------------------------
# Price-movement gate (ROD anti-stack)
# ----------------------------------------------------------------------------


def test_does_not_restack_same_price() -> None:
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(enable_qi_layer=False)
    )
    first = maker.on_tick(_bidask(22500, 22505))
    second = maker.on_tick(_bidask(22500, 22505))
    assert len([a for a in first if isinstance(a, PostQuote)]) == 2
    assert second == [Hold()] or all(
        not isinstance(a, PostQuote) for a in second
    )


def test_reposts_when_price_moves() -> None:
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(enable_qi_layer=False)
    )
    maker.on_tick(_bidask(22500, 22505))
    second = maker.on_tick(_bidask(22501, 22506))
    assert len([a for a in second if isinstance(a, PostQuote)]) == 2


# ----------------------------------------------------------------------------
# Bid/ask execution (no mid) — DA flag #2 MANDATORY
# ----------------------------------------------------------------------------


def test_posts_at_best_bid_not_mid() -> None:
    """Buy quotes go at bid_price exactly, not at mid."""
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    # Mid = 22502.5 x scale = 22_502_500_000 — must NOT equal buy quote
    assert posts["buy"] == 22500 * _SCALE
    assert posts["buy"] != 22_502_500_000


def test_posts_at_best_ask_not_mid() -> None:
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(enable_qi_layer=False)
    )
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["sell"] == 22505 * _SCALE


# ----------------------------------------------------------------------------
# TMFD6 point value & inst RT citation
# ----------------------------------------------------------------------------


def test_tmf_point_value_is_10_ntd() -> None:
    """TMFD6 point_value = 10 NTD/pt (NOT 200 like TXF)."""
    assert _TMF_POINT_VALUE_NTD == 10


def test_tmf_inst_rt_cost_is_1_5pt() -> None:
    """Cited from shared-context.yaml#cost_model.TMF (institutional estimate)."""
    assert _TMF_INST_RT_COST_PTS == 1.5


def test_tmf_retail_rt_cost_is_4pt_reference() -> None:
    """Retail reference for delta comparison (NOT the inst source)."""
    assert _TMF_RETAIL_RT_COST_PTS == 4.0


# ----------------------------------------------------------------------------
# Monotonic time ordering
# ----------------------------------------------------------------------------


def test_monotonic_timestamp_ordering_preserved() -> None:
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(enable_qi_layer=False)
    )
    t0 = time.monotonic_ns()
    maker.on_tick(_bidask(22500, 22505, ts_ns=t0))
    t1 = time.monotonic_ns()
    maker.on_tick(_bidask(22501, 22506, ts_ns=t1))
    assert t1 > t0
    EPOCH_THRESHOLD_NS = 100_000_000_000_000_000
    assert t0 < EPOCH_THRESHOLD_NS
    assert t1 < EPOCH_THRESHOLD_NS


# ----------------------------------------------------------------------------
# Degenerate books
# ----------------------------------------------------------------------------


def test_trade_event_returns_hold() -> None:
    maker = TmfD6SoloMakerMinimal()
    actions = maker.on_tick(_trade(22501, volume=3))
    assert actions == [Hold()]


def test_rejects_crossed_book() -> None:
    maker = TmfD6SoloMakerMinimal()
    actions = maker.on_tick(_bidask(22505, 22504))  # crossed
    assert [a for a in actions if isinstance(a, PostQuote)] == []


def test_rejects_zero_priced_book() -> None:
    maker = TmfD6SoloMakerMinimal()
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
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(enable_qi_layer=False)
    )
    maker.on_tick(_bidask(22500, 22505))
    maker.on_gap()
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_on_gap_preserves_position() -> None:
    """Gap must not mutate fill-tracked position."""
    maker = TmfD6SoloMakerMinimal()
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert maker.position == 1
    maker.on_gap()
    assert maker.position == 1


def test_reset_clears_position_and_quotes() -> None:
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(max_pos=3, enable_qi_layer=False)
    )
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    maker.on_tick(_bidask(22500, 22505))
    assert maker.position == 1
    assert maker.quotes_posted == 2
    maker.reset()
    assert maker.position == 0
    assert maker.quotes_posted == 0
    assert maker.qi_widen_events == 0


# ----------------------------------------------------------------------------
# AlphaProtocol conformance
# ----------------------------------------------------------------------------


def test_c60_alpha_conforms_to_alpha_protocol() -> None:
    alpha = C60Alpha()
    assert isinstance(alpha, AlphaProtocol)
    assert alpha.manifest.alpha_id == "c60_tmfd6_r47_minimal_inst_rt"
    assert alpha.manifest.strategy_type == "maker"
    sig = alpha.update(foo=1)
    assert isinstance(sig, float)
    assert alpha.reset() is None


def test_c60_manifest_declares_latency_profile() -> None:
    alpha = C60Alpha()
    assert alpha.manifest.latency_profile is not None
    assert alpha.manifest.latency_profile != ""


def test_c60_manifest_documents_instrument_tmfd6() -> None:
    alpha = C60Alpha()
    assert alpha.manifest.instrument == "TMFD6"


def test_c60_hypothesis_cites_r47_minimal() -> None:
    alpha = C60Alpha()
    h = alpha.manifest.hypothesis.upper()
    assert "R47-MINIMAL" in h or "MINIMAL" in h


def test_c60_hypothesis_cites_inst_rt() -> None:
    alpha = C60Alpha()
    h = alpha.manifest.hypothesis
    assert "1.5" in h and "4" in h  # inst vs retail


def test_c60_hypothesis_cites_non_pos_gated() -> None:
    """Must document non-|pos|-gating to signal C22-class avoidance."""
    alpha = C60Alpha()
    h = alpha.manifest.hypothesis.lower()
    assert "pos" in h and ("non" in h or "avoid" in h)


def test_c60_reset_clears_position() -> None:
    alpha = C60Alpha()
    alpha.maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert alpha.maker.position == 1
    alpha.reset()
    assert alpha.maker.position == 0


# ----------------------------------------------------------------------------
# Cycle tracking (position accounting for close-maker-rate analysis)
# ----------------------------------------------------------------------------


def test_position_decreases_on_sell_fill() -> None:
    """Close-side-reducing semantics: selling when long reduces |pos|."""
    maker = TmfD6SoloMakerMinimal(params=C60Params(max_pos=3))
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    assert maker.position == 2
    maker.on_fill("sell", 22_510 * _SCALE, 22505.5)
    assert maker.position == 1
    maker.on_fill("sell", 22_510 * _SCALE, 22505.5)
    assert maker.position == 0


def test_position_resets_last_quote_on_fill() -> None:
    """On fill, last_bid/last_ask clears so the next tick can re-post."""
    maker = TmfD6SoloMakerMinimal(
        params=C60Params(max_pos=3, enable_qi_layer=False)
    )
    first = maker.on_tick(_bidask(22500, 22505))
    assert len([a for a in first if isinstance(a, PostQuote)]) == 2
    maker.on_fill("buy", 22_500 * _SCALE, 22502.5)
    # Same book -- buy side should repost since last_bid cleared on fill.
    second = maker.on_tick(_bidask(22500, 22505))
    buy_posts = [
        a for a in second
        if isinstance(a, PostQuote) and a.side == "buy"
    ]
    assert len(buy_posts) == 1
