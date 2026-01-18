import timeit

import numpy as np

# Mock Data (Shioaji style)
payload = {
    "code": "2330",
    "ts": 1600000000000000,
    "bid_price": [100.0, 99.5, 99.0, 98.5, 98.0],
    "bid_volume": [1, 2, 3, 4, 5],
    "ask_price": [100.5, 101.0, 101.5, 102.0, 102.5],
    "ask_volume": [1, 2, 3, 4, 5],
}

SCALE = 10000


def numpy_impl(payload):
    bp = payload.get("bid_price", [])
    bv = payload.get("bid_volume", [])

    b_p_arr = np.array(bp, dtype=np.float64)
    b_v_arr = np.array(bv, dtype=np.int64)

    mask_b = b_p_arr > 0

    bids_final = np.empty((np.sum(mask_b), 2), dtype=np.int64)
    if bids_final.size > 0:
        bids_final[:, 0] = (b_p_arr[mask_b] * SCALE).astype(np.int64)
        bids_final[:, 1] = b_v_arr[mask_b]
    return bids_final


def pure_python_impl(payload):
    bp = payload.get("bid_price", [])
    bv = payload.get("bid_volume", [])

    # List comprehension is often faster for small N (5)
    bids = []
    for p, v in zip(bp, bv):
        if p > 0:
            bids.append([int(p * SCALE), int(v)])

    # If we need numpy array at the end for LOB Engine:
    # return np.array(bids, dtype=np.int64)
    # BUT LOB Engine uses Numpy? Yes.
    # So we must return numpy array.
    # Checking cost of np.array(list_of_lists)
    if not bids:
        return np.empty((0, 2), dtype=np.int64)
    return np.array(bids, dtype=np.int64)


def pure_python_impl_optimized(payload):
    # Avoid zip overhead?
    bp = payload.get("bid_price", [])
    bv = payload.get("bid_volume", [])

    # Presize? No, just loop.
    bids = [[int(p * SCALE), int(v)] for p, v in zip(bp, bv) if p > 0]

    return np.array(bids, dtype=np.int64)


if __name__ == "__main__":
    t_numpy = timeit.timeit(lambda: numpy_impl(payload), number=10000)
    t_python = timeit.timeit(lambda: pure_python_impl(payload), number=10000)
    t_opt = timeit.timeit(lambda: pure_python_impl_optimized(payload), number=10000)

    print(f"Numpy: {t_numpy:.4f}s (per call: {t_numpy / 10000 * 1e6:.2f} us)")
    print(f"Python: {t_python:.4f}s (per call: {t_python / 10000 * 1e6:.2f} us)")
    print(f"Opt: {t_opt:.4f}s (per call: {t_opt / 10000 * 1e6:.2f} us)")
