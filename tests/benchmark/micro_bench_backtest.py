"""WU-13: Micro-benchmark harness for backtest adapter.

Measures:
  - Per-tick fill recording overhead
  - Per-tick equity sampling overhead
  - Vectorized fill_stats computation

Run: uv run pytest tests/benchmark/micro_bench_backtest.py -v
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from hft_platform.backtest import adapter as hbt_adapter
from hft_platform.backtest._equity_core import compute_equity_from_positions
from hft_platform.strategy.base import BaseStrategy


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------
class _Depth:
    best_bid = 10000
    best_ask = 10010
    best_bid_qty = 100
    best_ask_qty = 200


class _Hbt:
    def __init__(self, *a, **kw):
        self._ran = False
        self.current_timestamp = 123
        self.submitted = []

    def wait_next_feed(self, *_a, **_kw):
        return 1

    def depth(self, *_a, **_kw):
        return _Depth()

    def position(self, *_a, **_kw):
        return 0

    def submit_buy_order(self, *a, **kw):
        pass

    def submit_sell_order(self, *a, **kw):
        pass

    def cancel(self, *_a, **_kw):
        pass

    def close(self):
        return True


class _BacktestAsset:
    def data(self, *_a, **_kw):
        return self

    def linear_asset(self, *_a, **_kw):
        return self

    def power_prob_queue_model(self, *_a, **_kw):
        return self

    def int_order_id_converter(self):
        return self


class _NoopStrategy(BaseStrategy):
    def on_stats(self, event):
        pass


def _patch(monkeypatch):
    monkeypatch.setattr(hbt_adapter, "HFTBACKTEST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(hbt_adapter, "HashMapMarketDepthBacktest", _Hbt, raising=False)
    monkeypatch.setattr(hbt_adapter, "BacktestAsset", _BacktestAsset, raising=False)
    monkeypatch.setattr(hbt_adapter, "IOC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "GTC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "LIMIT", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "_detect_wait_status_mode", lambda: "modern", raising=False)


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------
N_TICKS = 100_000


def test_bench_fill_recording(monkeypatch):
    """Measure per-fill SoA recording overhead."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )

    t0 = time.perf_counter_ns()
    for i in range(N_TICKS):
        adapter._record_fill(i * 1000, 1, i + 1, 20000 + i)
    elapsed_ns = time.perf_counter_ns() - t0

    per_fill_us = elapsed_ns / N_TICKS / 1000.0
    print(f"\nFill recording: {per_fill_us:.3f} us/fill ({N_TICKS} fills)")
    assert per_fill_us < 10, f"Fill recording too slow: {per_fill_us:.3f} us"


def test_bench_equity_sampling(monkeypatch):
    """Measure per-tick equity sampling overhead."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        equity_sample_ns=1,
    )
    adapter._reset_equity_buffers()

    t0 = time.perf_counter_ns()
    for i in range(N_TICKS):
        adapter._maybe_record_equity_point(i, 10000, 10010)
    elapsed_ns = time.perf_counter_ns() - t0

    per_tick_us = elapsed_ns / N_TICKS / 1000.0
    print(f"\nEquity sampling: {per_tick_us:.3f} us/tick ({N_TICKS} ticks)")
    assert per_tick_us < 10, f"Equity sampling too slow: {per_tick_us:.3f} us"


def test_bench_fill_stats_vectorized(monkeypatch):
    """Measure fill_stats computation on large fill log."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
    )
    n = 10_000
    adapter._total_buy_fills = n // 2
    adapter._total_sell_fills = n // 2
    for i in range(n):
        delta = 1 if i % 2 == 0 else -1
        adapter._record_fill(i * 1_000_000, delta, i, 20000 + i * 10)

    t0 = time.perf_counter_ns()
    stats = adapter.fill_stats
    elapsed_ns = time.perf_counter_ns() - t0

    elapsed_ms = elapsed_ns / 1_000_000.0
    print(f"\nFill stats ({n} fills): {elapsed_ms:.3f} ms")
    assert stats["n_fill_events"] == n
    assert elapsed_ms < 50, f"fill_stats too slow: {elapsed_ms:.3f} ms"


def test_bench_equity_core_100k():
    """Measure compute_equity_from_positions for 100K ticks."""
    np.random.seed(42)
    prices = np.cumsum(np.random.randn(N_TICKS)) + 1000.0
    positions = np.clip(np.cumsum(np.sign(np.random.randn(N_TICKS))), -5, 5).astype(np.float64)

    t0 = time.perf_counter_ns()
    eq = compute_equity_from_positions(prices, positions, fee_rate=0.001)
    elapsed_ns = time.perf_counter_ns() - t0

    elapsed_ms = elapsed_ns / 1_000_000.0
    print(f"\nEquity core ({N_TICKS} ticks): {elapsed_ms:.3f} ms")
    assert len(eq) == N_TICKS
    assert elapsed_ms < 50, f"Equity computation too slow: {elapsed_ms:.3f} ms"


def test_bench_save_baseline(monkeypatch):
    """Save baseline results for regression detection."""
    _patch(monkeypatch)
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=_NoopStrategy("t"),
        asset_symbol="X",
        data_path="d",
        equity_sample_ns=1,
    )

    # Fill recording
    t0 = time.perf_counter_ns()
    for i in range(10_000):
        adapter._record_fill(i * 1000, 1, i + 1, 20000)
    fill_us = (time.perf_counter_ns() - t0) / 10_000 / 1000.0

    # Equity sampling
    adapter._reset_equity_buffers()
    t0 = time.perf_counter_ns()
    for i in range(10_000):
        adapter._maybe_record_equity_point(i, 10000, 10010)
    equity_us = (time.perf_counter_ns() - t0) / 10_000 / 1000.0

    baseline = {
        "fill_recording_us_per_fill": round(fill_us, 3),
        "equity_sampling_us_per_tick": round(equity_us, 3),
        "regression_threshold_pct": 25,
    }

    baseline_dir = Path(__file__).parent / "baselines"
    baseline_dir.mkdir(exist_ok=True)
    baseline_path = baseline_dir / "backtest_adapter.json"
    baseline_path.write_text(json.dumps(baseline, indent=2))
    print(f"\nBaseline saved: {baseline}")
