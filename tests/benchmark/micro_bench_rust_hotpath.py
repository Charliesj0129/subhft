import timeit

import numpy as np


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
bid_prices = [100.0 - 0.5 * i for i in range(LEVELS)]
bid_vols = [10 + i for i in range(LEVELS)]
ask_prices = [100.5 + 0.5 * i for i in range(LEVELS)]
ask_vols = [11 + i for i in range(LEVELS)]
bid_prices_np = np.asarray(bid_prices, dtype=np.float64)
bid_vols_np = np.asarray(bid_vols, dtype=np.int64)
ask_prices_np = np.asarray(ask_prices, dtype=np.float64)
ask_vols_np = np.asarray(ask_vols, dtype=np.int64)
payload = {
    "code": "2330",
    "ts": 1700000000,
    "bid_price": bid_prices,
    "bid_volume": bid_vols,
    "ask_price": ask_prices,
    "ask_volume": ask_vols,
}


def python_impl():
    bids = [[int(p * SCALE), int(v)] for p, v in zip(bid_prices, bid_vols) if p > 0]
    asks = [[int(p * SCALE), int(v)] for p, v in zip(ask_prices, ask_vols) if p > 0]
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


def rust_impl(rc):
    fn = getattr(rc, "scale_book_pair_stats_np", None)
    if fn is None:
        return rc.scale_book_pair_stats(bid_prices, bid_vols, ask_prices, ask_vols, SCALE)
    return fn(bid_prices_np, bid_vols_np, ask_prices_np, ask_vols_np, SCALE)


def python_normalize_bidask():
    bids = [[int(p * SCALE), int(v)] for p, v in zip(bid_prices, bid_vols) if p > 0]
    asks = [[int(p * SCALE), int(v)] for p, v in zip(ask_prices, ask_vols) if p > 0]
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
        return rc.normalize_bidask_tuple(payload, payload["code"], SCALE)
    return fn(
        payload["code"],
        payload["ts"],
        bid_prices_np,
        bid_vols_np,
        ask_prices_np,
        ask_vols_np,
        SCALE,
    )


if __name__ == "__main__":
    rc = _load_rust_core()
    if rc is None:
        print("rust_core not available; skipping rust benchmark.")
    else:
        t_rust = timeit.timeit(lambda: rust_impl(rc), number=20000)
        print(f"Rust:   {t_rust:.4f}s (per call: {t_rust / 20000 * 1e6:.2f} us)")
        t_rust_norm = timeit.timeit(lambda: rust_normalize_bidask(rc), number=20000)
        print(f"Rust normalize: {t_rust_norm:.4f}s (per call: {t_rust_norm / 20000 * 1e6:.2f} us)")

    t_py = timeit.timeit(python_impl, number=20000)
    print(f"Python: {t_py:.4f}s (per call: {t_py / 20000 * 1e6:.2f} us)")
    t_py_norm = timeit.timeit(python_normalize_bidask, number=20000)
    print(f"Python normalize: {t_py_norm:.4f}s (per call: {t_py_norm / 20000 * 1e6:.2f} us)")
