import numpy as np
import pytest

from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData
from hft_platform.feature.boundary import event_to_typed_frame, typed_frame_to_event
from hft_platform.feature.engine import (
    QUALITY_FLAG_OUT_OF_ORDER,
    QUALITY_FLAG_PARTIAL,
    QUALITY_FLAG_STATE_RESET,
    FeatureEngine,
    _LobKernelState,
)
from hft_platform.feature.registry import (
    build_default_lob_feature_set_v1,
    build_default_lob_feature_set_v2,
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
    assert evt.feature_set_id == "lob_shared_v3"
    assert evt.schema_version == 3
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
    assert view["feature_set_id"] == "lob_shared_v3"


def test_feature_engine_v1_explicit():
    """v1 feature set still works when explicitly selected."""
    eng = FeatureEngine(feature_set_id="lob_shared_v1")
    evt = eng.process_lob_stats(_stats(), local_ts_ns=2)
    assert evt is not None
    assert evt.feature_set_id == "lob_shared_v1"
    assert evt.schema_version == 1
    assert len(evt.values) == 16


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
    s1 = LOBStatsEvent(
        symbol=symbol, ts=1, imbalance=0.0, best_bid=1000000, best_ask=1001000, bid_depth=100, ask_depth=200
    )
    u1 = eng.process_lob_update(e1, s1, local_ts_ns=1)
    assert u1 is not None
    assert u1.get("l1_bid_qty") == 10
    assert u1.get("l1_ask_qty") == 20
    assert u1.get("ofi_l1_raw") == 0

    # Same prices, bid qty up (+5), ask qty down (-5) -> OFI raw positive
    e2 = _bidask(symbol, 2, 1000000, 15, 1001000, 15)
    s2 = LOBStatsEvent(
        symbol=symbol, ts=2, imbalance=0.0, best_bid=1000000, best_ask=1001000, bid_depth=110, ask_depth=190
    )
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
    s3 = LOBStatsEvent(
        symbol=symbol, ts=3, imbalance=0.0, best_bid=1000100, best_ask=1001000, bid_depth=120, ask_depth=190
    )
    u3 = eng.process_lob_update(e3, s3, local_ts_ns=3)
    assert u3 is not None
    assert u3.get("ofi_l1_raw") == 12
    assert u3.get("ofi_l1_cum") == 22


def test_feature_engine_out_of_order_flag():
    eng = FeatureEngine()
    eng.process_lob_stats(_stats(ts=10), local_ts_ns=10)
    evt = eng.process_lob_stats(_stats(ts=9), local_ts_ns=9)
    # OOO events are now skipped (return None) to prevent stale data overwriting state
    assert evt is None


def test_ooo_event_does_not_overwrite_state():
    """OOO event must not overwrite prev state — values, seq, ts must remain from the newer tick."""
    eng = FeatureEngine()
    # First tick: ts=100, specific prices
    s1 = _stats(ts=100, bid=1000000, ask=1002000, bq=50, aq=60)
    evt1 = eng.process_lob_stats(s1, local_ts_ns=100)
    assert evt1 is not None
    prev_values = evt1.values
    prev_seq = evt1.seq
    prev_ts = evt1.ts

    # Second tick: ts=50 (OOO — older than first), different prices
    s2 = _stats(ts=50, bid=900000, ask=910000, bq=10, aq=20)
    evt2 = eng.process_lob_stats(s2, local_ts_ns=50)
    assert evt2 is None  # must be skipped

    # State must still reflect the first (newer) tick
    state = eng._states.get("2330")
    assert state is not None
    assert state.source_ts_ns == 100
    assert state.seq == prev_seq
    assert state.values == prev_values

    # Subsequent in-order tick must work normally
    s3 = _stats(ts=200, bid=1010000, ask=1012000, bq=55, aq=65)
    evt3 = eng.process_lob_stats(s3, local_ts_ns=200)
    assert evt3 is not None
    assert evt3.ts == 200


def test_normalizer_seq_flows_through_lob_to_feature():
    """normalizer_seq from LOBStatsEvent must be stored in _FeatureState and used for OOO detection."""
    eng = FeatureEngine()
    # First event with normalizer_seq=100
    s1 = LOBStatsEvent(
        symbol="2330", ts=1000, imbalance=0.0,
        best_bid=1000000, best_ask=1001000,
        bid_depth=10, ask_depth=20,
        normalizer_seq=100,
    )
    evt1 = eng.process_lob_stats(s1, local_ts_ns=1000)
    assert evt1 is not None
    state = eng._states["2330"]
    assert state.normalizer_seq == 100

    # Second event with higher ts BUT lower normalizer_seq → OOO by seq
    s2 = LOBStatsEvent(
        symbol="2330", ts=2000, imbalance=0.0,
        best_bid=1000000, best_ask=1001000,
        bid_depth=10, ask_depth=20,
        normalizer_seq=50,
    )
    evt2 = eng.process_lob_stats(s2, local_ts_ns=2000)
    assert evt2 is None  # OOO detected via normalizer_seq
    assert state.normalizer_seq == 100  # unchanged

    # Third event with higher normalizer_seq → accepted
    s3 = LOBStatsEvent(
        symbol="2330", ts=3000, imbalance=0.0,
        best_bid=1010000, best_ask=1011000,
        bid_depth=15, ask_depth=25,
        normalizer_seq=200,
    )
    evt3 = eng.process_lob_stats(s3, local_ts_ns=3000)
    assert evt3 is not None
    assert eng._states["2330"].normalizer_seq == 200


def test_crossed_book_emits_partial_flag():
    """Crossed/empty book (mid_price_x2=0) must emit FeatureUpdateEvent with PARTIAL flag and prev values."""
    eng = FeatureEngine()
    # First: normal event to establish state
    s1 = _stats(ts=100, bid=1000000, ask=1001000, bq=50, aq=60)
    evt1 = eng.process_lob_stats(s1, local_ts_ns=100)
    assert evt1 is not None
    prev_values = evt1.values

    # Second: crossed book (mid_price_x2=0)
    s2 = LOBStatsEvent(
        symbol="2330", ts=200, imbalance=0.0,
        best_bid=0, best_ask=0,
        bid_depth=0, ask_depth=0,
        mid_price_x2=0, spread_scaled=0,
    )
    evt2 = eng.process_lob_stats(s2, local_ts_ns=200)
    assert evt2 is not None, "crossed book must emit event (not None) when prev state exists"
    assert evt2.quality_flags & QUALITY_FLAG_PARTIAL, "PARTIAL flag must be set"
    assert evt2.values == prev_values, "values must be stale re-emit of previous"
    assert evt2.changed_mask == 0, "no features changed (stale re-emit)"
    assert evt2.ts == 200, "timestamp must advance"

    # Verify state advanced (seq increased)
    state = eng._states["2330"]
    assert state.source_ts_ns == 200

    # Third: normal event should still work
    s3 = _stats(ts=300, bid=1010000, ask=1012000, bq=55, aq=65)
    evt3 = eng.process_lob_stats(s3, local_ts_ns=300)
    assert evt3 is not None
    assert not (evt3.quality_flags & QUALITY_FLAG_PARTIAL)


def test_crossed_book_no_prev_returns_none():
    """Crossed book with no previous state must return None."""
    eng = FeatureEngine()
    s = LOBStatsEvent(
        symbol="NEW", ts=100, imbalance=0.0,
        best_bid=0, best_ask=0,
        bid_depth=0, ask_depth=0,
        mid_price_x2=0, spread_scaled=0,
    )
    evt = eng.process_lob_stats(s, local_ts_ns=100)
    assert evt is None


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
    s = LOBStatsEvent(
        symbol=symbol, ts=1, imbalance=0.0, best_bid=1000000, best_ask=1001000, bid_depth=100, ask_depth=100
    )
    eng.process_lob_update(e, s)
    # equal weights -> microprice_x2 == mid_price_x2 == 2001000
    assert eng.get_feature(symbol, "microprice_x2") == 2001000


def test_microprice_x2_zero_depth_fallback():
    eng = FeatureEngine()
    # No l1 qty info (stats_only mode with bid_depth=0, ask_depth=0)
    stats = LOBStatsEvent(
        symbol="2330", ts=1, imbalance=0.0, best_bid=1000000, best_ask=1001000, bid_depth=0, ask_depth=0
    )
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
    # Default is v3 — verify count matches registry definition
    from hft_platform.feature.registry import build_default_lob_feature_set_v3

    fs = build_default_lob_feature_set_v3()
    assert len(tpl) == len(fs.features)

    # v1 explicit: 16 features
    eng_v1 = FeatureEngine(feature_set_id="lob_shared_v1")
    eng_v1.process_lob_stats(_stats())
    tpl_v1 = eng_v1.get_feature_tuple("2330")
    assert tpl_v1 is not None
    assert len(tpl_v1) == 16


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
    eng = FeatureEngine(feature_set_id="lob_shared_v1")
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
        # Compare v1 features (first 16) — v2 features (ISS/MLDM) appended after
        assert got.values[:16] == _ref_values(ref, evt, stats)


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


def _stats_tuple(
    symbol: str = "2330",
    ts: int = 1,
    bid: int = 1000000,
    ask: int = 1001000,
    bq: int = 10,
    aq: int = 20,
) -> tuple:
    """Build a stats tuple matching BookState.get_stats_tuple() layout (tagged)."""
    mid_x2 = bid + ask
    spread = ask - bid
    total = bq + aq
    imbalance = (bq - aq) / total if total > 0 else 0.0
    return ("lobstats", symbol, ts, mid_x2, spread, imbalance, bid, ask, bq, aq)


def test_feature_engine_accepts_stats_tuple():
    """process_lob_update should accept a raw tuple and produce identical results to LOBStatsEvent."""
    eng_event = FeatureEngine()
    eng_tuple = FeatureEngine()

    bid, ask, bq, aq = 1000000, 1001000, 10, 20
    stats_event = _stats(ts=1, bid=bid, ask=ask, bq=bq, aq=aq)
    stats_tup = _stats_tuple(ts=1, bid=bid, ask=ask, bq=bq, aq=aq)

    evt1 = eng_event.process_lob_update(None, stats_event, local_ts_ns=1)
    evt2 = eng_tuple.process_lob_update(None, stats_tup, local_ts_ns=1)

    assert evt1 is not None and evt2 is not None
    assert evt1.values == evt2.values
    assert evt1.symbol == evt2.symbol
    assert evt1.ts == evt2.ts


def test_feature_engine_tuple_multi_tick_sequence():
    """Verify that a multi-tick sequence via tuple input produces correct OFI accumulation."""
    eng = FeatureEngine()

    # Tick 1: initial
    t1 = _stats_tuple(ts=1, bid=1000000, ask=1001000, bq=10, aq=20)
    u1 = eng.process_lob_update(None, t1, local_ts_ns=1)
    assert u1 is not None
    assert u1.get("ofi_l1_raw") == 0  # First tick, no OFI

    # Tick 2: same prices, bid qty up, ask qty down
    t2 = _stats_tuple(ts=2, bid=1000000, ask=1001000, bq=15, aq=15)
    u2 = eng.process_lob_update(None, t2, local_ts_ns=2)
    assert u2 is not None
    # b_flow = 15-10 = 5; a_flow = 15-20 = -5; ofi = 5-(-5) = 10
    assert u2.get("ofi_l1_raw") == 10
    assert u2.get("ofi_l1_cum") == 10


def test_feature_state_in_place_mutation():
    """Verify that _FeatureState is pre-allocated once and mutated in-place on subsequent ticks."""
    eng = FeatureEngine()

    # First tick creates the state
    eng.process_lob_update(None, _stats(ts=1), local_ts_ns=1)
    state1 = eng._states.get("2330")
    assert state1 is not None
    state1_id = id(state1)

    # Second tick should reuse the same _FeatureState object
    eng.process_lob_update(None, _stats(ts=2, bid=1000100, ask=1001100), local_ts_ns=2)
    state2 = eng._states.get("2330")
    assert state2 is not None
    assert id(state2) == state1_id  # Same object, mutated in-place
    assert state2.seq == 2
    assert state2.source_ts_ns == 2

    # Third tick: still same object
    eng.process_lob_update(None, _stats(ts=3, bid=1000200, ask=1001200), local_ts_ns=3)
    state3 = eng._states.get("2330")
    assert id(state3) == state1_id
    assert state3.warm_count == 3


def test_feature_state_fresh_after_reset():
    """After reset_symbol, warm_count restarts at 1 (fresh state)."""
    eng = FeatureEngine()
    eng.process_lob_update(None, _stats(ts=1), local_ts_ns=1)
    eng.process_lob_update(None, _stats(ts=2), local_ts_ns=2)
    state_before = eng._states["2330"]
    assert state_before.warm_count == 2

    eng.reset_symbol("2330")
    assert "2330" not in eng._states  # State cleared

    eng.process_lob_update(None, _stats(ts=3), local_ts_ns=3)
    state_after = eng._states["2330"]
    # After reset, warm_count restarts at 1
    assert state_after.warm_count == 1
    assert state_after.seq == 3


def test_feature_engine_rust_backend_parity_when_available():
    py_eng = FeatureEngine(feature_set_id="lob_shared_v1", kernel_backend="python")
    rust_eng = FeatureEngine(feature_set_id="lob_shared_v1", kernel_backend="rust")
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
        # Rust kernel produces v1 (16 features); Python produces v2 (21).
        # Compare first 16 for parity.
        assert r.values[:16] == p.values[:16]


# --- v2 features tests ---


def test_v2_feature_set_has_22_features():
    fs = build_default_lob_feature_set_v2()
    assert len(fs.features) == 22
    assert fs.feature_set_id == "lob_shared_v2"
    assert fs.schema_version == 2
    assert fs.features[16].feature_id == "ofi_depth_norm_ppm"
    assert fs.features[17].feature_id == "ret_autocov_5s_x1e6"
    assert fs.features[18].feature_id == "tob_survival_ms"
    assert fs.features[19].feature_id == "impact_surprise_x1000"
    assert fs.features[20].feature_id == "deep_depth_momentum_x1000"
    assert fs.features[21].feature_id == "toxicity_ema50_x1000"


def test_v2_ofi_depth_norm_basic():
    """ofi_depth_norm_ppm = ofi_ema8 * 1e6 / avg_l1_depth."""
    eng = FeatureEngine()  # default is v2
    sym = "TXFD6"
    # Tick 1: initialize
    e1 = _bidask(sym, 1, 1000000, 50, 1001000, 50)
    s1 = LOBStatsEvent(symbol=sym, ts=1, imbalance=0.0, best_bid=1000000, best_ask=1001000, bid_depth=50, ask_depth=50)
    eng.process_lob_update(e1, s1, local_ts_ns=1)

    # Tick 2: bid qty up by 10 -> positive OFI
    e2 = _bidask(sym, 2, 1000000, 60, 1001000, 50)
    s2 = LOBStatsEvent(symbol=sym, ts=2, imbalance=0.0, best_bid=1000000, best_ask=1001000, bid_depth=60, ask_depth=50)
    eng.process_lob_update(e2, s2, local_ts_ns=2)

    ofi_ema = eng.get_feature(sym, "ofi_l1_ema8")
    depth_norm = eng.get_feature(sym, "ofi_depth_norm_ppm")
    assert ofi_ema is not None
    assert depth_norm is not None
    # Depth norm uses float ofi_ema8 internally (not the rounded int).
    # Just verify sign and order of magnitude.
    assert depth_norm > 0  # positive OFI -> positive normalized OFI
    # avg_depth = (60+50)/2 = 55. For ofi_raw=10, ema8 alpha=2/9:
    # ema = 10 * 2/9 ≈ 2.22, depth_norm ≈ 2.22 * 1e6 / 55 ≈ 40404
    assert 30_000 < depth_norm < 60_000


def test_v2_ofi_depth_norm_zero_depth():
    """ofi_depth_norm_ppm = 0 when depth is 0."""
    eng = FeatureEngine()
    sym = "TXFD6"
    s = LOBStatsEvent(symbol=sym, ts=1, imbalance=0.0, best_bid=1000000, best_ask=1001000, bid_depth=0, ask_depth=0)
    eng.process_lob_stats(s)
    assert eng.get_feature(sym, "ofi_depth_norm_ppm") == 0


def test_v2_ret_autocov_needs_warmup():
    """ret_autocov_5s_x1e6 should be 0 until enough data points (>= 3 ticks)."""
    eng = FeatureEngine()
    sym = "TXFD6"
    for i in range(3):
        s = LOBStatsEvent(
            symbol=sym,
            ts=i + 1,
            imbalance=0.0,
            best_bid=1000000 + i * 100,
            best_ask=1001000 + i * 100,
            bid_depth=50,
            ask_depth=50,
        )
        eng.process_lob_stats(s)
    # After 3 ticks, autocov should be computed
    autocov = eng.get_feature(sym, "ret_autocov_5s_x1e6")
    assert autocov is not None


def test_v2_ret_autocov_oscillating_is_negative():
    """Oscillating prices (up-down-up-down) should produce negative autocovariance."""
    eng = FeatureEngine()
    sym = "TXFD6"
    prices = [1000000, 1001000, 1000000, 1001000, 1000000, 1001000, 1000000, 1001000]
    for i, px in enumerate(prices):
        s = LOBStatsEvent(
            symbol=sym,
            ts=i + 1,
            imbalance=0.0,
            best_bid=px,
            best_ask=px + 1000,
            bid_depth=50,
            ask_depth=50,
        )
        eng.process_lob_stats(s)
    autocov = eng.get_feature(sym, "ret_autocov_5s_x1e6")
    assert autocov is not None
    assert autocov < 0  # Oscillating = negative autocovariance


def test_v2_ret_autocov_trending_is_positive():
    """Monotonically increasing prices should produce positive autocovariance."""
    eng = FeatureEngine()
    sym = "TXFD6"
    for i in range(10):
        px = 1000000 + i * 100
        s = LOBStatsEvent(
            symbol=sym,
            ts=i + 1,
            imbalance=0.0,
            best_bid=px,
            best_ask=px + 1000,
            bid_depth=50,
            ask_depth=50,
        )
        eng.process_lob_stats(s)
    autocov = eng.get_feature(sym, "ret_autocov_5s_x1e6")
    assert autocov is not None
    assert autocov > 0  # Trending = positive autocovariance


def test_v2_tob_survival_increases():
    """tob_survival_ms should increase when best price stays the same."""
    eng = FeatureEngine()
    sym = "TXFD6"
    base_ns = 1_000_000_000  # 1 second in ns
    # Tick 1 & 2: same price, 500ms apart
    s1 = LOBStatsEvent(
        symbol=sym,
        ts=base_ns,
        imbalance=0.0,
        best_bid=1000000,
        best_ask=1001000,
        bid_depth=50,
        ask_depth=50,
    )
    eng.process_lob_stats(s1)
    s2 = LOBStatsEvent(
        symbol=sym,
        ts=base_ns + 500_000_000,
        imbalance=0.0,
        best_bid=1000000,
        best_ask=1001000,
        bid_depth=50,
        ask_depth=50,
    )
    eng.process_lob_stats(s2)
    survival = eng.get_feature(sym, "tob_survival_ms")
    assert survival is not None
    assert survival == 500  # 500ms since last change


def test_v2_tob_survival_resets_on_price_change():
    """tob_survival_ms should reset to 0 when best price changes."""
    eng = FeatureEngine()
    sym = "TXFD6"
    base_ns = 1_000_000_000
    # Tick 1: initial
    s1 = LOBStatsEvent(
        symbol=sym,
        ts=base_ns,
        imbalance=0.0,
        best_bid=1000000,
        best_ask=1001000,
        bid_depth=50,
        ask_depth=50,
    )
    eng.process_lob_stats(s1)
    # Tick 2: same price, 500ms later
    s2 = LOBStatsEvent(
        symbol=sym,
        ts=base_ns + 500_000_000,
        imbalance=0.0,
        best_bid=1000000,
        best_ask=1001000,
        bid_depth=50,
        ask_depth=50,
    )
    eng.process_lob_stats(s2)
    assert eng.get_feature(sym, "tob_survival_ms") == 500
    # Tick 3: price changes, 100ms later
    s3 = LOBStatsEvent(
        symbol=sym,
        ts=base_ns + 600_000_000,
        imbalance=0.0,
        best_bid=1000100,
        best_ask=1001100,
        bid_depth=50,
        ask_depth=50,
    )
    eng.process_lob_stats(s3)
    survival = eng.get_feature(sym, "tob_survival_ms")
    assert survival == 0  # Reset on price change


def test_v2_backward_compat_v1_engine():
    """v1 engine produces 16 features without v2 fields."""
    eng = FeatureEngine(feature_set_id="lob_shared_v1")
    eng.process_lob_stats(_stats(ts=1))
    tpl = eng.get_feature_tuple("2330")
    assert tpl is not None
    assert len(tpl) == 16
    # No v2 features accessible
    assert eng.get_feature("2330", "ofi_depth_norm_ppm") is None


class TestSymbolCardinalityGuard:
    """Rule 12: symbol cardinality guard prevents unbounded dict growth in FeatureEngine."""

    def test_process_returns_none_when_limit_exceeded(self):
        eng = FeatureEngine()
        eng._max_symbols = 2
        # Fill to limit
        eng.process_lob_stats(_stats(symbol="SYM_A", ts=1))
        eng.process_lob_stats(_stats(symbol="SYM_B", ts=2))
        assert len(eng._states) == 2
        # Third symbol should be rejected
        result = eng.process_lob_stats(_stats(symbol="SYM_C", ts=3))
        assert result is None
        assert len(eng._states) == 2

    def test_existing_symbol_still_processed_at_limit(self):
        eng = FeatureEngine()
        eng._max_symbols = 2
        eng.process_lob_stats(_stats(symbol="SYM_A", ts=1))
        eng.process_lob_stats(_stats(symbol="SYM_B", ts=2))
        # Existing symbol should still update
        result = eng.process_lob_stats(_stats(symbol="SYM_A", ts=3))
        assert result is not None
        assert result.symbol == "SYM_A"
        assert result.seq == 3

    def test_cardinality_warning_logged(self):
        from unittest.mock import MagicMock, patch

        eng = FeatureEngine()
        eng._max_symbols = 0  # reject all new symbols
        mock_logger = MagicMock()
        with patch("hft_platform.feature.engine.logger", mock_logger):
            result = eng.process_lob_stats(_stats(symbol="SYM_X", ts=1))
        assert result is None
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "feature_symbol_cardinality_exceeded"

    def test_default_max_symbols_is_10000(self):
        eng = FeatureEngine()
        assert eng._max_symbols == 10000


class TestLobKernelStateHasNan:
    """Tests for _LobKernelState.has_nan() — NaN/Inf contamination detection."""

    def test_clean_state_returns_false(self):
        ks = _LobKernelState()
        assert ks.has_nan() is False

    def test_ofi_l1_ema8_nan_detected(self):
        ks = _LobKernelState()
        ks.ofi_l1_ema8 = float("nan")
        assert ks.has_nan() is True

    def test_spread_ema8_nan_detected(self):
        ks = _LobKernelState()
        ks.spread_ema8 = float("nan")
        assert ks.has_nan() is True

    def test_imbalance_ema8_ppm_nan_detected(self):
        ks = _LobKernelState()
        ks.imbalance_ema8_ppm = float("nan")
        assert ks.has_nan() is True

    def test_iss_ema_ofi_nan_detected(self):
        """Previously unchecked field: iss_ema_ofi."""
        ks = _LobKernelState()
        ks.iss_ema_ofi = float("nan")
        assert ks.has_nan() is True

    def test_iss_ema_ret_nan_detected(self):
        ks = _LobKernelState()
        ks.iss_ema_ret = float("nan")
        assert ks.has_nan() is True

    def test_iss_ema_ofi2_inf_detected(self):
        ks = _LobKernelState()
        ks.iss_ema_ofi2 = float("inf")
        assert ks.has_nan() is True

    def test_iss_ema_ofi_ret_nan_detected(self):
        ks = _LobKernelState()
        ks.iss_ema_ofi_ret = float("nan")
        assert ks.has_nan() is True

    def test_iss_baseline_ema_nan_detected(self):
        ks = _LobKernelState()
        ks.iss_baseline_ema = float("nan")
        assert ks.has_nan() is True

    def test_mldm_deep_ema_fast_nan_detected(self):
        ks = _LobKernelState()
        ks.mldm_deep_ema_fast = float("nan")
        assert ks.has_nan() is True

    def test_mldm_deep_ema_slow_nan_detected(self):
        ks = _LobKernelState()
        ks.mldm_deep_ema_slow = float("nan")
        assert ks.has_nan() is True

    def test_mldm_output_ema_nan_detected(self):
        ks = _LobKernelState()
        ks.mldm_output_ema = float("nan")
        assert ks.has_nan() is True

    def test_tox_signed_vol_ema_nan_detected(self):
        ks = _LobKernelState()
        ks.tox_signed_vol_ema = float("nan")
        assert ks.has_nan() is True

    def test_tox_total_vol_ema_nan_detected(self):
        ks = _LobKernelState()
        ks.tox_total_vol_ema = float("nan")
        assert ks.has_nan() is True

    def test_agg_ofi_ema5s_nan_detected(self):
        ks = _LobKernelState()
        ks.agg_ofi_ema5s = float("nan")
        assert ks.has_nan() is True

    def test_agg_ofi_ema30s_nan_detected(self):
        ks = _LobKernelState()
        ks.agg_ofi_ema30s = float("nan")
        assert ks.has_nan() is True

    def test_agg_imb_ema5s_nan_detected(self):
        ks = _LobKernelState()
        ks.agg_imb_ema5s = float("nan")
        assert ks.has_nan() is True

    def test_agg_spread_ema30s_nan_detected(self):
        ks = _LobKernelState()
        ks.agg_spread_ema30s = float("nan")
        assert ks.has_nan() is True

    def test_agg_spread_ema300s_nan_detected(self):
        ks = _LobKernelState()
        ks.agg_spread_ema300s = float("nan")
        assert ks.has_nan() is True

    def test_negative_inf_detected(self):
        ks = _LobKernelState()
        ks.tox_total_vol_ema = float("-inf")
        assert ks.has_nan() is True

    def test_engine_resets_symbol_on_nan_in_new_field(self):
        """Integration: engine resets state when a previously-unguarded field goes NaN."""
        from unittest.mock import MagicMock, patch

        eng = FeatureEngine()
        eng.process_lob_stats(_stats(), local_ts_ns=1)
        # Corrupt a field that was NOT previously guarded
        ks = eng._lob_kernel_states["2330"]
        assert ks is not None
        ks.iss_ema_ofi = float("nan")

        mock_logger = MagicMock()
        with patch("hft_platform.feature.engine.logger", mock_logger):
            eng.process_lob_stats(_stats(ts=2), local_ts_ns=2)

        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][0] == "feature_nan_detected"


def test_nan_guard_runs_on_rust_fused_path():
    """NaN contamination guard must fire and reset_symbol when Rust fused path is active."""
    from unittest.mock import MagicMock, patch

    eng = FeatureEngine()
    eng.process_lob_stats(_stats(), local_ts_ns=1)

    # Corrupt kernel state to simulate NaN produced by Rust backend path
    ks = eng._lob_kernel_states.get("2330")
    assert ks is not None, "kernel state should exist after first tick"
    ks.ofi_l1_ema8 = float("nan")

    # Patch at the class level and set the instance backend flag
    # R8: NaN guard now checks fused output values directly, so inject NaN into the output tuple
    nan_values = list([0] * 27)
    nan_values[0] = float("nan")
    fake_fused = (tuple(nan_values), 0, 0)
    mock_logger = MagicMock()

    with (
        patch.object(FeatureEngine, "_compute_fused_rust", return_value=fake_fused),
        patch("hft_platform.feature.engine.logger", mock_logger),
    ):
        eng._kernel_backend = "rust"
        result = eng.process_lob_stats(_stats(ts=2), local_ts_ns=2)

    # Engine must return None (tick skipped) and log the warning
    assert result is None
    mock_logger.warning.assert_called_once()
    call_kwargs = mock_logger.warning.call_args
    assert call_kwargs[0][0] == "feature_nan_detected"
    assert call_kwargs[1].get("backend") == "rust"

    # State for symbol must be cleared by reset_symbol
    assert eng._states.get("2330") is None
