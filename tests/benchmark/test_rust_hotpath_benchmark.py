import pytest
import numpy as np
import pytest

pytest.importorskip("pytest_benchmark")


def _load_rust_core():
    try:
        from hft_platform import rust_core as rc  # type: ignore
    except Exception:
        try:
            import rust_core as rc  # type: ignore
        except Exception:
            return None
    return rc


SCALE = 100
LEVELS = 20
BID_PRICES = [100.0 - 0.5 * i for i in range(LEVELS)]
BID_VOLS = [10 + i for i in range(LEVELS)]
ASK_PRICES = [100.5 + 0.5 * i for i in range(LEVELS)]
ASK_VOLS = [11 + i for i in range(LEVELS)]
_BID_PRICES_NP = np.asarray(BID_PRICES, dtype=np.float64)
_BID_VOLS_NP = np.asarray(BID_VOLS, dtype=np.int64)
_ASK_PRICES_NP = np.asarray(ASK_PRICES, dtype=np.float64)
_ASK_VOLS_NP = np.asarray(ASK_VOLS, dtype=np.int64)
PAYLOAD = {
    "code": "2330",
    "ts": 1700000000,
    "bid_price": BID_PRICES,
    "bid_volume": BID_VOLS,
    "ask_price": ASK_PRICES,
    "ask_volume": ASK_VOLS,
}


def python_scale_book_pair_stats():
    bids = [[int(p * SCALE), int(v)] for p, v in zip(BID_PRICES, BID_VOLS) if p > 0]
    asks = [[int(p * SCALE), int(v)] for p, v in zip(ASK_PRICES, ASK_VOLS) if p > 0]
    bids_arr = np.array(bids, dtype=np.int64)
    asks_arr = np.array(asks, dtype=np.int64)
    best_bid = int(bids_arr[0, 0])
    best_ask = int(asks_arr[0, 0])
    bid_depth = int(bids_arr[:, 1].sum())
    ask_depth = int(asks_arr[:, 1].sum())
    mid = (best_bid + best_ask) / 2.0
    spread = float(best_ask - best_bid)
    total_top = int(bids_arr[0, 1] + asks_arr[0, 1])
    imbalance = ((int(bids_arr[0, 1]) - int(asks_arr[0, 1])) / total_top) if total_top else 0.0
    return bids_arr, asks_arr, (best_bid, best_ask, bid_depth, ask_depth, mid, spread, imbalance)


def rust_scale_book_pair_stats(rc):
    fn = getattr(rc, "scale_book_pair_stats_np", None)
    if fn is None:
        return rc.scale_book_pair_stats(BID_PRICES, BID_VOLS, ASK_PRICES, ASK_VOLS, SCALE)
    return fn(_BID_PRICES_NP, _BID_VOLS_NP, _ASK_PRICES_NP, _ASK_VOLS_NP, SCALE)


def python_normalize_bidask():
    bids = [[int(p * SCALE), int(v)] for p, v in zip(BID_PRICES, BID_VOLS) if p > 0]
    asks = [[int(p * SCALE), int(v)] for p, v in zip(ASK_PRICES, ASK_VOLS) if p > 0]
    bids_arr = np.array(bids, dtype=np.int64)
    asks_arr = np.array(asks, dtype=np.int64)
    best_bid = int(bids_arr[0, 0])
    best_ask = int(asks_arr[0, 0])
    bid_depth = int(bids_arr[:, 1].sum())
    ask_depth = int(asks_arr[:, 1].sum())
    mid = (best_bid + best_ask) / 2.0
    spread = float(best_ask - best_bid)
    total_top = int(bids_arr[0, 1] + asks_arr[0, 1])
    imbalance = ((int(bids_arr[0, 1]) - int(asks_arr[0, 1])) / total_top) if total_top else 0.0
    return (
        "bidask",
        "2330",
        bids_arr,
        asks_arr,
        1700000000,
        False,
        best_bid,
        best_ask,
        bid_depth,
        ask_depth,
        mid,
        spread,
        imbalance,
    )


def rust_normalize_bidask(rc):
    fn = getattr(rc, "normalize_bidask_tuple_np", None)
    if fn is None:
        return rc.normalize_bidask_tuple(PAYLOAD, PAYLOAD["code"], SCALE)
    return fn(
        PAYLOAD["code"],
        PAYLOAD["ts"],
        _BID_PRICES_NP,
        _BID_VOLS_NP,
        _ASK_PRICES_NP,
        _ASK_VOLS_NP,
        SCALE,
    )


def test_bench_python_scale_book_pair_stats(benchmark):
    benchmark(python_scale_book_pair_stats)


def test_bench_rust_scale_book_pair_stats(benchmark):
    rc = _load_rust_core()
    if rc is None:
        pytest.skip("rust_core not available")
    benchmark(lambda: rust_scale_book_pair_stats(rc))


def test_bench_python_normalize_bidask(benchmark):
    benchmark(python_normalize_bidask)


def test_bench_rust_normalize_bidask(benchmark):
    rc = _load_rust_core()
    if rc is None:
        pytest.skip("rust_core not available")
    benchmark(lambda: rust_normalize_bidask(rc))


# ---------------------------------------------------------------------------
# Position tracker benchmarks
# ---------------------------------------------------------------------------
_POS_KEY = "ACC:STRAT:2330"
_POS_FILLS = [
    (0, 10, 100000, 5, 0),   # BUY 10 @ 100000
    (0, 5, 100200, 3, 0),    # BUY 5 @ 100200
    (1, 8, 100500, 4, 0),    # SELL 8 @ 100500
    (1, 7, 100100, 4, 0),    # SELL 7 @ 100100
    (0, 20, 100050, 10, 0),  # BUY 20 @ 100050
    (1, 20, 100400, 10, 0),  # SELL 20 @ 100400
]


def _python_position_update():
    """Simulate Python Position.update() logic in pure integer arithmetic."""
    net = 0
    avg = 0
    pnl = 0
    fees = 0
    for side, qty, price, fee, tax in _POS_FILLS:
        is_buy = side == 0
        signed = qty if is_buy else -qty
        fees += fee + tax
        cur_sign = 1 if net > 0 else (-1 if net < 0 else 0)
        fill_sign = 1 if is_buy else -1
        if cur_sign != 0 and fill_sign != cur_sign:
            close_qty = min(abs(net), qty)
            if is_buy:
                pnl += (avg - price) * close_qty
            else:
                pnl += (price - avg) * close_qty
            net += signed
            if (cur_sign > 0 and net < 0) or (cur_sign < 0 and net > 0):
                avg = price
        else:
            if net == 0:
                avg = price
                net += signed
            else:
                total = net * avg + signed * price
                net += signed
                if net != 0:
                    avg = total // net
    return net, avg, pnl, fees


def _rust_position_update(tracker):
    for side, qty, price, fee, tax in _POS_FILLS:
        tracker.update(_POS_KEY, side, qty, price, fee, tax, 0)


def test_bench_python_position_update(benchmark):
    benchmark(_python_position_update)


def test_bench_rust_position_update(benchmark):
    rc = _load_rust_core()
    if rc is None or not hasattr(rc, "RustPositionTracker"):
        pytest.skip("rust_core.RustPositionTracker not available")
    tracker = rc.RustPositionTracker()
    benchmark(lambda: _rust_position_update(tracker))
