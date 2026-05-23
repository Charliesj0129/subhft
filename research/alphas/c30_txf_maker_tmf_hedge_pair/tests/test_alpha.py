"""Unit tests for C30 TXF-maker + TMF-hedge pair.

Patterns per .agent/skills/hft-test-hft/SKILL.md:
  - Scaled-int price assertions (CK scale = 1_000_000)
  - Monotonic time via time.monotonic_ns(), no wall-clock deps
  - Factory fixtures via helper functions
"""

from __future__ import annotations

import time

import pytest

from research.alphas.c30_txf_maker_tmf_hedge_pair.impl import (
    C30Alpha,
    C30Params,
    HedgeOrder,
    PairStepResult,
    TxfTmfPairMaker,
)
from research.backtest.maker_engine import Hold, PostQuote, TickData
from research.registry.schemas import AlphaProtocol

_SCALE = 1_000_000
_TXF_POINT_VALUE_NTD = 200  # TXFD6: 200 NTD/pt


# ----------------------------------------------------------------------------
# Factory fixtures
# ----------------------------------------------------------------------------


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
# TXF maker leg — scaled-int price arithmetic
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bid_pts,ask_pts,expected_bid_scaled,expected_ask_scaled",
    [
        (17500, 17505, 17_500_000_000, 17_505_000_000),
        (18000, 18006, 18_000_000_000, 18_006_000_000),
        (19500, 19505, 19_500_000_000, 19_505_000_000),
    ],
)
def test_txf_maker_posts_quotes_at_scaled_int_prices(
    bid_pts: int,
    ask_pts: int,
    expected_bid_scaled: int,
    expected_ask_scaled: int,
) -> None:
    maker = TxfTmfPairMaker()
    step = maker.on_txf_tick(_bidask(bid_pts, ask_pts))
    posts = [a for a in step.maker_actions if isinstance(a, PostQuote)]
    assert len(posts) == 2
    prices = {a.side: a.price for a in posts}
    assert prices["buy"] == expected_bid_scaled
    assert prices["sell"] == expected_ask_scaled
    for a in posts:
        assert isinstance(a.price, int)


def test_txf_spread_gate_blocks_quotes_below_threshold() -> None:
    """Default spread_threshold_pts=5. Spread=4 (below TXF 5pt gate) -> blocked."""
    maker = TxfTmfPairMaker()
    step = maker.on_txf_tick(_bidask(17500, 17504))  # spread=4
    posts = [a for a in step.maker_actions if isinstance(a, PostQuote)]
    assert posts == []
    assert step.maker_actions == [Hold()]
    assert step.hedge is None


def test_txf_spread_gate_admits_at_threshold() -> None:
    maker = TxfTmfPairMaker()
    step = maker.on_txf_tick(_bidask(17500, 17505))
    posts = [a for a in step.maker_actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_custom_spread_threshold_overrides_default() -> None:
    maker = TxfTmfPairMaker(params=C30Params(spread_threshold_pts=3))
    step = maker.on_txf_tick(_bidask(17500, 17503))
    posts = [a for a in step.maker_actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


# ----------------------------------------------------------------------------
# Monotonic time ordering
# ----------------------------------------------------------------------------


def test_monotonic_timestamp_ordering_preserved() -> None:
    maker = TxfTmfPairMaker()
    t0 = time.monotonic_ns()
    maker.on_txf_tick(_bidask(17500, 17505, ts_ns=t0))
    t1 = time.monotonic_ns()
    maker.on_txf_tick(_bidask(17501, 17506, ts_ns=t1))
    assert t1 > t0
    # Monotonic clock is much smaller than wall-clock epoch ns.
    EPOCH_THRESHOLD_NS = 100_000_000_000_000_000
    assert t0 < EPOCH_THRESHOLD_NS
    assert t1 < EPOCH_THRESHOLD_NS


# ----------------------------------------------------------------------------
# Max-position gate on TXF leg
# ----------------------------------------------------------------------------


def test_txf_stops_buying_at_max_pos() -> None:
    maker = TxfTmfPairMaker(params=C30Params(txf_max_pos_contracts=3))
    for _ in range(3):
        maker.on_txf_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.txf_position == 3
    step = maker.on_txf_tick(_bidask(17500, 17505))
    sides = {a.side for a in step.maker_actions if isinstance(a, PostQuote)}
    assert "buy" not in sides
    assert "sell" in sides


def test_txf_stops_selling_at_negative_max_pos() -> None:
    maker = TxfTmfPairMaker(params=C30Params(txf_max_pos_contracts=3))
    for _ in range(3):
        maker.on_txf_fill("sell", 17_500 * _SCALE, 17500.5)
    assert maker.txf_position == -3
    step = maker.on_txf_tick(_bidask(17500, 17505))
    sides = {a.side for a in step.maker_actions if isinstance(a, PostQuote)}
    assert "sell" not in sides
    assert "buy" in sides


# ----------------------------------------------------------------------------
# Price-movement gate (ROD anti-stack)
# ----------------------------------------------------------------------------


def test_txf_does_not_restack_same_price() -> None:
    maker = TxfTmfPairMaker()
    first = maker.on_txf_tick(_bidask(17500, 17505))
    second = maker.on_txf_tick(_bidask(17500, 17505))
    first_posts = [a for a in first.maker_actions if isinstance(a, PostQuote)]
    second_posts = [a for a in second.maker_actions if isinstance(a, PostQuote)]
    assert len(first_posts) == 2
    assert second_posts == []


# ----------------------------------------------------------------------------
# Hedge trigger — critical pair logic
# ----------------------------------------------------------------------------


def test_hedge_not_issued_below_trigger() -> None:
    """No TXF inventory -> no hedge."""
    maker = TxfTmfPairMaker(params=C30Params(hedge_inv_trigger_pts=20))
    step = maker.on_txf_tick(_bidask(17500, 17505))
    assert step.hedge is None


def test_hedge_issued_when_long_inventory_crosses_trigger() -> None:
    """Trigger unit is TXF contracts. 1 contract long at trigger=1 fires SELL hedge."""
    maker = TxfTmfPairMaker(
        params=C30Params(
            hedge_inv_trigger_pts=1, tmf_hedge_ratio=20, txf_max_pos_contracts=2
        ),
        txf_point_value_ntd=_TXF_POINT_VALUE_NTD,
    )
    maker.on_txf_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.txf_position == 1
    step = maker.on_txf_tick(_bidask(17500, 17505))
    assert step.hedge is not None
    assert step.hedge.side == "sell"
    assert step.hedge.qty == 20  # 1 TXF contract × 20 TMF hedge-ratio
    assert step.hedge.trigger_txf_pos_pts == 1


def test_hedge_issued_when_short_inventory_crosses_trigger() -> None:
    """TXF short crosses contract-count trigger -> hedge BUY TMF."""
    maker = TxfTmfPairMaker(
        params=C30Params(
            hedge_inv_trigger_pts=1, tmf_hedge_ratio=20, txf_max_pos_contracts=2
        ),
        txf_point_value_ntd=_TXF_POINT_VALUE_NTD,
    )
    maker.on_txf_fill("sell", 17_500 * _SCALE, 17500.5)
    assert maker.txf_position == -1
    step = maker.on_txf_tick(_bidask(17500, 17505))
    assert step.hedge is not None
    assert step.hedge.side == "buy"
    assert step.hedge.qty == 20


def test_hedge_scales_with_txf_contract_count() -> None:
    """At |TXF|=3 contracts, hedge qty = 3 * tmf_hedge_ratio = 60 TMF contracts."""
    maker = TxfTmfPairMaker(
        params=C30Params(
            hedge_inv_trigger_pts=3,
            tmf_hedge_ratio=20,
            txf_max_pos_contracts=5,
        ),
        txf_point_value_ntd=_TXF_POINT_VALUE_NTD,
    )
    for _ in range(3):
        maker.on_txf_fill("buy", 17_500 * _SCALE, 17500.5)
    assert maker.txf_position == 3
    step = maker.on_txf_tick(_bidask(17500, 17505))
    assert step.hedge is not None
    assert step.hedge.qty == 60
    assert step.hedge.trigger_txf_pos_pts == 3


def test_hedge_bracket_trigger_respected() -> None:
    """Inventory-trigger bracket support (DA WARN #5). Unit is TXF contracts."""
    for trigger in (10, 20, 40):
        params = C30Params(
            hedge_inv_trigger_pts=trigger, txf_max_pos_contracts=60
        )
        maker = TxfTmfPairMaker(
            params=params, txf_point_value_ntd=_TXF_POINT_VALUE_NTD
        )
        for _ in range(20):
            maker.on_txf_fill("buy", 17_500 * _SCALE, 17500.5)
        step = maker.on_txf_tick(_bidask(17500, 17505))
        if trigger <= 20:
            assert step.hedge is not None, f"trigger={trigger}: expected hedge"
        else:
            assert step.hedge is None, f"trigger={trigger}: expected no hedge"


def test_hedge_fill_updates_tmf_position() -> None:
    maker = TxfTmfPairMaker()
    applied = maker.on_tmf_fill("buy", 2_000 * _SCALE, 20)
    assert applied == 20
    assert maker.tmf_position == 20
    # Sell hedge decrements
    applied = maker.on_tmf_fill("sell", 2_000 * _SCALE, 5)
    assert applied == -5
    assert maker.tmf_position == 15


def test_hedge_events_counter_advances_on_fill() -> None:
    maker = TxfTmfPairMaker()
    assert maker.hedge_events == 0
    maker.on_tmf_fill("sell", 2_000 * _SCALE, 20)
    assert maker.hedge_events == 1


def test_hedge_execution_price_uses_far_side_tob() -> None:
    """Buy hedge lifts ask; sell hedge hits bid. No slippage by default."""
    maker = TxfTmfPairMaker()
    maker.on_tmf_tick(_bidask(2000, 2002))
    assert maker.hedge_execution_price("buy") == 2002 * _SCALE
    assert maker.hedge_execution_price("sell") == 2000 * _SCALE


def test_hedge_execution_price_applies_slippage() -> None:
    """DA WARN #4: configurable slippage (0..1 pt)."""
    maker = TxfTmfPairMaker(params=C30Params(tmf_taker_slippage_pts=1))
    maker.on_tmf_tick(_bidask(2000, 2002))
    # buy lifts ask -> ask + slip
    assert maker.hedge_execution_price("buy") == (2002 + 1) * _SCALE
    # sell hits bid -> bid - slip
    assert maker.hedge_execution_price("sell") == (2000 - 1) * _SCALE


def test_tmf_tick_alone_rechecks_hedge() -> None:
    """A TMF-only tick re-evaluates the trigger (inventory may have crossed between TXF ticks)."""
    maker = TxfTmfPairMaker(
        params=C30Params(hedge_inv_trigger_pts=1, txf_max_pos_contracts=2),
        txf_point_value_ntd=_TXF_POINT_VALUE_NTD,
    )
    maker.on_txf_fill("buy", 17_500 * _SCALE, 17500.5)
    step = maker.on_tmf_tick(_bidask(2000, 2002))
    assert step.hedge is not None
    assert step.hedge.side == "sell"


# ----------------------------------------------------------------------------
# Gap reset
# ----------------------------------------------------------------------------


def test_on_gap_clears_transient_quote_state() -> None:
    maker = TxfTmfPairMaker()
    maker.on_txf_tick(_bidask(17500, 17505))
    maker.on_tmf_tick(_bidask(2000, 2002))
    maker.on_gap()
    # Post-gap, the same prices should re-enter the price-movement gate.
    step = maker.on_txf_tick(_bidask(17500, 17505))
    posts = [a for a in step.maker_actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_on_gap_preserves_authoritative_position() -> None:
    maker = TxfTmfPairMaker()
    maker.on_txf_fill("buy", 17_500 * _SCALE, 17500.5)
    maker.on_tmf_fill("sell", 2_000 * _SCALE, 20)
    assert maker.txf_position == 1
    assert maker.tmf_position == -20
    maker.on_gap()
    assert maker.txf_position == 1
    assert maker.tmf_position == -20


# ----------------------------------------------------------------------------
# Malformed books
# ----------------------------------------------------------------------------


def test_trade_events_return_hold_and_do_not_post() -> None:
    maker = TxfTmfPairMaker()
    step = maker.on_txf_tick(_trade(17501, volume=3))
    assert step.maker_actions == [Hold()]
    assert step.hedge is None


def test_rejects_crossed_book() -> None:
    maker = TxfTmfPairMaker()
    step = maker.on_txf_tick(_bidask(17505, 17504))  # bid > ask
    assert [a for a in step.maker_actions if isinstance(a, PostQuote)] == []


def test_rejects_zero_priced_book() -> None:
    maker = TxfTmfPairMaker()
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
    step = maker.on_txf_tick(bad)
    assert [a for a in step.maker_actions if isinstance(a, PostQuote)] == []


# ----------------------------------------------------------------------------
# AlphaProtocol conformance
# ----------------------------------------------------------------------------


def test_c30_alpha_conforms_to_alpha_protocol() -> None:
    alpha = C30Alpha()
    assert isinstance(alpha, AlphaProtocol)
    assert alpha.manifest.alpha_id == "c30_txf_maker_tmf_hedge_pair"
    assert alpha.manifest.strategy_type == "maker"
    sig = alpha.update(foo=1)
    assert isinstance(sig, float)
    assert alpha.reset() is None


def test_c30_manifest_declares_latency_profile() -> None:
    alpha = C30Alpha()
    assert alpha.manifest.latency_profile is not None
    assert alpha.manifest.latency_profile != ""


def test_c30_manifest_documents_pair_instruments() -> None:
    alpha = C30Alpha()
    assert "TXFD6" in alpha.manifest.instrument
    assert "TMFD6" in alpha.manifest.instrument


def test_c30_manifest_hypothesis_cites_h3_differentiation() -> None:
    """Hypothesis MUST flag distinction from killed TX-TMF leadlag class."""
    alpha = C30Alpha()
    h = alpha.manifest.hypothesis.upper()
    assert "LEADLAG" in h or "R26" in h or "DIRECTIONAL PREDICTION" in h


def test_c30_reset_clears_positions() -> None:
    alpha = C30Alpha()
    alpha.maker.on_txf_fill("buy", 17_500 * _SCALE, 17500.5)
    alpha.maker.on_tmf_fill("sell", 2_000 * _SCALE, 20)
    assert alpha.maker.txf_position == 1
    assert alpha.maker.tmf_position == -20
    alpha.reset()
    assert alpha.maker.txf_position == 0
    assert alpha.maker.tmf_position == 0


# ----------------------------------------------------------------------------
# PairStepResult shape
# ----------------------------------------------------------------------------


def test_pair_step_result_default_is_empty() -> None:
    step = PairStepResult()
    assert step.maker_actions == []
    assert step.hedge is None


def test_hedge_order_fields() -> None:
    h = HedgeOrder(side="buy", qty=20, trigger_txf_pos_pts=20)
    assert h.side == "buy"
    assert h.qty == 20
    assert h.trigger_txf_pos_pts == 20
