import numpy as np
import pytest

from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData
from hft_platform.feature.boundary import event_to_typed_frame, typed_frame_to_event
from hft_platform.feature.engine import FeatureEngine, QUALITY_FLAG_OUT_OF_ORDER, QUALITY_FLAG_STATE_RESET
from hft_platform.feature.registry import (
    build_default_lob_feature_set_v1,
    feature_id_to_index,
)


def _stats(symbol: str = "2330", ts: int = 1, bid: int = 1000000, ask: int = 1001000, bq: int = 10, aq: int = 20):
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=0.0,
        best_bid=bid,
        best_ask=ask,
        bid_depth=bq,
        ask_depth=aq,
    )


def test_feature_engine_emits_default_feature_update():
    eng = FeatureEngine()
    evt = eng.process_lob_stats(_stats(), local_ts_ns=2)
    assert evt is not None
    assert evt.feature_set_id == "lob_shared_v1"
    assert evt.schema_version == 1
    assert evt.seq == 1
    assert evt.symbol == "2330"
    assert evt.ts == 1
    assert evt.local_ts == 2
    assert evt.changed_mask != 0
    assert evt.warmup_ready_mask != 0
    assert "spread_scaled" in evt.feature_ids
    assert evt.get("spread_scaled") == 1000
    assert evt.get("ofi_l1_raw") == 0
    assert eng.get_feature("2330", "spread_scaled") == 1000
    view = eng.get_feature_view("2330")
    assert view is not None
    assert view["feature_set_id"] == "lob_shared_v1"


def test_feature_engine_changed_mask_and_reset_flag():
    eng = FeatureEngine()
    first = eng.process_lob_stats(_stats(ts=10), local_ts_ns=10)
    second = eng.process_lob_stats(_stats(ts=11), local_ts_ns=11)
    assert first is not None and second is not None
    # same inputs except timestamps => feature values unchanged
    assert second.changed_mask == 0

    eng.reset_symbol("2330")
    third = eng.process_lob_stats(_stats(ts=12, bid=1000100, ask=1001100), local_ts_ns=12)
    assert third is not None
    assert third.quality_flags & QUALITY_FLAG_STATE_RESET
    assert third.changed_mask != 0


def _bidask(symbol: str, ts: int, bid_px: int, bid_qty: int, ask_px: int, ask_qty: int) -> BidAskEvent:
    return BidAskEvent(
        meta=MetaData(seq=ts, source_ts=ts, local_ts=ts, topic="test"),
        symbol=symbol,
        bids=np.asarray([[bid_px, bid_qty]], dtype=np.int64),
        asks=np.asarray([[ask_px, ask_qty]], dtype=np.int64),
        is_snapshot=False,
    )


def test_feature_engine_process_lob_update_uses_l1_qty_and_computes_ofi_sequence():
    eng = FeatureEngine()
    symbol = "2330"

    e1 = _bidask(symbol, 1, 1000000, 10, 1001000, 20)
    s1 = LOBStatsEvent(symbol=symbol, ts=1, imbalance=0.0, best_bid=1000000, best_ask=1001000, bid_depth=100, ask_depth=200)
    u1 = eng.process_lob_update(e1, s1, local_ts_ns=1)
    assert u1 is not None
    assert u1.get("l1_bid_qty") == 10
    assert u1.get("l1_ask_qty") == 20
    assert u1.get("ofi_l1_raw") == 0

    # Same prices, bid qty up (+5), ask qty down (-5) -> OFI raw positive
    e2 = _bidask(symbol, 2, 1000000, 15, 1001000, 15)
    s2 = LOBStatsEvent(symbol=symbol, ts=2, imbalance=0.0, best_bid=1000000, best_ask=1001000, bid_depth=110, ask_depth=190)
    u2 = eng.process_lob_update(e2, s2, local_ts_ns=2)
    assert u2 is not None
    # b_flow = +5; a_flow = -5 => ofi = 10
    assert u2.get("ofi_l1_raw") == 10
    assert u2.get("ofi_l1_cum") == 10
    assert u2.get("ofi_l1_ema8") != 0
    assert u2.get("depth_imbalance_ema8_ppm") is not None
    assert u2.get("spread_ema8_scaled") == 1000

    # Bid price up -> b_flow uses current bid qty, ask unchanged qty delta=0
    e3 = _bidask(symbol, 3, 1000100, 12, 1001000, 15)
    s3 = LOBStatsEvent(symbol=symbol, ts=3, imbalance=0.0, best_bid=1000100, best_ask=1001000, bid_depth=120, ask_depth=190)
    u3 = eng.process_lob_update(e3, s3, local_ts_ns=3)
    assert u3 is not None
    assert u3.get("ofi_l1_raw") == 12
    assert u3.get("ofi_l1_cum") == 22


def test_feature_engine_out_of_order_flag():
    eng = FeatureEngine()
    eng.process_lob_stats(_stats(ts=10), local_ts_ns=10)
    evt = eng.process_lob_stats(_stats(ts=9), local_ts_ns=9)
    assert evt is not None
    assert evt.quality_flags & QUALITY_FLAG_OUT_OF_ORDER


# --- Sprint 1 additional tests ---

def test_stateless_features_from_lob_stats_event():
    eng = FeatureEngine()
    stats = _stats(bid=1000000, ask=1001000, bq=100, aq=200)
    eng.process_lob_stats(stats)
    assert eng.get_feature("2330", "best_bid") == 1000000
    assert eng.get_feature("2330", "best_ask") == 1001000
    assert eng.get_feature("2330", "spread_scaled") == 1000
    assert eng.get_feature("2330", "mid_price_x2") == 2001000
    assert eng.get_feature("2330", "bid_depth") == 100
    assert eng.get_feature("2330", "ask_depth") == 200


def test_depth_imbalance_ppm_bid_heavy():
    eng = FeatureEngine()
    # bid=300, ask=100 -> (300-100)/(300+100)*1e6 = 200/400*1e6 = 500_000
    eng.process_lob_stats(_stats(bq=300, aq=100))
    val = eng.get_feature("2330", "depth_imbalance_ppm")
    assert val is not None
    assert val == 500_000


def test_depth_imbalance_ppm_balanced():
    eng = FeatureEngine()
    # bid==ask -> 0
    eng.process_lob_stats(_stats(bq=100, aq=100))
    val = eng.get_feature("2330", "depth_imbalance_ppm")
    assert val == 0


def test_microprice_x2_weighted():
    eng = FeatureEngine()
    # Use l1 qty from BidAskEvent: bid=10, ask=10 => microprice = mid
    symbol = "2330"
    e = _bidask(symbol, 1, 1000000, 10, 1001000, 10)
    s = LOBStatsEvent(symbol=symbol, ts=1, imbalance=0.0, best_bid=1000000, best_ask=1001000, bid_depth=100, ask_depth=100)
    eng.process_lob_update(e, s)
    # equal weights -> microprice_x2 == mid_price_x2 == 2001000
    assert eng.get_feature(symbol, "microprice_x2") == 2001000


def test_microprice_x2_zero_depth_fallback():
    eng = FeatureEngine()
    # No l1 qty info (stats_only mode with bid_depth=0, ask_depth=0)
    stats = LOBStatsEvent(symbol="2330", ts=1, imbalance=0.0, best_bid=1000000, best_ask=1001000, bid_depth=0, ask_depth=0)
    eng.process_lob_stats(stats)
    # Falls back to mid_price_x2
    mid_x2 = eng.get_feature("2330", "mid_price_x2")
    micro_x2 = eng.get_feature("2330", "microprice_x2")
    assert micro_x2 == mid_x2


def test_get_feature_by_index():
    eng = FeatureEngine()
    eng.process_lob_stats(_stats(bid=1000000, ask=1001000))
    # index 0 = best_bid per feature set v1
    fs = build_default_lob_feature_set_v1()
    best_bid_idx = feature_id_to_index(fs, "best_bid")
    val = eng.get_feature_by_index("2330", best_bid_idx)
    assert val == 1000000


def test_get_feature_by_index_out_of_range():
    eng = FeatureEngine()
    eng.process_lob_stats(_stats())
    # Out-of-range index returns None
    assert eng.get_feature_by_index("2330", 9999) is None
    # Negative index returns None
    assert eng.get_feature_by_index("2330", -1) is None


def test_get_feature_tuple_length():
    eng = FeatureEngine()
    eng.process_lob_stats(_stats())
    tpl = eng.get_feature_tuple("2330")
    assert tpl is not None
    fs = build_default_lob_feature_set_v1()
    assert len(tpl) == len(fs.features)


def test_unknown_symbol_returns_none():
    eng = FeatureEngine()
    assert eng.get_feature("NOSYM", "spread_scaled") is None
    assert eng.get_feature_by_index("NOSYM", 0) is None
    assert eng.get_feature_tuple("NOSYM") is None
    assert eng.get_feature_view("NOSYM") is None


def test_reset_clears_values():
    eng = FeatureEngine()
    eng.process_lob_stats(_stats(ts=1))
    assert eng.get_feature("2330", "best_bid") is not None
    eng.reset_symbol("2330")
    # After reset, state is gone until next update
    assert eng.get_feature("2330", "best_bid") is None
    assert eng.get_feature_tuple("2330") is None


def test_quality_flag_on_reset():
    eng = FeatureEngine()
    eng.process_lob_stats(_stats(ts=1))
    eng.reset_symbol("2330")
    evt = eng.process_lob_stats(_stats(ts=2))
    assert evt is not None
    assert evt.quality_flags & QUALITY_FLAG_STATE_RESET


def test_feature_id_to_index_helper():
    fs = build_default_lob_feature_set_v1()
    assert feature_id_to_index(fs, "best_bid") == 0
    assert feature_id_to_index(fs, "best_ask") == 1
    assert feature_id_to_index(fs, "spread_scaled") == 3
    with pytest.raises(KeyError):
        feature_id_to_index(fs, "nonexistent_feature")


class _ParityRefState:
    __slots__ = ("prev_bid", "prev_ask", "prev_bq", "prev_aq", "ofi_cum", "ofi_ema", "spread_ema", "imb_ema", "init")

    def __init__(self) -> None:
        self.prev_bid = 0
        self.prev_ask = 0
        self.prev_bq = 0
        self.prev_aq = 0
        self.ofi_cum = 0
        self.ofi_ema = 0.0
        self.spread_ema = 0.0
        self.imb_ema = 0.0
        self.init = False


def _ref_values(state: _ParityRefState, evt: BidAskEvent, stats: LOBStatsEvent) -> tuple[int, ...]:
    bid = int(stats.best_bid)
    ask = int(stats.best_ask)
    bid_depth = int(stats.bid_depth)
    ask_depth = int(stats.ask_depth)
    bq = int(evt.bids[0][1])
    aq = int(evt.asks[0][1])
    mid_x2 = int(stats.mid_price_x2 or (bid + ask))
    spread = int(stats.spread_scaled or (ask - bid))

    depth_total = bid_depth + ask_depth
    imbalance_ppm = int(round(((bid_depth - ask_depth) * 1_000_000.0) / depth_total)) if depth_total > 0 else 0
    l1_total = bq + aq
    if l1_total > 0:
        l1_imbalance_ppm = int(round(((bq - aq) * 1_000_000.0) / l1_total))
        microprice_x2 = int(round((2.0 * ((ask * bq) + (bid * aq))) / l1_total))
    else:
        l1_imbalance_ppm = 0
        microprice_x2 = mid_x2

    if not state.init:
        ofi_raw = 0
        ofi_cum = 0
        ofi_ema8 = 0
        state.spread_ema = float(spread)
        state.imb_ema = float(l1_imbalance_ppm)
        spread_ema8 = int(round(state.spread_ema))
        imbalance_ema8 = int(round(state.imb_ema))
        state.init = True
    else:
        if bid > state.prev_bid:
            b_flow = bq
        elif bid == state.prev_bid:
            b_flow = bq - state.prev_bq
        else:
            b_flow = -state.prev_bq
        if ask > state.prev_ask:
            a_flow = -state.prev_aq
        elif ask == state.prev_ask:
            a_flow = aq - state.prev_aq
        else:
            a_flow = aq
        ofi_raw = int(b_flow - a_flow)
        state.ofi_cum += ofi_raw
        alpha = 2.0 / 9.0
        state.ofi_ema = (1.0 - alpha) * state.ofi_ema + alpha * float(ofi_raw)
        state.spread_ema = (1.0 - alpha) * state.spread_ema + alpha * float(spread)
        state.imb_ema = (1.0 - alpha) * state.imb_ema + alpha * float(l1_imbalance_ppm)
        ofi_cum = int(state.ofi_cum)
        ofi_ema8 = int(round(state.ofi_ema))
        spread_ema8 = int(round(state.spread_ema))
        imbalance_ema8 = int(round(state.imb_ema))

    state.prev_bid = bid
    state.prev_ask = ask
    state.prev_bq = bq
    state.prev_aq = aq

    return (
        bid,
        ask,
        mid_x2,
        spread,
        bid_depth,
        ask_depth,
        imbalance_ppm,
        microprice_x2,
        bq,
        aq,
        l1_imbalance_ppm,
        int(ofi_raw),
        int(ofi_cum),
        int(ofi_ema8),
        int(spread_ema8),
        int(imbalance_ema8),
    )


def test_feature_engine_reference_parity_random_sequence():
    rng = np.random.default_rng(20260224)
    eng = FeatureEngine()
    ref = _ParityRefState()
    bid = 1_000_000
    ask = 1_001_000
    bq = 10
    aq = 12

    for i in range(200):
        bid += int(rng.choice([-100, 0, 100]))
        ask = max(bid + 100, ask + int(rng.choice([-100, 0, 100])))
        bq = max(1, bq + int(rng.choice([-2, -1, 0, 1, 2])))
        aq = max(1, aq + int(rng.choice([-2, -1, 0, 1, 2])))
        evt = _bidask("2330", i + 1, bid, bq, ask, aq)
        stats = LOBStatsEvent(
            symbol="2330",
            ts=i + 1,
            imbalance=0.0,
            best_bid=bid,
            best_ask=ask,
            bid_depth=max(bq, bq + 5),
            ask_depth=max(aq, aq + 5),
        )
        got = eng.process_lob_update(evt, stats, local_ts_ns=i + 1)
        assert got is not None
        assert got.values == _ref_values(ref, evt, stats)


def test_feature_update_event_typed_frame_roundtrip():
    eng = FeatureEngine()
    evt = eng.process_lob_stats(_stats(ts=1), local_ts_ns=2)
    assert evt is not None
    frame = event_to_typed_frame(evt)
    assert frame.marker == "feature_update_v1"
    assert frame.feature_set_id == evt.feature_set_id
    evt2 = typed_frame_to_event(frame)
    assert evt2.symbol == evt.symbol
    assert evt2.seq == evt.seq
    assert evt2.feature_ids == evt.feature_ids
    assert evt2.values == evt.values


def test_feature_engine_rust_backend_parity_when_available():
    py_eng = FeatureEngine(kernel_backend="python")
    rust_eng = FeatureEngine(kernel_backend="rust")
    if rust_eng.kernel_backend() != "rust":
        pytest.skip("Rust LobFeatureKernelV1 unavailable")

    rng = np.random.default_rng(20260224)
    bid = 1_000_000
    ask = 1_001_000
    bq = 10
    aq = 12
    for i in range(200):
        bid += int(rng.choice([-100, 0, 100]))
        ask = max(bid + 100, ask + int(rng.choice([-100, 0, 100])))
        bq = max(1, bq + int(rng.choice([-2, -1, 0, 1, 2])))
        aq = max(1, aq + int(rng.choice([-2, -1, 0, 1, 2])))
        evt = _bidask("2330", i + 1, bid, bq, ask, aq)
        stats = LOBStatsEvent(
            symbol="2330",
            ts=i + 1,
            imbalance=0.0,
            best_bid=bid,
            best_ask=ask,
            bid_depth=max(bq, bq + 5),
            ask_depth=max(aq, aq + 5),
        )
        p = py_eng.process_lob_update(evt, stats, local_ts_ns=i + 1)
        r = rust_eng.process_lob_update(evt, stats, local_ts_ns=i + 1)
        assert p is not None and r is not None
        assert r.values == p.values
