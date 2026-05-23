"""Unit tests for C63 TXFD6 R47-minimal maker with tightened spread threshold.

Patterns per .agent/skills/hft-test-hft/SKILL.md:
  - Scaled-int price assertions (CK scale = 1_000_000; TXFD6 tick = 1 pt)
  - Monotonic time via time.monotonic_ns(), no wall-clock deps
  - Factory fixtures via helper functions

DA T2 critical conditions verified:
  - Spread-gate boundary (sp=1/2 blocked; sp=3/4/5/6 admit at threshold=3)
  - R47-minimal: ALL FOUR signal-layer methods ABSENT on strategy
  - Max_pos gate (per contracts) at {1, 3, 5}
  - Inventory skew applied correctly (scaled-int; LINEAR in pos)
  - Bid/ask execution (post at best bid / best ask; no mid pricing)
  - TXFD6 point value (200 NTD/pt)
  - Cost citation: inst RT 1.5 pt (NOT retail 3 pt)
  - AlphaProtocol conformance
  - C33 distinction: spread_threshold default = 3 (NOT C33's 5)
"""

from __future__ import annotations

import time

import pytest

from research.alphas.c63_txfd6_r47_tight_spread.impl import (
    _DISABLED_SIGNAL_LAYERS,
    _TXF_INST_RT_COST_PTS,
    _TXF_POINT_VALUE_NTD,
    _TXF_RETAIL_RT_COST_PTS,
    C63Alpha,
    C63Params,
    TxfD6R47TightSpreadMaker,
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
# Spread-gate boundary — C63 signature: threshold=3, NOT C33's 5
# ----------------------------------------------------------------------------


def test_spread_threshold_default_is_3_not_5() -> None:
    """C63 single-lever change: spread_threshold_pts = 3 (C33 was 5)."""
    assert C63Params().spread_threshold_pts == 3


def test_spread_gate_blocks_at_sp1() -> None:
    """sp=1 < threshold=3 -> Hold."""
    maker = TxfD6R47TightSpreadMaker()
    actions = maker.on_tick(_bidask(17500, 17501))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []
    assert actions == [Hold()]


def test_spread_gate_blocks_at_sp2() -> None:
    """sp=2 < threshold=3 -> Hold."""
    maker = TxfD6R47TightSpreadMaker()
    actions = maker.on_tick(_bidask(17500, 17502))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []


def test_spread_gate_admits_at_sp3() -> None:
    """sp=3 at threshold -> quotes post (strict >= comparison)."""
    maker = TxfD6R47TightSpreadMaker()
    actions = maker.on_tick(_bidask(17500, 17503))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_spread_gate_admits_at_sp4() -> None:
    """sp=4 > threshold=3 -> quotes post (and would ALSO have triggered C33)."""
    maker = TxfD6R47TightSpreadMaker()
    actions = maker.on_tick(_bidask(17500, 17504))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_spread_gate_admits_at_sp5() -> None:
    """sp=5 at C33's threshold (but C63 admits already at sp=3)."""
    maker = TxfD6R47TightSpreadMaker()
    actions = maker.on_tick(_bidask(17500, 17505))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_spread_gate_admits_at_sp6() -> None:
    maker = TxfD6R47TightSpreadMaker()
    actions = maker.on_tick(_bidask(17500, 17506))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_spread_gate_counter_advances_on_block() -> None:
    maker = TxfD6R47TightSpreadMaker()
    maker.on_tick(_bidask(17500, 17501))
    maker.on_tick(_bidask(17501, 17502))  # diff levels, avoid price-move gate
    assert maker.spread_blocked == 2


# ----------------------------------------------------------------------------
# Scaled-int price arithmetic
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bid_pts,ask_pts,expected_bid,expected_ask",
    [
        (17500, 17503, 17_500_000_000, 17_503_000_000),
        (17500, 17504, 17_500_000_000, 17_504_000_000),
        (18000, 18005, 18_000_000_000, 18_005_000_000),
        (19500, 19506, 19_500_000_000, 19_506_000_000),
    ],
)
def test_posts_at_scaled_int_prices(
    bid_pts: int,
    ask_pts: int,
    expected_bid: int,
    expected_ask: int,
) -> None:
    maker = TxfD6R47TightSpreadMaker()
    actions = maker.on_tick(_bidask(bid_pts, ask_pts))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    prices = {a.side: a.price for a in posts}
    assert prices["buy"] == expected_bid
    assert prices["sell"] == expected_ask
    for a in posts:
        assert isinstance(a.price, int)


# ----------------------------------------------------------------------------
# R47-minimal: ALL signal layers disabled (attribute absence)
# ----------------------------------------------------------------------------


def test_no_signal_layer_methods_exposed() -> None:
    """R47-minimal: strategy must NOT expose PE/Queue/MFG/QI signal methods."""
    maker = TxfD6R47TightSpreadMaker()
    for attr_name in _DISABLED_SIGNAL_LAYERS:
        assert not hasattr(maker, attr_name), (
            f"R47-minimal violation: {attr_name} found on strategy"
        )


def test_signal_layer_params_default_to_false() -> None:
    params = C63Params()
    assert params.enable_pe_layer is False
    assert params.enable_queue_layer is False
    assert params.enable_mfg_layer is False
    assert params.enable_qi_layer is False


def test_signal_layer_params_exposed_for_future() -> None:
    """Params exist for future enabling after TXFD6-specific calibration."""
    assert hasattr(C63Params(), "enable_pe_layer")
    assert hasattr(C63Params(), "enable_queue_layer")
    assert hasattr(C63Params(), "enable_mfg_layer")
    assert hasattr(C63Params(), "enable_qi_layer")


def test_impl_does_not_import_r47_signal_state_classes() -> None:
    """Static check: impl module does not import R47 signal state classes."""
    import research.alphas.c63_txfd6_r47_tight_spread.impl as mod
    src_path = mod.__file__
    assert src_path is not None
    with open(src_path) as f:
        source = f.read()
    forbidden = ("_PEState", "_QueueState", "_MFGState", "_QIState")
    for sym in forbidden:
        assert sym not in source, (
            f"R47-minimal violation: impl references {sym}"
        )


# ----------------------------------------------------------------------------
# Max-pos gate (canonical mp=3, same as C33)
# ----------------------------------------------------------------------------


def test_max_pos_default_is_3_canonical() -> None:
    """C63 inherits C33 research operating point (mp=3)."""
    assert C63Params().max_pos == 3


def test_stops_buying_at_max_pos_1() -> None:
    maker = TxfD6R47TightSpreadMaker(params=C63Params(max_pos=1))
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.position == 1
    actions = maker.on_tick(_bidask(17500, 17503))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides
    assert "sell" in sides


def test_stops_selling_at_negative_max_pos_1() -> None:
    maker = TxfD6R47TightSpreadMaker(params=C63Params(max_pos=1))
    maker.on_fill("sell", 17_500 * _SCALE, 17500.5)
    assert maker.position == -1
    actions = maker.on_tick(_bidask(17500, 17503))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "sell" not in sides
    assert "buy" in sides


def test_stops_buying_at_max_pos_3() -> None:
    """T5 bracket: max_pos=3 (canonical)."""
    maker = TxfD6R47TightSpreadMaker(params=C63Params(max_pos=3))
    for _ in range(3):
        maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.position == 3
    actions = maker.on_tick(_bidask(17500, 17503))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides


def test_stops_buying_at_max_pos_5() -> None:
    """T5 bracket: max_pos=5 (extension)."""
    maker = TxfD6R47TightSpreadMaker(params=C63Params(max_pos=5))
    for _ in range(5):
        maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.position == 5
    actions = maker.on_tick(_bidask(17500, 17503))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides


def test_max_pos_blocked_counter_advances() -> None:
    maker = TxfD6R47TightSpreadMaker(params=C63Params(max_pos=1))
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    maker.on_tick(_bidask(17500, 17503))
    maker.on_tick(_bidask(17501, 17504))
    assert maker.max_pos_blocked >= 1


# ----------------------------------------------------------------------------
# Inventory skew (LINEAR in pos; NOT |pos|-gated -> no C22-class meta-kill)
# ----------------------------------------------------------------------------


def test_inventory_skew_none_at_pos_zero() -> None:
    """At pos=0 skew = 0; quotes exactly at bid/ask."""
    maker = TxfD6R47TightSpreadMaker()
    actions = maker.on_tick(_bidask(17500, 17503))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 17500 * _SCALE
    assert posts["sell"] == 17503 * _SCALE


def test_inventory_skew_lowers_both_quotes_when_long() -> None:
    """Long inventory -> skew negative (both quotes down) to encourage sells."""
    params = C63Params(max_pos=3, inventory_skew_tenths=2)
    maker = TxfD6R47TightSpreadMaker(params=params)
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    # skew = 1 * 2 * scale // 10 = 200_000 (0.2 pt)
    actions = maker.on_tick(_bidask(17500, 17503))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 17500 * _SCALE - 200_000
    assert posts["sell"] == 17503 * _SCALE - 200_000


def test_inventory_skew_raises_both_quotes_when_short() -> None:
    """Short inventory -> skew positive (both quotes up) to encourage buys."""
    params = C63Params(max_pos=3, inventory_skew_tenths=2)
    maker = TxfD6R47TightSpreadMaker(params=params)
    maker.on_fill("sell", 17_500 * _SCALE, 17500.5)
    actions = maker.on_tick(_bidask(17500, 17503))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 17500 * _SCALE + 200_000
    assert posts["sell"] == 17503 * _SCALE + 200_000


def test_inventory_skew_scales_linearly_with_pos() -> None:
    """Skew is LINEAR in pos (not |pos|-thresholded -> no C22-class kill)."""
    params = C63Params(max_pos=5, inventory_skew_tenths=2)
    maker = TxfD6R47TightSpreadMaker(params=params)
    # Drive pos to +3
    for _ in range(3):
        maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    # Skew should be 3 * 2 * scale // 10 = 600_000 (3x the pos=1 value)
    actions = maker.on_tick(_bidask(17500, 17506))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 17500 * _SCALE - 600_000
    assert posts["sell"] == 17506 * _SCALE - 600_000


# ----------------------------------------------------------------------------
# Price-movement gate (ROD anti-stack)
# ----------------------------------------------------------------------------


def test_does_not_restack_same_price() -> None:
    maker = TxfD6R47TightSpreadMaker()
    first = maker.on_tick(_bidask(17500, 17503))
    second = maker.on_tick(_bidask(17500, 17503))
    assert len([a for a in first if isinstance(a, PostQuote)]) == 2
    assert second == [Hold()] or all(
        not isinstance(a, PostQuote) for a in second
    )


def test_reposts_when_price_moves() -> None:
    maker = TxfD6R47TightSpreadMaker()
    maker.on_tick(_bidask(17500, 17503))
    second = maker.on_tick(_bidask(17501, 17504))
    assert len([a for a in second if isinstance(a, PostQuote)]) == 2


# ----------------------------------------------------------------------------
# Bid/ask execution (no mid)
# ----------------------------------------------------------------------------


def test_posts_at_best_bid_not_mid() -> None:
    maker = TxfD6R47TightSpreadMaker()
    actions = maker.on_tick(_bidask(17500, 17503))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    # Mid = 17501.5 * _SCALE = 17_501_500_000
    assert posts["buy"] == 17500 * _SCALE
    assert posts["buy"] != 17_501_500_000


def test_posts_at_best_ask_not_mid() -> None:
    maker = TxfD6R47TightSpreadMaker()
    actions = maker.on_tick(_bidask(17500, 17503))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["sell"] == 17503 * _SCALE


# ----------------------------------------------------------------------------
# TXFD6 point value & inst RT citation
# ----------------------------------------------------------------------------


def test_txf_point_value_is_200_ntd() -> None:
    """TXFD6 point_value = 200 NTD/pt (NOT 10 like TMF)."""
    assert _TXF_POINT_VALUE_NTD == 200


def test_txf_inst_rt_cost_is_1_5pt() -> None:
    """Cited from shared-context.yaml#cost_model.TXF (institutional estimate)."""
    assert _TXF_INST_RT_COST_PTS == 1.5


def test_txf_retail_rt_cost_is_3pt_reference() -> None:
    """Retail reference for delta comparison (NOT the inst source)."""
    assert _TXF_RETAIL_RT_COST_PTS == 3.0


def test_tight_threshold_economic_viability() -> None:
    """sp=3 (threshold) gross = 3 pt > inst RT 1.5 pt -> structurally viable."""
    params = C63Params()
    assert params.spread_threshold_pts >= _TXF_INST_RT_COST_PTS
    # gross at threshold floor
    gross_at_threshold = float(params.spread_threshold_pts)
    margin = gross_at_threshold - _TXF_INST_RT_COST_PTS
    assert margin >= 1.0  # at least 1 pt structural margin


# ----------------------------------------------------------------------------
# Monotonic time ordering
# ----------------------------------------------------------------------------


def test_monotonic_timestamp_ordering_preserved() -> None:
    maker = TxfD6R47TightSpreadMaker()
    t0 = time.monotonic_ns()
    maker.on_tick(_bidask(17500, 17503, ts_ns=t0))
    t1 = time.monotonic_ns()
    maker.on_tick(_bidask(17501, 17504, ts_ns=t1))
    assert t1 > t0
    EPOCH_THRESHOLD_NS = 100_000_000_000_000_000
    assert t0 < EPOCH_THRESHOLD_NS
    assert t1 < EPOCH_THRESHOLD_NS


# ----------------------------------------------------------------------------
# Degenerate books
# ----------------------------------------------------------------------------


def test_trade_event_returns_hold() -> None:
    maker = TxfD6R47TightSpreadMaker()
    actions = maker.on_tick(_trade(17501, volume=3))
    assert actions == [Hold()]


def test_rejects_crossed_book() -> None:
    maker = TxfD6R47TightSpreadMaker()
    actions = maker.on_tick(_bidask(17505, 17504))  # crossed
    assert [a for a in actions if isinstance(a, PostQuote)] == []


def test_rejects_zero_priced_book() -> None:
    maker = TxfD6R47TightSpreadMaker()
    bad = TickData(
        exch_ts=1,
        bid_price=0,
        ask_price=17_505 * _SCALE,
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
    maker = TxfD6R47TightSpreadMaker()
    maker.on_tick(_bidask(17500, 17503))
    maker.on_gap()
    # After gap, same prices can post again (price-gate cleared).
    actions = maker.on_tick(_bidask(17500, 17503))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_on_gap_preserves_position() -> None:
    """Gap must not mutate fill-tracked position."""
    maker = TxfD6R47TightSpreadMaker()
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.position == 1
    maker.on_gap()
    assert maker.position == 1


def test_reset_clears_position_and_quotes() -> None:
    maker = TxfD6R47TightSpreadMaker(params=C63Params(max_pos=3))
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    maker.on_tick(_bidask(17500, 17503))
    assert maker.position == 1
    assert maker.quotes_posted == 2
    maker.reset()
    assert maker.position == 0
    assert maker.quotes_posted == 0


# ----------------------------------------------------------------------------
# AlphaProtocol conformance
# ----------------------------------------------------------------------------


def test_c63_alpha_conforms_to_alpha_protocol() -> None:
    alpha = C63Alpha()
    assert isinstance(alpha, AlphaProtocol)
    assert alpha.manifest.alpha_id == "c63_txfd6_r47_tight_spread"
    assert alpha.manifest.strategy_type == "maker"
    sig = alpha.update(foo=1)
    assert isinstance(sig, float)
    assert alpha.reset() is None


def test_c63_manifest_declares_latency_profile() -> None:
    alpha = C63Alpha()
    assert alpha.manifest.latency_profile is not None
    assert alpha.manifest.latency_profile != ""


def test_c63_manifest_documents_instrument_txfd6() -> None:
    alpha = C63Alpha()
    assert alpha.manifest.instrument == "TXFD6"


def test_c63_hypothesis_cites_r47_minimal() -> None:
    alpha = C63Alpha()
    h = alpha.manifest.hypothesis.upper()
    assert "R47-MINIMAL" in h or "MINIMAL" in h


def test_c63_hypothesis_cites_tight_threshold() -> None:
    alpha = C63Alpha()
    h = alpha.manifest.hypothesis
    # Must document the lever change explicitly
    assert "3" in h and "5" in h


def test_c63_hypothesis_cites_inst_rt() -> None:
    alpha = C63Alpha()
    h = alpha.manifest.hypothesis
    assert "1.5" in h and "3" in h


def test_c63_reset_clears_position() -> None:
    alpha = C63Alpha()
    alpha.maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    assert alpha.maker.position == 1
    alpha.reset()
    assert alpha.maker.position == 0


# ----------------------------------------------------------------------------
# Cycle tracking / position accounting
# ----------------------------------------------------------------------------


def test_position_decreases_on_sell_fill() -> None:
    maker = TxfD6R47TightSpreadMaker(params=C63Params(max_pos=3))
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.position == 2
    maker.on_fill("sell", 17_510 * _SCALE, 17505.5)
    assert maker.position == 1
    maker.on_fill("sell", 17_510 * _SCALE, 17505.5)
    assert maker.position == 0


def test_position_resets_last_quote_on_fill() -> None:
    """On fill, last_bid/last_ask clears so next tick can re-post."""
    maker = TxfD6R47TightSpreadMaker(params=C63Params(max_pos=3))
    first = maker.on_tick(_bidask(17500, 17503))
    assert len([a for a in first if isinstance(a, PostQuote)]) == 2
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    # Same book -- buy side should repost since last_bid cleared on fill.
    second = maker.on_tick(_bidask(17500, 17503))
    buy_posts = [
        a for a in second if isinstance(a, PostQuote) and a.side == "buy"
    ]
    assert len(buy_posts) == 1


# ----------------------------------------------------------------------------
# C33 distinction — C63's single-lever variant signature
# ----------------------------------------------------------------------------


def test_c63_differs_from_c33_only_in_spread_threshold() -> None:
    """C63 MUST be structurally identical to C33 except threshold=3 vs 5."""
    params = C63Params()
    # Match C33 on non-lever params (see c33_txfd6_solo_passive_maker impl)
    assert params.max_pos == 3                 # (C33 research operating mp=3)
    assert params.inventory_skew_tenths == 2   # (C33 matches)
    assert params.enable_pe_layer is False
    assert params.enable_queue_layer is False
    assert params.enable_mfg_layer is False
    assert params.enable_qi_layer is False     # C33 precedent for TXFD6
    # Differ only on the lever:
    assert params.spread_threshold_pts == 3    # (C33 is 5)


def test_c63_manifest_paper_refs_include_c33_precedent() -> None:
    alpha = C63Alpha()
    refs = alpha.manifest.paper_refs
    assert any("c33" in r.lower() for r in refs)


def test_c63_instrument_is_txfd6_not_tmfd6() -> None:
    """C63 is TXFD6 (200 NTD/pt), NOT TMFD6 (10 NTD/pt)."""
    alpha = C63Alpha()
    assert alpha.manifest.instrument == "TXFD6"
    assert _TXF_POINT_VALUE_NTD == 200
