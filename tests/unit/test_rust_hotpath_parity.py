import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load_rust_core():
    try:
        from hft_platform import rust_core as rc  # type: ignore
    except Exception:
        try:
            import rust_core as rc  # type: ignore
        except Exception:
            return None
    return rc


def _py_scale_book(prices, vols, scale):
    out = []
    for p, v in zip(prices, vols):
        if p > 0:
            out.append([int(p * scale), int(v)])
    return np.array(out, dtype=np.int64)


def _py_stats(bids, asks):
    if bids.size > 0:
        best_bid = int(bids[0, 0])
        bid_top_vol = int(bids[0, 1])
        bid_depth = int(bids[:, 1].sum())
    else:
        best_bid = 0
        bid_top_vol = 0
        bid_depth = 0

    if asks.size > 0:
        best_ask = int(asks[0, 0])
        ask_top_vol = int(asks[0, 1])
        ask_depth = int(asks[:, 1].sum())
    else:
        best_ask = 0
        ask_top_vol = 0
        ask_depth = 0

    if best_bid > 0 and best_ask > 0:
        mid = (best_bid + best_ask) / 2.0
        spread = float(best_ask - best_bid)
        total_top = bid_top_vol + ask_top_vol
        if total_top > 0:
            imbalance = (bid_top_vol - ask_top_vol) / float(total_top)
        else:
            imbalance = 0.0
    else:
        mid = 0.0
        spread = 0.0
        imbalance = 0.0

    return best_bid, best_ask, bid_depth, ask_depth, mid, spread, imbalance


def _py_normalize_tick(payload, scale):
    symbol = payload.get("code") or payload.get("Code")
    ts_val = payload.get("ts") or payload.get("datetime")
    close_val = payload.get("close") or payload.get("Close") or payload.get("price")
    vol_val = payload.get("volume") or payload.get("Volume")
    total_volume = int(payload.get("total_volume") or 0)
    is_simtrade = bool(payload.get("simtrade") or 0)
    is_odd_lot = bool(payload.get("intraday_odd") or 0)

    if ts_val is not None and hasattr(ts_val, "timestamp"):
        exch_ts = int(ts_val.timestamp() * 1e9)
    else:
        exch_ts = int(ts_val) if ts_val else 0

    if close_val is not None:
        price = int(float(close_val) * scale)
    else:
        price = 0
    volume = int(vol_val) if vol_val is not None else 0

    return (
        "tick",
        symbol,
        price,
        volume,
        total_volume,
        is_simtrade,
        is_odd_lot,
        exch_ts,
    )


def _py_normalize_bidask(payload, scale):
    symbol = payload.get("code") or payload.get("Code")
    ts_val = payload.get("ts") or payload.get("datetime")
    bp = payload.get("bid_price") or []
    bv = payload.get("bid_volume") or []
    ap = payload.get("ask_price") or []
    av = payload.get("ask_volume") or []

    if ts_val is not None and hasattr(ts_val, "timestamp"):
        exch_ts = int(ts_val.timestamp() * 1e9)
    else:
        exch_ts = int(ts_val) if ts_val else 0

    bids = _py_scale_book(bp, bv, scale)
    asks = _py_scale_book(ap, av, scale)
    stats = _py_stats(bids, asks)
    return (
        "bidask",
        symbol,
        bids,
        asks,
        exch_ts,
        False,
        stats[0],
        stats[1],
        stats[2],
        stats[3],
        stats[4],
        stats[5],
        stats[6],
    )


def test_scale_book_pair_stats_parity():
    rc = _load_rust_core()
    if rc is None or not hasattr(rc, "scale_book_pair_stats"):
        pytest.skip("rust_core not available")

    scale = 100
    bid_prices = [100.0, 99.5, 0.0, 98.0]
    bid_vols = [10, 20, 0, 40]
    ask_prices = [100.5, 101.0, 0.0, 102.0]
    ask_vols = [11, 21, 0, 41]

    bids_py = _py_scale_book(bid_prices, bid_vols, scale)
    asks_py = _py_scale_book(ask_prices, ask_vols, scale)
    stats_py = _py_stats(bids_py, asks_py)

    bids_rs, asks_rs, stats_rs = rc.scale_book_pair_stats(
        bid_prices, bid_vols, ask_prices, ask_vols, scale
    )
    bids_rs = np.asarray(bids_rs)
    asks_rs = np.asarray(asks_rs)

    assert np.array_equal(bids_rs, bids_py)
    assert np.array_equal(asks_rs, asks_py)

    assert stats_rs[0] == stats_py[0]
    assert stats_rs[1] == stats_py[1]
    assert stats_rs[2] == stats_py[2]
    assert stats_rs[3] == stats_py[3]
    assert stats_rs[4] == pytest.approx(stats_py[4])
    assert stats_rs[5] == pytest.approx(stats_py[5])
    assert stats_rs[6] == pytest.approx(stats_py[6])


def test_normalize_tick_tuple_parity():
    rc = _load_rust_core()
    if rc is None or not hasattr(rc, "normalize_tick_tuple"):
        pytest.skip("rust_core not available")

    payload = {
        "code": "2330",
        "ts": 1700000000,
        "close": 100.25,
        "volume": 7,
        "total_volume": 12,
        "simtrade": 0,
        "intraday_odd": 1,
    }
    scale = 100
    py_tuple = _py_normalize_tick(payload, scale)
    rs_tuple = rc.normalize_tick_tuple(payload, payload["code"], scale)
    assert rs_tuple == py_tuple


def test_normalize_bidask_tuple_parity():
    rc = _load_rust_core()
    if rc is None or not hasattr(rc, "normalize_bidask_tuple"):
        pytest.skip("rust_core not available")

    payload = {
        "code": "2330",
        "ts": 1700000000,
        "bid_price": [100.0, 99.5, 0.0, 98.0],
        "bid_volume": [10, 20, 0, 40],
        "ask_price": [100.5, 101.0, 0.0, 102.0],
        "ask_volume": [11, 21, 0, 41],
    }
    scale = 100
    py_tuple = _py_normalize_bidask(payload, scale)
    rs_tuple = rc.normalize_bidask_tuple(payload, payload["code"], scale)
    assert rs_tuple[0] == py_tuple[0]
    assert rs_tuple[1] == py_tuple[1]
    assert rs_tuple[4] == py_tuple[4]
    assert rs_tuple[5] == py_tuple[5]
    assert np.array_equal(np.asarray(rs_tuple[2]), py_tuple[2])
    assert np.array_equal(np.asarray(rs_tuple[3]), py_tuple[3])
    assert rs_tuple[6:] == pytest.approx(py_tuple[6:])


def test_normalize_bidask_tuple_np_parity():
    rc = _load_rust_core()
    if rc is None or not hasattr(rc, "normalize_bidask_tuple_np"):
        pytest.skip("rust_core not available")

    payload = {
        "code": "2330",
        "ts": 1700000000,
        "bid_price": [100.0, 99.5, 0.0, 98.0],
        "bid_volume": [10, 20, 0, 40],
        "ask_price": [100.5, 101.0, 0.0, 102.0],
        "ask_volume": [11, 21, 0, 41],
    }
    scale = 100
    py_tuple = _py_normalize_bidask(payload, scale)

    bid_prices_np = np.asarray(payload["bid_price"], dtype=np.float64)
    bid_vols_np = np.asarray(payload["bid_volume"], dtype=np.int64)
    ask_prices_np = np.asarray(payload["ask_price"], dtype=np.float64)
    ask_vols_np = np.asarray(payload["ask_volume"], dtype=np.int64)
    rs_tuple = rc.normalize_bidask_tuple_np(
        payload["code"],
        int(payload["ts"]),
        bid_prices_np,
        bid_vols_np,
        ask_prices_np,
        ask_vols_np,
        scale,
    )

    assert rs_tuple[0] == py_tuple[0]
    assert rs_tuple[1] == py_tuple[1]
    assert rs_tuple[4] == py_tuple[4]
    assert rs_tuple[5] == py_tuple[5]
    assert np.array_equal(np.asarray(rs_tuple[2]), py_tuple[2])
    assert np.array_equal(np.asarray(rs_tuple[3]), py_tuple[3])
    assert rs_tuple[6:] == pytest.approx(py_tuple[6:])


def test_normalize_bidask_tuple_with_synth_no_asks():
    """Rust synth normalizer synthesizes ask side when asks are empty."""
    rc = _load_rust_core()
    if rc is None or not hasattr(rc, "normalize_bidask_tuple_with_synth"):
        pytest.skip("rust_core.normalize_bidask_tuple_with_synth not available")

    scale = 100
    tick_size_scaled = 1  # 1 tick = 1 unit in scaled space
    synthetic_ticks = 1

    bid_prices = np.array([100.0, 99.5], dtype=np.float64)
    bid_vols = np.array([10, 20], dtype=np.int64)
    ask_prices = np.array([], dtype=np.float64)
    ask_vols = np.array([], dtype=np.int64)

    result = rc.normalize_bidask_tuple_with_synth(
        "2330", 1700000000,
        bid_prices, bid_vols,
        ask_prices, ask_vols,
        scale, tick_size_scaled, synthetic_ticks,
    )

    assert result[0] == "bidask"
    assert result[1] == "2330"
    bids = np.asarray(result[2])
    asks = np.asarray(result[3])
    assert bids.shape[0] == 2
    assert asks.shape[0] == 1  # synthesized

    best_bid = int(bids[0, 0])
    synth_ask = int(asks[0, 0])
    assert synth_ask == best_bid + tick_size_scaled * synthetic_ticks
    assert int(asks[0, 1]) == 1  # 1-lot

    synthesized = result[13]
    assert synthesized is True


def test_normalize_bidask_tuple_with_synth_no_bids():
    """Rust synth normalizer synthesizes bid side when bids are empty."""
    rc = _load_rust_core()
    if rc is None or not hasattr(rc, "normalize_bidask_tuple_with_synth"):
        pytest.skip("rust_core.normalize_bidask_tuple_with_synth not available")

    scale = 100
    tick_size_scaled = 1
    synthetic_ticks = 2

    bid_prices = np.array([], dtype=np.float64)
    bid_vols = np.array([], dtype=np.int64)
    ask_prices = np.array([101.0, 101.5], dtype=np.float64)
    ask_vols = np.array([5, 15], dtype=np.int64)

    result = rc.normalize_bidask_tuple_with_synth(
        "2330", 1700000000,
        bid_prices, bid_vols,
        ask_prices, ask_vols,
        scale, tick_size_scaled, synthetic_ticks,
    )

    bids = np.asarray(result[2])
    asks = np.asarray(result[3])
    assert bids.shape[0] == 1  # synthesized
    assert asks.shape[0] == 2

    best_ask = int(asks[0, 0])
    synth_bid = int(bids[0, 0])
    assert synth_bid == best_ask - tick_size_scaled * synthetic_ticks
    assert int(bids[0, 1]) == 1
    assert result[13] is True


def test_normalize_bidask_tuple_with_synth_both_sides():
    """When both sides have levels, no synthesis occurs."""
    rc = _load_rust_core()
    if rc is None or not hasattr(rc, "normalize_bidask_tuple_with_synth"):
        pytest.skip("rust_core.normalize_bidask_tuple_with_synth not available")

    scale = 100
    bid_prices = np.array([100.0], dtype=np.float64)
    bid_vols = np.array([10], dtype=np.int64)
    ask_prices = np.array([101.0], dtype=np.float64)
    ask_vols = np.array([10], dtype=np.int64)

    result = rc.normalize_bidask_tuple_with_synth(
        "2330", 1700000000,
        bid_prices, bid_vols,
        ask_prices, ask_vols,
        scale, 1, 1,
    )

    bids = np.asarray(result[2])
    asks = np.asarray(result[3])
    assert bids.shape[0] == 1
    assert asks.shape[0] == 1
    assert result[13] is False  # not synthesized

    # Stats should match non-synth version
    rs_np = rc.normalize_bidask_tuple_np(
        "2330", 1700000000,
        bid_prices, bid_vols,
        ask_prices, ask_vols,
        scale,
    )
    # Compare stats fields (indices 6..12)
    for i in range(6, 13):
        assert result[i] == pytest.approx(rs_np[i])


@pytest.mark.asyncio
async def test_ring_buffer_rust_backend_roundtrip(monkeypatch):
    rc = _load_rust_core()
    if rc is None or not hasattr(rc, "FastRingBuffer"):
        pytest.skip("rust_core not available")

    monkeypatch.setenv("HFT_RUST_ACCEL", "1")
    monkeypatch.setenv("HFT_BUS_RUST", "1")

    import hft_platform.engine.event_bus as event_bus

    importlib.reload(event_bus)

    bus = event_bus.RingBufferBus(size=8)
    assert getattr(bus, "_use_rust", False) is True

    for i in range(5):
        await bus.publish(i)

    events = []
    async for event in bus.consume(start_cursor=-1):
        events.append(event)
        if len(events) == 5:
            break

    assert events == [0, 1, 2, 3, 4]
