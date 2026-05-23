"""Unit tests for C33 TXFD6 solo passive maker.

Patterns per .agent/skills/hft-test-hft/SKILL.md:
  - Scaled-int price assertions (CK scale = 1_000_000; TXFD6 tick = 1 pt)
  - Monotonic time via time.monotonic_ns(), no wall-clock deps
  - Factory fixtures via helper functions

DA T2 critical conditions verified:
  - Spread-gate boundary (sp=4 blocked; sp=5 passes; sp=6 passes)
  - R47-minimal: signal-layer methods ABSENT on strategy (PE, Queue, MFG, QI)
  - Max_pos gate (per contracts) — suppresses adverse side at cap
  - Inventory skew applied correctly (scaled-int)
  - Bid/ask execution (post at best bid / best ask; no mid pricing)
  - TXFD6 point value (200 NTD/pt) on NTD conversion
  - AlphaProtocol conformance
"""

from __future__ import annotations

import time

import pytest

from research.alphas.c33_txfd6_solo_passive_maker.impl import (
    C33Alpha,
    C33Params,
    TxfD6SoloMaker,
    _DISABLED_SIGNAL_LAYERS,
    _TXF_POINT_VALUE_NTD,
    _TXF_RT_COST_PTS,
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
# Spread-gate boundary (DA-mandated edge case)
# ----------------------------------------------------------------------------


def test_spread_gate_blocks_at_sp4() -> None:
    """Default spread_threshold_pts=5. Spread=4 → Hold (blocked)."""
    maker = TxfD6SoloMaker()
    actions = maker.on_tick(_bidask(17500, 17504))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []
    assert actions == [Hold()]


def test_spread_gate_admits_at_sp5() -> None:
    """Spread=5 at threshold → quotes post (strict >= comparison)."""
    maker = TxfD6SoloMaker()
    actions = maker.on_tick(_bidask(17500, 17505))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_spread_gate_admits_at_sp6() -> None:
    """Spread=6 > threshold → quotes post."""
    maker = TxfD6SoloMaker()
    actions = maker.on_tick(_bidask(17500, 17506))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_spread_gate_counter_advances_on_block() -> None:
    maker = TxfD6SoloMaker()
    maker.on_tick(_bidask(17500, 17504))
    maker.on_tick(_bidask(17501, 17504))  # diff levels to avoid price-move gate
    assert maker.spread_blocked == 2


# ----------------------------------------------------------------------------
# Scaled-int price arithmetic
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bid_pts,ask_pts,expected_bid,expected_ask",
    [
        (17500, 17505, 17_500_000_000, 17_505_000_000),
        (18000, 18006, 18_000_000_000, 18_006_000_000),
        (19500, 19505, 19_500_000_000, 19_505_000_000),
    ],
)
def test_posts_at_scaled_int_prices(
    bid_pts: int,
    ask_pts: int,
    expected_bid: int,
    expected_ask: int,
) -> None:
    maker = TxfD6SoloMaker()
    actions = maker.on_tick(_bidask(bid_pts, ask_pts))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    prices = {a.side: a.price for a in posts}
    assert prices["buy"] == expected_bid
    assert prices["sell"] == expected_ask
    for a in posts:
        assert isinstance(a.price, int)


# ----------------------------------------------------------------------------
# R47-minimal: signal layers disabled (attribute absence)
# ----------------------------------------------------------------------------


def test_no_signal_layer_methods_exposed() -> None:
    """R47-minimal: strategy must NOT expose PE/Queue/MFG/QI signal methods."""
    maker = TxfD6SoloMaker()
    for attr_name in _DISABLED_SIGNAL_LAYERS:
        assert not hasattr(maker, attr_name), (
            f"R47-minimal violation: {attr_name} found on strategy"
        )


def test_signal_layer_params_default_to_false() -> None:
    params = C33Params()
    assert params.enable_pe_layer is False
    assert params.enable_queue_layer is False
    assert params.enable_mfg_layer is False
    assert params.enable_qi_layer is False


def test_signal_layer_params_exposed_for_future() -> None:
    """Params exist so T8/future round can enable layers after calibration."""
    assert hasattr(C33Params(), "enable_pe_layer")
    assert hasattr(C33Params(), "enable_queue_layer")
    assert hasattr(C33Params(), "enable_mfg_layer")
    assert hasattr(C33Params(), "enable_qi_layer")


# ----------------------------------------------------------------------------
# Max-pos gate
# ----------------------------------------------------------------------------


def test_max_pos_1_default() -> None:
    assert C33Params().max_pos == 1


def test_stops_buying_at_max_pos_1() -> None:
    maker = TxfD6SoloMaker(params=C33Params(max_pos=1))
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.position == 1
    actions = maker.on_tick(_bidask(17500, 17505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides
    assert "sell" in sides


def test_stops_selling_at_negative_max_pos_1() -> None:
    maker = TxfD6SoloMaker(params=C33Params(max_pos=1))
    maker.on_fill("sell", 17_500 * _SCALE, 17500.5)
    assert maker.position == -1
    actions = maker.on_tick(_bidask(17500, 17505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "sell" not in sides
    assert "buy" in sides


def test_stops_buying_at_max_pos_3() -> None:
    """T5 bracket: max_pos=3."""
    maker = TxfD6SoloMaker(params=C33Params(max_pos=3))
    for _ in range(3):
        maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.position == 3
    actions = maker.on_tick(_bidask(17500, 17505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides


def test_stops_buying_at_max_pos_5() -> None:
    """T5 bracket: max_pos=5."""
    maker = TxfD6SoloMaker(params=C33Params(max_pos=5))
    for _ in range(5):
        maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.position == 5
    actions = maker.on_tick(_bidask(17500, 17505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides


def test_max_pos_blocked_counter_advances() -> None:
    maker = TxfD6SoloMaker(params=C33Params(max_pos=1))
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    # Different price each tick to clear price-movement gate.
    maker.on_tick(_bidask(17500, 17505))
    maker.on_tick(_bidask(17501, 17506))
    assert maker.max_pos_blocked >= 1


# ----------------------------------------------------------------------------
# Inventory skew
# ----------------------------------------------------------------------------


def test_inventory_skew_none_at_pos_zero() -> None:
    """At pos=0 skew = 0; quotes exactly at bid/ask."""
    maker = TxfD6SoloMaker()
    actions = maker.on_tick(_bidask(17500, 17505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 17500 * _SCALE
    assert posts["sell"] == 17505 * _SCALE


def test_inventory_skew_lowers_both_quotes_when_long() -> None:
    """Long inventory → skew is negative (shift bid DOWN, ask DOWN) to encourage sells."""
    params = C33Params(max_pos=3, inventory_skew_tenths=2)
    maker = TxfD6SoloMaker(params=params)
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    # skew = 1 * 2 * scale // 10 = 200_000 (0.2 pt)
    actions = maker.on_tick(_bidask(17500, 17505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["buy"] == 17500 * _SCALE - 200_000
    assert posts["sell"] == 17505 * _SCALE - 200_000


def test_inventory_skew_raises_both_quotes_when_short() -> None:
    """Short inventory → skew is positive (shift bid UP, ask UP) to encourage buys."""
    params = C33Params(max_pos=3, inventory_skew_tenths=2)
    maker = TxfD6SoloMaker(params=params)
    maker.on_fill("sell", 17_500 * _SCALE, 17500.5)
    actions = maker.on_tick(_bidask(17500, 17505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    # pos = -1 → skew = -1 * 2 * scale // 10 = -200_000
    assert posts["buy"] == 17500 * _SCALE - (-200_000)  # = +200_000
    assert posts["sell"] == 17505 * _SCALE + 200_000


# ----------------------------------------------------------------------------
# Price-movement gate (ROD anti-stack)
# ----------------------------------------------------------------------------


def test_does_not_restack_same_price() -> None:
    maker = TxfD6SoloMaker()
    first = maker.on_tick(_bidask(17500, 17505))
    second = maker.on_tick(_bidask(17500, 17505))
    assert len([a for a in first if isinstance(a, PostQuote)]) == 2
    assert second == [Hold()] or all(
        not isinstance(a, PostQuote) for a in second
    )


def test_reposts_when_price_moves() -> None:
    maker = TxfD6SoloMaker()
    maker.on_tick(_bidask(17500, 17505))
    second = maker.on_tick(_bidask(17501, 17506))
    assert len([a for a in second if isinstance(a, PostQuote)]) == 2


# ----------------------------------------------------------------------------
# Bid/ask execution (no mid)
# ----------------------------------------------------------------------------


def test_posts_at_best_bid_not_mid() -> None:
    """Buy quotes go at bid_price exactly, not at mid."""
    maker = TxfD6SoloMaker()
    actions = maker.on_tick(_bidask(17500, 17505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    # Mid = 17502.5 × scale = 17_502_500_000 — must NOT equal buy quote
    assert posts["buy"] == 17500 * _SCALE
    assert posts["buy"] != 17_502_500_000


def test_posts_at_best_ask_not_mid() -> None:
    maker = TxfD6SoloMaker()
    actions = maker.on_tick(_bidask(17500, 17505))
    posts = {a.side: a.price for a in actions if isinstance(a, PostQuote)}
    assert posts["sell"] == 17505 * _SCALE


# ----------------------------------------------------------------------------
# TXFD6 point value
# ----------------------------------------------------------------------------


def test_txf_point_value_is_200_ntd() -> None:
    """TXFD6 point_value = 200 NTD/pt (NOT 10 like TMFD6)."""
    assert _TXF_POINT_VALUE_NTD == 200


def test_txf_rt_cost_is_3pt() -> None:
    """Cited from memory/feedback_taifex_fee_structure.md."""
    assert _TXF_RT_COST_PTS == 3.0


# ----------------------------------------------------------------------------
# Monotonic time ordering
# ----------------------------------------------------------------------------


def test_monotonic_timestamp_ordering_preserved() -> None:
    maker = TxfD6SoloMaker()
    t0 = time.monotonic_ns()
    maker.on_tick(_bidask(17500, 17505, ts_ns=t0))
    t1 = time.monotonic_ns()
    maker.on_tick(_bidask(17501, 17506, ts_ns=t1))
    assert t1 > t0
    EPOCH_THRESHOLD_NS = 100_000_000_000_000_000
    assert t0 < EPOCH_THRESHOLD_NS
    assert t1 < EPOCH_THRESHOLD_NS


# ----------------------------------------------------------------------------
# Degenerate books
# ----------------------------------------------------------------------------


def test_trade_event_returns_hold() -> None:
    maker = TxfD6SoloMaker()
    actions = maker.on_tick(_trade(17501, volume=3))
    assert actions == [Hold()]


def test_rejects_crossed_book() -> None:
    maker = TxfD6SoloMaker()
    actions = maker.on_tick(_bidask(17505, 17504))  # crossed
    assert [a for a in actions if isinstance(a, PostQuote)] == []


def test_rejects_zero_priced_book() -> None:
    maker = TxfD6SoloMaker()
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
    maker = TxfD6SoloMaker()
    maker.on_tick(_bidask(17500, 17505))
    maker.on_gap()
    # After gap, same prices can post again (price-gate cleared).
    actions = maker.on_tick(_bidask(17500, 17505))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_on_gap_preserves_position() -> None:
    """Gap must not mutate fill-tracked position."""
    maker = TxfD6SoloMaker()
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.position == 1
    maker.on_gap()
    assert maker.position == 1


def test_reset_clears_position_and_quotes() -> None:
    """Reset clears pos + counters. Use max_pos=3 so both sides can quote."""
    maker = TxfD6SoloMaker(params=C33Params(max_pos=3))
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    maker.on_tick(_bidask(17500, 17505))
    assert maker.position == 1
    assert maker.quotes_posted == 2
    maker.reset()
    assert maker.position == 0
    assert maker.quotes_posted == 0


# ----------------------------------------------------------------------------
# AlphaProtocol conformance
# ----------------------------------------------------------------------------


def test_c33_alpha_conforms_to_alpha_protocol() -> None:
    alpha = C33Alpha()
    assert isinstance(alpha, AlphaProtocol)
    assert alpha.manifest.alpha_id == "c33_txfd6_solo_passive_maker"
    assert alpha.manifest.strategy_type == "maker"
    sig = alpha.update(foo=1)
    assert isinstance(sig, float)
    assert alpha.reset() is None


def test_c33_manifest_declares_latency_profile() -> None:
    alpha = C33Alpha()
    assert alpha.manifest.latency_profile is not None
    assert alpha.manifest.latency_profile != ""


def test_c33_manifest_documents_instrument_txfd6() -> None:
    alpha = C33Alpha()
    assert alpha.manifest.instrument == "TXFD6"


def test_c33_hypothesis_cites_maker_full_cycle_framework() -> None:
    alpha = C33Alpha()
    h = alpha.manifest.hypothesis.upper()
    assert "FULL-CYCLE" in h or "FULL CYCLE" in h or "CYCLE" in h


def test_c33_hypothesis_cites_r47_minimal() -> None:
    alpha = C33Alpha()
    h = alpha.manifest.hypothesis.upper()
    assert "R47-MINIMAL" in h or "MINIMAL" in h


def test_c33_reset_clears_position() -> None:
    alpha = C33Alpha()
    alpha.maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    assert alpha.maker.position == 1
    alpha.reset()
    assert alpha.maker.position == 0


# ----------------------------------------------------------------------------
# Cycle tracking (DA-mandated close-side classification helper)
# ----------------------------------------------------------------------------


def test_position_decreases_on_sell_fill() -> None:
    """Close-side-reducing semantics: selling when long reduces |pos|."""
    maker = TxfD6SoloMaker(params=C33Params(max_pos=3))
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    maker.on_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.position == 2
    maker.on_fill("sell", 17_510 * _SCALE, 17505.5)
    assert maker.position == 1  # reduced by 1; would-be close fill partially
    maker.on_fill("sell", 17_510 * _SCALE, 17505.5)
    assert maker.position == 0  # fully flat; last fill closed the position
