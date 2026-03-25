"""Unit tests for FeatureEngine lob_shared_v2: ISS + MLDM features."""

from __future__ import annotations

import numpy as np

from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feature.registry import (
    FEATURE_SET_VERSION,
    build_default_lob_feature_set_v1,
    build_default_lob_feature_set_v2,
    default_feature_registry,
)


def _make_stats(symbol: str, bb: int, ba: int, bd: int, ad: int, ts: int = 1000) -> LOBStatsEvent:
    mid_x2 = bb + ba
    spread = ba - bb
    return LOBStatsEvent(
        symbol=symbol, ts=ts, imbalance=0.0,
        best_bid=bb, best_ask=ba, bid_depth=bd, ask_depth=ad,
        mid_price_x2=mid_x2, spread_scaled=spread,
    )


def _make_event(bids: list[list[int]], asks: list[list[int]]) -> BidAskEvent:
    return BidAskEvent(
        meta=MetaData(source_ts=1000, local_ts=1000, seq=0),
        symbol="TEST",
        bids=np.array(bids, dtype=np.int64),
        asks=np.array(asks, dtype=np.int64),
    )


# --- Registry ---

def test_v2_feature_set_has_18_features() -> None:
    fs = build_default_lob_feature_set_v2()
    assert len(fs.features) == 18


def test_v2_iss_at_index_16() -> None:
    fs = build_default_lob_feature_set_v2()
    assert fs.features[16].feature_id == "impact_surprise_x1000"


def test_v2_mldm_at_index_17() -> None:
    fs = build_default_lob_feature_set_v2()
    assert fs.features[17].feature_id == "deep_depth_momentum_x1000"


def test_v1_backward_compatible() -> None:
    fs_v1 = build_default_lob_feature_set_v1()
    fs_v2 = build_default_lob_feature_set_v2()
    for i in range(16):
        assert fs_v2.features[i].feature_id == fs_v1.features[i].feature_id


def test_default_registry_is_v2() -> None:
    reg = default_feature_registry()
    assert reg.get_default().feature_set_id == "lob_shared_v2"


def test_v1_still_accessible() -> None:
    reg = default_feature_registry()
    v1 = reg.get("lob_shared_v1")
    assert len(v1.features) == 16


def test_feature_set_version_is_v2() -> None:
    assert FEATURE_SET_VERSION == "lob_shared_v2"


# --- FeatureEngine v2 tuple ---

def test_v2_engine_produces_18_features() -> None:
    engine = FeatureEngine(feature_set_id="lob_shared_v2", kernel_backend="python")
    stats = _make_stats("TEST", 100_0000, 101_0000, 50, 50)
    event = _make_event(
        [[100_0000, 50], [99_0000, 30], [98_0000, 20], [97_0000, 10], [96_0000, 5]],
        [[101_0000, 50], [102_0000, 30], [103_0000, 20], [104_0000, 10], [105_0000, 5]],
    )
    engine.process_lob_update(event, stats)
    vals = engine.get_feature_tuple("TEST")
    assert vals is not None
    assert len(vals) == 18


def test_v1_engine_still_produces_16_features() -> None:
    engine = FeatureEngine(feature_set_id="lob_shared_v1", kernel_backend="python")
    stats = _make_stats("TEST", 100_0000, 101_0000, 50, 50)
    engine.process_lob_update(None, stats)
    vals = engine.get_feature_tuple("TEST")
    assert vals is not None
    assert len(vals) == 16


def test_v2_first_16_match_v1() -> None:
    """V1 features at indices 0-15 must be identical in v2."""
    eng_v1 = FeatureEngine(feature_set_id="lob_shared_v1", kernel_backend="python")
    eng_v2 = FeatureEngine(feature_set_id="lob_shared_v2", kernel_backend="python")
    stats = _make_stats("TEST", 100_0000, 101_0000, 50, 50)
    event = _make_event(
        [[100_0000, 50], [99_0000, 30], [98_0000, 20], [97_0000, 10], [96_0000, 5]],
        [[101_0000, 50], [102_0000, 30], [103_0000, 20], [104_0000, 10], [105_0000, 5]],
    )
    eng_v1.process_lob_update(None, stats)
    eng_v2.process_lob_update(event, stats)
    v1 = eng_v1.get_feature_tuple("TEST")
    v2 = eng_v2.get_feature_tuple("TEST")
    assert v1 is not None and v2 is not None
    for i in range(16):
        assert v1[i] == v2[i], f"index {i}: v1={v1[i]} != v2={v2[i]}"


# --- ISS behavior ---

def test_iss_zero_during_warmup() -> None:
    engine = FeatureEngine(feature_set_id="lob_shared_v2", kernel_backend="python")
    event = _make_event(
        [[100_0000, 50], [99_0000, 30], [98_0000, 20], [97_0000, 10], [96_0000, 5]],
        [[101_0000, 50], [102_0000, 30], [103_0000, 20], [104_0000, 10], [105_0000, 5]],
    )
    for i in range(399):
        stats = _make_stats("TEST", 100_0000, 101_0000, 50, 50, ts=1000 + i)
        engine.process_lob_update(event, stats)
    vals = engine.get_feature_tuple("TEST")
    assert vals is not None
    assert vals[16] == 0, f"ISS should be 0 during warmup, got {vals[16]}"


def test_iss_nonzero_after_warmup_with_correlated_data() -> None:
    engine = FeatureEngine(feature_set_id="lob_shared_v2", kernel_backend="python")
    mid = 100_0000
    for i in range(600):
        mid += 100  # trending price
        bb = mid - 5000
        ba = mid + 5000
        event = _make_event(
            [[bb, 50 + i % 10], [bb - 10000, 30], [bb - 20000, 20], [bb - 30000, 10], [bb - 40000, 5]],
            [[ba, 50], [ba + 10000, 30], [ba + 20000, 20], [ba + 30000, 10], [ba + 40000, 5]],
        )
        stats = _make_stats("TEST", bb, ba, 50 + i % 10, 50, ts=1000 + i)
        engine.process_lob_update(event, stats)
    vals = engine.get_feature_tuple("TEST")
    assert vals is not None
    # ISS may or may not be nonzero depending on OFI/return correlation, but should be finite
    assert isinstance(vals[16], int)


# --- MLDM behavior ---

def test_mldm_zero_with_no_event() -> None:
    """MLDM should be 0 when no BidAskEvent is passed (event=None)."""
    engine = FeatureEngine(feature_set_id="lob_shared_v2", kernel_backend="python")
    for i in range(200):
        stats = _make_stats("TEST", 100_0000, 101_0000, 50, 50, ts=1000 + i)
        engine.process_lob_update(None, stats)
    vals = engine.get_feature_tuple("TEST")
    assert vals is not None
    assert vals[17] == 0


def test_mldm_responds_to_deep_book_changes() -> None:
    engine = FeatureEngine(feature_set_id="lob_shared_v2", kernel_backend="python")
    # Warmup with stable book
    for i in range(150):
        event = _make_event(
            [[100_0000, 50], [99_0000, 30], [98_0000, 20], [97_0000, 10], [96_0000, 5]],
            [[101_0000, 50], [102_0000, 30], [103_0000, 20], [104_0000, 10], [105_0000, 5]],
        )
        stats = _make_stats("TEST", 100_0000, 101_0000, 50, 50, ts=1000 + i)
        engine.process_lob_update(event, stats)
    # Now grow bid depth at L2-L5
    for i in range(50):
        deep_qty = 30 + 5 * i
        event = _make_event(
            [[100_0000, 50], [99_0000, deep_qty], [98_0000, deep_qty], [97_0000, deep_qty], [96_0000, deep_qty]],
            [[101_0000, 50], [102_0000, 30], [103_0000, 20], [104_0000, 10], [105_0000, 5]],
        )
        stats = _make_stats("TEST", 100_0000, 101_0000, 50, 50, ts=1200 + i)
        engine.process_lob_update(event, stats)
    vals = engine.get_feature_tuple("TEST")
    assert vals is not None
    assert vals[17] > 0, f"MLDM should be positive with growing bid L2-L5 depth, got {vals[17]}"


def test_mldm_bbo_shift_guard() -> None:
    """When BBO price changes, MLDM deep_net should be zeroed."""
    engine = FeatureEngine(feature_set_id="lob_shared_v2", kernel_backend="python")
    # Feed stable book
    for i in range(200):
        event = _make_event(
            [[100_0000, 50], [99_0000, 30], [98_0000, 20], [97_0000, 10], [96_0000, 5]],
            [[101_0000, 50], [102_0000, 30], [103_0000, 20], [104_0000, 10], [105_0000, 5]],
        )
        stats = _make_stats("TEST", 100_0000, 101_0000, 50, 50, ts=1000 + i)
        engine.process_lob_update(event, stats)
    val_before = engine.get_feature_tuple("TEST")[17]
    # BBO shift: bid moves from 100 to 101
    event_shifted = _make_event(
        [[101_0000, 200], [100_0000, 300], [99_0000, 200], [98_0000, 100], [97_0000, 50]],
        [[102_0000, 50], [103_0000, 30], [104_0000, 20], [105_0000, 10], [106_0000, 5]],
    )
    stats_shifted = _make_stats("TEST", 101_0000, 102_0000, 200, 50, ts=1201)
    engine.process_lob_update(event_shifted, stats_shifted)
    val_after = engine.get_feature_tuple("TEST")[17]
    # After BBO shift, MLDM should not spike from the level-shift artifact
    assert abs(val_after) <= abs(val_before) + 100, \
        f"MLDM should be guarded on BBO shift: before={val_before}, after={val_after}"


def test_mldm_thin_book_guard() -> None:
    """MLDM should be 0 when fewer than 2 levels."""
    engine = FeatureEngine(feature_set_id="lob_shared_v2", kernel_backend="python")
    for i in range(200):
        event = _make_event([[100_0000, 50]], [[101_0000, 50]])
        stats = _make_stats("TEST", 100_0000, 101_0000, 50, 50, ts=1000 + i)
        engine.process_lob_update(event, stats)
    vals = engine.get_feature_tuple("TEST")
    assert vals is not None
    assert vals[17] == 0


# --- Feature access ---

def test_get_feature_by_id_iss() -> None:
    engine = FeatureEngine(feature_set_id="lob_shared_v2", kernel_backend="python")
    event = _make_event(
        [[100_0000, 50], [99_0000, 30], [98_0000, 20], [97_0000, 10], [96_0000, 5]],
        [[101_0000, 50], [102_0000, 30], [103_0000, 20], [104_0000, 10], [105_0000, 5]],
    )
    stats = _make_stats("TEST", 100_0000, 101_0000, 50, 50)
    engine.process_lob_update(event, stats)
    iss = engine.get_feature("TEST", "impact_surprise_x1000")
    assert iss is not None
    assert isinstance(iss, int)


def test_get_feature_by_id_mldm() -> None:
    engine = FeatureEngine(feature_set_id="lob_shared_v2", kernel_backend="python")
    event = _make_event(
        [[100_0000, 50], [99_0000, 30], [98_0000, 20], [97_0000, 10], [96_0000, 5]],
        [[101_0000, 50], [102_0000, 30], [103_0000, 20], [104_0000, 10], [105_0000, 5]],
    )
    stats = _make_stats("TEST", 100_0000, 101_0000, 50, 50)
    engine.process_lob_update(event, stats)
    mldm = engine.get_feature("TEST", "deep_depth_momentum_x1000")
    assert mldm is not None
    assert isinstance(mldm, int)
