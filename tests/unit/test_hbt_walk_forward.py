from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from research.backtest.hft_native_runner import HftNativeRunner
from research.backtest.types import BacktestConfig, WalkForwardConfig
from research.registry.schemas import AlphaManifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_feed(path: Path, n: int, *, seed: int = 7) -> None:
    """Create a minimal hftbt.npz for HftNativeRunner walk-forward tests."""
    dt = np.dtype([
        ("ev", "i8"), ("exch_ts", "i8"), ("local_ts", "i8"),
        ("px", "f8"), ("qty", "f8"), ("a", "i4"), ("b", "i4"), ("c", "f8"),
    ])
    rng = np.random.default_rng(seed)
    arr = np.zeros(n, dtype=dt)
    arr["exch_ts"] = np.arange(n) * 1_000_000
    arr["local_ts"] = arr["exch_ts"]
    arr["px"] = 100.0 + rng.normal(0, 0.5, n)
    arr["qty"] = 10.0
    np.savez_compressed(str(path), data=arr)


def _cfg(path: Path) -> BacktestConfig:
    return BacktestConfig(
        data_paths=[str(path)],
        maker_fee_bps=0.0,
        taker_fee_bps=0.0,
        signal_threshold=0.1,
        local_decision_pipeline_latency_us=0,
        submit_ack_latency_ms=0.0,
        modify_ack_latency_ms=0.0,
        cancel_ack_latency_ms=0.0,
        live_uplift_factor=1.0,
    )


class _ConstAlpha:
    def __init__(self, signal: float) -> None:
        self.signal = float(signal)
        self.reset_calls = 0
        self.manifest = AlphaManifest(
            alpha_id="const_alpha",
            hypothesis="test",
            formula="const",
            paper_refs=(),
            data_fields=("ofi", "qty"),
            complexity="O(1)",
        )

    def reset(self) -> None:
        self.reset_calls += 1

    def update(self, **_: float) -> float:
        return self.signal

    def get_signal(self) -> float:
        return self.signal


@pytest.fixture
def mock_adapter_slice(monkeypatch):
    """Bypass hftbacktest execution; return deterministic fold results."""

    def _fake(alpha, npz_path, config, symbol="ASSET"):
        # Mirror what AlphaStrategyBridge.reset() → alpha.reset() would do
        alpha.reset()
        n = 30
        rng = np.random.default_rng(42)
        sig_val = float(alpha.signal) if hasattr(alpha, "signal") else 1.0
        equity = np.cumprod(1 + rng.normal(0.0002 * (1 if sig_val >= 0 else -1), 0.001, n))
        equity *= config.initial_equity
        signals = np.full(n, sig_val)
        mid = np.linspace(100, 101, n)
        pos = np.zeros(n)
        return equity, signals, mid, pos

    monkeypatch.setattr("research.backtest.hft_native_runner._run_adapter_slice", _fake)


# ---------------------------------------------------------------------------
# Walk-forward tests
# ---------------------------------------------------------------------------

def test_walk_forward_n_folds(tmp_path: Path, mock_adapter_slice) -> None:
    path = tmp_path / "hftbt.npz"
    _make_feed(path, n=120)
    alpha = _ConstAlpha(1.0)
    runner = HftNativeRunner(alpha, _cfg(path))
    result = runner.run_walk_forward(alpha, WalkForwardConfig(n_splits=3, min_train_samples=5))
    assert len(result.folds) == 3


def test_walk_forward_consistency_pct(tmp_path: Path, mock_adapter_slice) -> None:
    path = tmp_path / "hftbt.npz"
    _make_feed(path, n=160, seed=11)
    alpha = _ConstAlpha(1.0)
    runner = HftNativeRunner(alpha, _cfg(path))
    result = runner.run_walk_forward(alpha, WalkForwardConfig(n_splits=4, min_train_samples=5))
    assert result.fold_consistency_pct == 1.0


def test_walk_forward_negative_consistency(tmp_path: Path, mock_adapter_slice, monkeypatch) -> None:
    """Signal=-1.0 → equity trends down → sharpe < 0 → consistency = 0.0."""
    path = tmp_path / "hftbt.npz"
    _make_feed(path, n=160, seed=21)

    # Override mock for negative equity path
    def _fake_neg(alpha, npz_path, config, symbol="ASSET"):
        alpha.reset()
        n = 30
        rng = np.random.default_rng(42)
        equity = np.cumprod(1 + rng.normal(-0.001, 0.0005, n)) * config.initial_equity
        signals = np.full(n, -1.0)
        mid = np.linspace(100, 99, n)
        pos = np.zeros(n)
        return equity, signals, mid, pos

    monkeypatch.setattr("research.backtest.hft_native_runner._run_adapter_slice", _fake_neg)
    alpha = _ConstAlpha(-1.0)
    runner = HftNativeRunner(alpha, _cfg(path))
    result = runner.run_walk_forward(alpha, WalkForwardConfig(n_splits=4, min_train_samples=5))
    assert result.fold_consistency_pct == 0.0


def test_walk_forward_fold_sharpe_min(tmp_path: Path, mock_adapter_slice) -> None:
    path = tmp_path / "hftbt.npz"
    _make_feed(path, n=180, seed=31)
    alpha = _ConstAlpha(0.8)
    runner = HftNativeRunner(alpha, _cfg(path))
    result = runner.run_walk_forward(alpha, WalkForwardConfig(n_splits=5, min_train_samples=5))
    assert result.fold_sharpe_min <= result.fold_sharpe_mean <= result.fold_sharpe_max


def test_walk_forward_too_small_data(tmp_path: Path, mock_adapter_slice) -> None:
    path = tmp_path / "hftbt.npz"
    _make_feed(path, n=10, seed=41)
    alpha = _ConstAlpha(1.0)
    runner = HftNativeRunner(alpha, _cfg(path))
    result = runner.run_walk_forward(alpha, WalkForwardConfig(n_splits=5))
    assert result.folds == []
    assert math.isnan(result.fold_consistency_pct)


def test_walk_forward_resets_alpha(tmp_path: Path, mock_adapter_slice) -> None:
    """HftNativeRunner calls alpha.reset() once per fold via _run_adapter_slice."""
    path = tmp_path / "hftbt.npz"
    _make_feed(path, n=140, seed=51)
    alpha = _ConstAlpha(1.0)
    runner = HftNativeRunner(alpha, _cfg(path))
    result = runner.run_walk_forward(alpha, WalkForwardConfig(n_splits=4, min_train_samples=5))
    assert alpha.reset_calls == len(result.folds)


# ---------------------------------------------------------------------------
# P3: auto_regime_split in BacktestConfig
# ---------------------------------------------------------------------------


def test_backtest_config_auto_regime_split_default_true() -> None:
    """BacktestConfig.auto_regime_split defaults to True."""
    cfg = BacktestConfig(data_paths=[])
    assert cfg.auto_regime_split is True


def test_backtest_run_auto_regime_split_populates_regime_metrics(
    tmp_path: Path, mock_adapter_slice
) -> None:
    """With auto_regime_split=True, regime_metrics populated via HftNativeRunner."""
    path = tmp_path / "hftbt.npz"
    _make_feed(path, n=120, seed=77)
    alpha = _ConstAlpha(0.5)

    # Override mock to return varied signals/prices so regime detection works
    def _fake_regime(a, npz_path, config, symbol="ASSET"):
        a.reset()
        n = 40
        rng = np.random.default_rng(77)
        equity = np.cumprod(1 + rng.normal(0.0002, 0.001, n)) * config.initial_equity
        signals = rng.uniform(-1, 1, n)
        mid = 100 + np.cumsum(rng.normal(0, 0.01, n))
        pos = np.zeros(n)
        return equity, signals, mid, pos

    import unittest.mock as mock
    with mock.patch("research.backtest.hft_native_runner._run_adapter_slice", _fake_regime):
        cfg = BacktestConfig(
            data_paths=[str(path)],
            signal_threshold=0.1,
            maker_fee_bps=0.0,
            taker_fee_bps=0.0,
            local_decision_pipeline_latency_us=0,
            submit_ack_latency_ms=0.0,
            modify_ack_latency_ms=0.0,
            cancel_ack_latency_ms=0.0,
            live_uplift_factor=1.0,
            auto_regime_split=True,
        )
        runner = HftNativeRunner(alpha, cfg)
        result = runner.run()
    assert "high_vol" in result.regime_metrics or "low_vol" in result.regime_metrics


def test_backtest_run_auto_regime_split_false_skips_regime(
    tmp_path: Path, mock_adapter_slice
) -> None:
    """With auto_regime_split=False, regime_metrics is empty."""
    path = tmp_path / "hftbt.npz"
    _make_feed(path, n=120, seed=88)
    alpha = _ConstAlpha(0.5)
    cfg = BacktestConfig(
        data_paths=[str(path)],
        signal_threshold=0.1,
        maker_fee_bps=0.0,
        taker_fee_bps=0.0,
        local_decision_pipeline_latency_us=0,
        submit_ack_latency_ms=0.0,
        modify_ack_latency_ms=0.0,
        cancel_ack_latency_ms=0.0,
        live_uplift_factor=1.0,
        auto_regime_split=False,
    )
    runner = HftNativeRunner(alpha, cfg)
    result = runner.run()
    assert result.regime_metrics == {}
