"""Full pipeline Rust vs Python benchmark.

Measures every Rust-accelerated stage with realistic data sizes.
Run: uv run python tests/benchmark/bench_rust_pipeline_full.py
"""

import sys
import timeit
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load_rust_core():
    try:
        from hft_platform import rust_core as rc
    except Exception:
        try:
            import rust_core as rc
        except Exception:
            return None
    return rc


rc = _load_rust_core()

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------
SCALE = 10000  # realistic x10000 scaling
N_ITERS = 50_000

# L5 book (5 levels) — typical Shioaji
L5_BID_P = np.array([100.0, 99.95, 99.90, 99.85, 99.80], dtype=np.float64)
L5_BID_V = np.array([50, 120, 80, 200, 150], dtype=np.int64)
L5_ASK_P = np.array([100.05, 100.10, 100.15, 100.20, 100.25], dtype=np.float64)
L5_ASK_V = np.array([60, 90, 110, 180, 140], dtype=np.int64)

# L20 book (20 levels) — deep book
L20_BID_P = np.array([100.0 - 0.05 * i for i in range(20)], dtype=np.float64)
L20_BID_V = np.array([50 + i * 10 for i in range(20)], dtype=np.int64)
L20_ASK_P = np.array([100.05 + 0.05 * i for i in range(20)], dtype=np.float64)
L20_ASK_V = np.array([60 + i * 10 for i in range(20)], dtype=np.int64)

# Tick payload
TICK_PAYLOAD = {
    "code": "TXFD6",
    "ts": 1700000000000000,
    "close": 20100.0,
    "volume": 5,
    "tick_type": 1,
}

# BidAsk payload
BA_PAYLOAD = {
    "code": "TXFD6",
    "ts": 1700000000000000,
    "bid_price": L5_BID_P.tolist(),
    "bid_volume": L5_BID_V.tolist(),
    "ask_price": L5_ASK_P.tolist(),
    "ask_volume": L5_ASK_V.tolist(),
}

# Position fills
POS_FILLS = [
    (0, 10, 1000000, 5, 0),
    (0, 5, 1002000, 3, 0),
    (1, 8, 1005000, 4, 0),
    (1, 7, 1001000, 4, 0),
    (0, 20, 1000500, 10, 0),
    (1, 20, 1004000, 10, 0),
]

# Feature engine inputs
FE_BEST_BID = 1000000
FE_BEST_ASK = 1000500
FE_MID_X2 = 2000500
FE_SPREAD = 500
FE_BID_DEPTH = 600
FE_ASK_DEPTH = 580
FE_L1_BID_QTY = 50
FE_L1_ASK_QTY = 60


def _header(name: str):
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")


def _bench(name: str, rust_fn, python_fn, n: int = N_ITERS):
    t_rust = timeit.timeit(rust_fn, number=n) if rust_fn else None
    t_py = timeit.timeit(python_fn, number=n)

    us_rust = (t_rust / n * 1e6) if t_rust is not None else None
    us_py = t_py / n * 1e6

    if us_rust is not None:
        speedup = us_py / us_rust
        marker = "🟢" if speedup > 1.2 else ("🔴" if speedup < 0.8 else "⚪")
        print(f"  {marker} {name:40s}  Rust: {us_rust:8.2f} µs  Python: {us_py:8.2f} µs  Speedup: {speedup:5.2f}x")
    else:
        print(f"  ⬜ {name:40s}  Rust: N/A        Python: {us_py:8.2f} µs")

    return us_rust, us_py


# ===========================================================================
# Stage 1: scale_book (LOB scaling)
# ===========================================================================
def bench_scale_book():
    _header("Stage 1: scale_book (LOB price scaling)")

    # L5 Python
    def py_scale_l5():
        bids = [[int(round(p * SCALE)), int(v)] for p, v in zip(L5_BID_P, L5_BID_V) if p > 0]
        asks = [[int(round(p * SCALE)), int(v)] for p, v in zip(L5_ASK_P, L5_ASK_V) if p > 0]
        return np.array(bids, dtype=np.int64), np.array(asks, dtype=np.int64)

    def rust_scale_l5():
        return rc.scale_book_pair_stats_np(L5_BID_P, L5_BID_V, L5_ASK_P, L5_ASK_V, SCALE)

    _bench("scale_book_pair_stats (L5, 5 levels)", rust_scale_l5, py_scale_l5)

    # L20 Python
    def py_scale_l20():
        bids = [[int(round(p * SCALE)), int(v)] for p, v in zip(L20_BID_P, L20_BID_V) if p > 0]
        asks = [[int(round(p * SCALE)), int(v)] for p, v in zip(L20_ASK_P, L20_ASK_V) if p > 0]
        return np.array(bids, dtype=np.int64), np.array(asks, dtype=np.int64)

    def rust_scale_l20():
        return rc.scale_book_pair_stats_np(L20_BID_P, L20_BID_V, L20_ASK_P, L20_ASK_V, SCALE)

    _bench("scale_book_pair_stats (L20, 20 levels)", rust_scale_l20, py_scale_l20)


# ===========================================================================
# Stage 2: normalize_tick
# ===========================================================================
def bench_normalize_tick():
    _header("Stage 2: normalize_tick")

    def py_norm_tick():
        return (
            "tick",
            TICK_PAYLOAD["code"],
            int(round(TICK_PAYLOAD["close"] * SCALE)),
            TICK_PAYLOAD["volume"],
            TICK_PAYLOAD["ts"],
            TICK_PAYLOAD.get("tick_type", 0),
        )

    def rust_norm_tick():
        return rc.normalize_tick_tuple(TICK_PAYLOAD, TICK_PAYLOAD["code"], SCALE)

    _bench("normalize_tick_tuple", rust_norm_tick, py_norm_tick)


# ===========================================================================
# Stage 3: normalize_bidask
# ===========================================================================
def bench_normalize_bidask():
    _header("Stage 3: normalize_bidask")

    def py_norm_ba():
        bp, bv = BA_PAYLOAD["bid_price"], BA_PAYLOAD["bid_volume"]
        ap, av = BA_PAYLOAD["ask_price"], BA_PAYLOAD["ask_volume"]
        bids = [[int(round(p * SCALE)), int(v)] for p, v in zip(bp, bv) if p > 0]
        asks = [[int(round(p * SCALE)), int(v)] for p, v in zip(ap, av) if p > 0]
        bids_arr = np.array(bids, dtype=np.int64)
        asks_arr = np.array(asks, dtype=np.int64)
        bb = int(bids_arr[0, 0])
        ba = int(asks_arr[0, 0])
        bd = int(bids_arr[:, 1].sum())
        ad = int(asks_arr[:, 1].sum())
        mid = (bb + ba) / 2.0
        spread = float(ba - bb)
        ttop = int(bids_arr[0, 1] + asks_arr[0, 1])
        imb = ((int(bids_arr[0, 1]) - int(asks_arr[0, 1])) / ttop) if ttop else 0.0
        return ("bidask", "TXFD6", bids_arr, asks_arr, BA_PAYLOAD["ts"], False, bb, ba, bd, ad, mid, spread, imb)

    def rust_norm_ba():
        return rc.normalize_bidask_tuple_np(
            BA_PAYLOAD["code"],
            BA_PAYLOAD["ts"],
            L5_BID_P,
            L5_BID_V,
            L5_ASK_P,
            L5_ASK_V,
            SCALE,
        )

    _bench("normalize_bidask_tuple_np (L5)", rust_norm_ba, py_norm_ba)

    # Also test the dict-based path
    def rust_norm_ba_dict():
        return rc.normalize_bidask_tuple(BA_PAYLOAD, BA_PAYLOAD["code"], SCALE)

    _bench("normalize_bidask_tuple (dict)", rust_norm_ba_dict, py_norm_ba)


# ===========================================================================
# Stage 4: RingBuffer publish/consume
# ===========================================================================
def bench_ring_buffer():
    _header("Stage 4: RingBuffer (publish + consume)")

    event = ("tick", "TXFD6", 201000000, 5, 1700000000000000, 1)

    # Rust RingBuffer
    ring_rust = rc.FastRingBuffer(4096)
    rust_seq = [0]

    def rust_pub_consume():
        seq = rust_seq[0]
        ring_rust.set(seq, event)
        result = ring_rust.get(seq)
        rust_seq[0] = seq + 1
        return result

    # Python list-based ring
    py_buf = [None] * 4096
    py_cursor = [0]

    def py_pub_consume():
        seq = py_cursor[0]
        py_buf[seq % 4096] = event
        py_cursor[0] = seq + 1
        return py_buf[(seq) % 4096]

    _bench("publish + get (single event)", rust_pub_consume, py_pub_consume)


# ===========================================================================
# Stage 5: RustBookState (LOB state management)
# ===========================================================================
def bench_book_state():
    _header("Stage 5: RustBookState (LOB state update)")

    if not hasattr(rc, "RustBookState"):
        print("  ⬜ RustBookState not available, skipping")
        return

    rust_bs = rc.RustBookState("TXFD6")
    bids_scaled = np.array([[int(round(p * SCALE)), v] for p, v in zip(L5_BID_P, L5_BID_V)], dtype=np.int64)
    asks_scaled = np.array([[int(round(p * SCALE)), v] for p, v in zip(L5_ASK_P, L5_ASK_V)], dtype=np.int64)
    ts = [1700000000000000]

    def rust_book_update():
        ts[0] += 1
        rust_bs.apply_update(bids_scaled, asks_scaled, ts[0])

    # Python equivalent
    class PyBookState:
        __slots__ = (
            "best_bid",
            "best_ask",
            "bid_depth",
            "ask_depth",
            "mid_price_x2",
            "spread_scaled",
            "imbalance",
            "last_ts",
        )

        def __init__(self):
            self.best_bid = 0
            self.best_ask = 0
            self.bid_depth = 0
            self.ask_depth = 0
            self.mid_price_x2 = 0
            self.spread_scaled = 0
            self.imbalance = 0.0
            self.last_ts = 0

        def update(self, bids, asks, ts):
            if ts <= self.last_ts:
                return
            self.last_ts = ts
            if len(bids) > 0 and len(asks) > 0:
                self.best_bid = int(bids[0, 0])
                self.best_ask = int(asks[0, 0])
                self.bid_depth = int(bids[:, 1].sum())
                self.ask_depth = int(asks[:, 1].sum())
                self.mid_price_x2 = self.best_bid + self.best_ask
                self.spread_scaled = self.best_ask - self.best_bid
                ttop = int(bids[0, 1]) + int(asks[0, 1])
                self.imbalance = (int(bids[0, 1]) - int(asks[0, 1])) / ttop if ttop else 0.0

    py_bs = PyBookState()
    py_ts = [1700000000000000]

    def py_book_update():
        py_ts[0] += 1
        py_bs.update(bids_scaled, asks_scaled, py_ts[0])

    _bench("apply_update (L5)", rust_book_update, py_book_update)


# ===========================================================================
# Stage 6: Position Tracker
# ===========================================================================
def bench_position_tracker():
    _header("Stage 6: PositionTracker (6-fill sequence)")

    if not hasattr(rc, "RustPositionTracker"):
        print("  ⬜ RustPositionTracker not available, skipping")
        return

    key = "ACC:STRAT:TXFD6"

    def rust_pos():
        tracker = rc.RustPositionTracker()
        for side, qty, price, fee, tax in POS_FILLS:
            tracker.update(key, side, qty, price, fee, tax, 0)

    def py_pos():
        net = avg = pnl = fees = 0
        for side, qty, price, fee, tax in POS_FILLS:
            is_buy = side == 0
            signed = qty if is_buy else -qty
            fees += fee + tax
            cur_sign = 1 if net > 0 else (-1 if net < 0 else 0)
            fill_sign = 1 if is_buy else -1
            if cur_sign != 0 and fill_sign != cur_sign:
                cq = min(abs(net), qty)
                if is_buy:
                    pnl += (avg - price) * cq
                else:
                    pnl += (price - avg) * cq
                net += signed
                if net == 0:
                    avg = 0
                elif (cur_sign > 0 and net < 0) or (cur_sign < 0 and net > 0):
                    avg = price
            else:
                if net == 0:
                    avg = price
                    net += signed
                else:
                    total = net * avg + signed * price
                    net += signed
                    if net != 0:
                        avg = (2 * total + net) // (2 * net)

    _bench("6-fill round trip", rust_pos, py_pos, n=20_000)


# ===========================================================================
# Stage 7: Risk Validation (RustRiskValidator)
# ===========================================================================
def bench_risk_validator():
    _header("Stage 7: RiskValidator (PriceBand + MaxNotional)")

    if not hasattr(rc, "RustRiskValidator"):
        print("  ⬜ RustRiskValidator not available, skipping")
        return

    rv = rc.RustRiskValidator(
        band_ticks=100,
        max_notional_scaled=100_000_000_000,
    )

    def rust_validate():
        return rv.check(1000000, 10, 1000500, 0)  # price, qty, mid, intent_type

    def py_validate():
        price = 1000000
        qty = 10
        mid = 1000500
        band = 100
        max_not = 100_000_000_000
        if price <= 0:
            return False, "invalid_price"
        if mid > 0 and abs(price - mid) > band:
            return False, "price_band"
        if price * qty > max_not:
            return False, "max_notional"
        return True, "ok"

    _bench("check (price_band + max_notional)", rust_validate, py_validate)


# ===========================================================================
# Stage 8: Feature Engine Kernel (LobFeatureKernelV1)
# ===========================================================================
def bench_feature_kernel():
    _header("Stage 8: LobFeatureKernelV1 (16 features)")

    if not hasattr(rc, "LobFeatureKernelV1"):
        print("  ⬜ LobFeatureKernelV1 not available, skipping")
        return

    kernel = rc.LobFeatureKernelV1()

    def rust_fe():
        return kernel.update(
            FE_BEST_BID,
            FE_BEST_ASK,
            FE_MID_X2,
            FE_SPREAD,
            FE_BID_DEPTH,
            FE_ASK_DEPTH,
            FE_L1_BID_QTY,
            FE_L1_ASK_QTY,
        )

    # Python equivalent (simplified EMA + OFI computation)
    prev = {"mid": 0, "spread": 0, "imb": 0.0, "ofi": 0.0, "bid_p": 0, "ask_p": 0, "bid_v": 0, "ask_v": 0}
    alpha = 2.0 / 9.0

    def py_fe():
        mid = FE_MID_X2
        spread = FE_SPREAD
        imb = (FE_L1_BID_QTY - FE_L1_ASK_QTY) / max(FE_L1_BID_QTY + FE_L1_ASK_QTY, 1)
        # EMA updates
        ema_mid = prev["mid"] + alpha * (mid - prev["mid"]) if prev["mid"] else mid
        ema_spread = prev["spread"] + alpha * (spread - prev["spread"]) if prev["spread"] else spread
        ema_imb = prev["imb"] + alpha * (imb - prev["imb"])
        # OFI
        e_bid = FE_L1_BID_QTY - prev["bid_v"] if FE_BEST_BID == prev["bid_p"] else FE_L1_BID_QTY
        e_ask = FE_L1_ASK_QTY - prev["ask_v"] if FE_BEST_ASK == prev["ask_p"] else FE_L1_ASK_QTY
        ofi = e_bid - e_ask
        ema_ofi = prev["ofi"] + alpha * (ofi - prev["ofi"])
        prev["mid"] = ema_mid
        prev["spread"] = ema_spread
        prev["imb"] = ema_imb
        prev["ofi"] = ema_ofi
        prev["bid_p"] = FE_BEST_BID
        prev["ask_p"] = FE_BEST_ASK
        prev["bid_v"] = FE_L1_BID_QTY
        prev["ask_v"] = FE_L1_ASK_QTY
        return (
            mid,
            spread,
            int(imb * 10000),
            FE_BID_DEPTH,
            FE_ASK_DEPTH,
            FE_L1_BID_QTY,
            FE_L1_ASK_QTY,
            FE_BEST_BID,
            int(ema_mid),
            int(ema_spread),
            int(ema_imb * 10000),
            int(ema_ofi),
            int(ofi),
            0,
            0,
            0,
        )

    _bench("16-feature kernel update", rust_fe, py_fe)


# ===========================================================================
# Stage 9: DedupStore
# ===========================================================================
def bench_dedup():
    _header("Stage 9: DedupStore (idempotency check)")

    if not hasattr(rc, "RustDedupStore"):
        print("  ⬜ RustDedupStore not available, skipping")
        return

    rust_ds = rc.RustDedupStore(10000)
    py_seen = {}
    counter = [0]

    def rust_dedup():
        counter[0] += 1
        key = f"order_{counter[0] % 1000}"
        return rust_ds.check_and_reserve(key)

    def py_dedup():
        counter[0] += 1
        key = f"order_{counter[0] % 1000}"
        if key in py_seen:
            return False
        py_seen[key] = True
        return True

    _bench("check_and_reserve", rust_dedup, py_dedup, n=100_000)


# ===========================================================================
# Stage 10: CircuitBreaker
# ===========================================================================
def bench_circuit_breaker():
    _header("Stage 10: CircuitBreaker (state transition)")

    if not hasattr(rc, "RustCircuitBreaker"):
        print("  ⬜ RustCircuitBreaker not available, skipping")
        return

    cb = rc.RustCircuitBreaker(threshold=10, cooldown_ms=5000, recovery_threshold=3)

    def rust_cb():
        cb.record_success()
        return cb.state()

    # Python equivalent
    class PyCB:
        __slots__ = ("failures", "successes", "state_val")

        def __init__(self):
            self.failures = 0
            self.successes = 0
            self.state_val = 0

        def record_success(self):
            if self.state_val == 1:
                self.successes += 1
                if self.successes >= 3:
                    self.state_val = 0
                    self.failures = 0
                    self.successes = 0

        def state(self):
            return self.state_val

    py_cb = PyCB()

    def py_cb_fn():
        py_cb.record_success()
        return py_cb.state()

    _bench("record_success + state", rust_cb, py_cb_fn)


# ===========================================================================
# Summary
# ===========================================================================
def main():
    if rc is None:
        print("ERROR: rust_core not available. Run: uv run maturin develop --manifest-path rust_core/Cargo.toml")
        return

    print(f"\nRust Pipeline Benchmark — {N_ITERS:,} iterations per test")
    print(f"rust_core loaded: {rc.__file__ if hasattr(rc, '__file__') else 'yes'}")

    benches = [
        bench_scale_book,
        bench_normalize_tick,
        bench_normalize_bidask,
        bench_ring_buffer,
        bench_book_state,
        bench_position_tracker,
        bench_risk_validator,
        bench_feature_kernel,
        bench_dedup,
        bench_circuit_breaker,
    ]
    for fn in benches:
        try:
            fn()
        except Exception as e:
            print(f"  ⚠️  {fn.__name__} skipped: {e}")

    print(f"\n{'=' * 60}")
    print("  Legend: 🟢 Rust >1.2x faster  ⚪ ~parity  🔴 Rust slower")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
