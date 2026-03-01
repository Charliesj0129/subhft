from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from research.backtest.hbt_runner import BacktestConfig, ResearchBacktestRunner, WalkForwardConfig
from research.registry.schemas import AlphaManifest


def _make_feed(path: Path, n: int, *, seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    steps = rng.normal(loc=0.02, scale=0.05, size=n).astype(np.float64)
    mid = 100.0 + np.cumsum(steps)
    arr = np.zeros(n, dtype=[("mid", "f8"), ("ofi", "f8"), ("qty", "f8"), ("local_ts", "i8")])
    arr["mid"] = mid
    arr["ofi"] = rng.normal(loc=0.0, scale=1.0, size=n)
    arr["qty"] = rng.uniform(1.0, 10.0, size=n)
    arr["local_ts"] = np.arange(n, dtype=np.int64) * 1_000_000
    np.save(path, arr)


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


def test_walk_forward_n_folds(tmp_path: Path) -> None:
    path = tmp_path / "wf.npy"
    _make_feed(path, n=120)
    alpha = _ConstAlpha(1.0)
    runner = ResearchBacktestRunner(alpha, _cfg(path))
    result = runner.run_walk_forward(alpha, WalkForwardConfig(n_splits=3, min_train_samples=5))
    assert len(result.folds) == 3


def test_walk_forward_consistency_pct(tmp_path: Path) -> None:
    path = tmp_path / "wf_pos.npy"
    _make_feed(path, n=160, seed=11)
    alpha = _ConstAlpha(1.0)
    runner = ResearchBacktestRunner(alpha, _cfg(path))
    result = runner.run_walk_forward(alpha, WalkForwardConfig(n_splits=4, min_train_samples=5))
    assert result.fold_consistency_pct == 1.0


def test_walk_forward_negative_consistency(tmp_path: Path) -> None:
    path = tmp_path / "wf_neg.npy"
    _make_feed(path, n=160, seed=21)
    alpha = _ConstAlpha(-1.0)
    runner = ResearchBacktestRunner(alpha, _cfg(path))
    result = runner.run_walk_forward(alpha, WalkForwardConfig(n_splits=4, min_train_samples=5))
    assert result.fold_consistency_pct == 0.0


def test_walk_forward_fold_sharpe_min(tmp_path: Path) -> None:
    path = tmp_path / "wf_bounds.npy"
    _make_feed(path, n=180, seed=31)
    alpha = _ConstAlpha(0.8)
    runner = ResearchBacktestRunner(alpha, _cfg(path))
    result = runner.run_walk_forward(alpha, WalkForwardConfig(n_splits=5, min_train_samples=5))
    assert result.fold_sharpe_min <= result.fold_sharpe_mean <= result.fold_sharpe_max


def test_walk_forward_too_small_data(tmp_path: Path) -> None:
    path = tmp_path / "wf_small.npy"
    _make_feed(path, n=10, seed=41)
    alpha = _ConstAlpha(1.0)
    runner = ResearchBacktestRunner(alpha, _cfg(path))
    result = runner.run_walk_forward(alpha, WalkForwardConfig(n_splits=5))
    assert result.folds == []
    assert math.isnan(result.fold_consistency_pct)


def test_walk_forward_resets_alpha(tmp_path: Path) -> None:
    path = tmp_path / "wf_reset.npy"
    _make_feed(path, n=140, seed=51)
    alpha = _ConstAlpha(1.0)
    runner = ResearchBacktestRunner(alpha, _cfg(path))
    result = runner.run_walk_forward(alpha, WalkForwardConfig(n_splits=4, min_train_samples=5))
    assert alpha.reset_calls == len(result.folds)


# ---------------------------------------------------------------------------
# P3: auto_regime_split in BacktestConfig
# ---------------------------------------------------------------------------


def test_backtest_config_auto_regime_split_default_true() -> None:
    """BacktestConfig.auto_regime_split defaults to True."""
    cfg = BacktestConfig(data_paths=[])
    assert cfg.auto_regime_split is True


def test_backtest_run_auto_regime_split_populates_regime_metrics(tmp_path: Path) -> None:
    """With auto_regime_split=True (default), regime_metrics is populated."""
    path = tmp_path / "regime_feed.npy"
    _make_feed(path, n=120, seed=77)
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
        auto_regime_split=True,
    )
    runner = ResearchBacktestRunner(alpha, cfg)
    result = runner.run()
    assert "high_vol" in result.regime_metrics
    assert "low_vol" in result.regime_metrics


def test_backtest_run_auto_regime_split_false_skips_regime(tmp_path: Path) -> None:
    """With auto_regime_split=False, regime_metrics is empty."""
    path = tmp_path / "regime_feed_off.npy"
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
    runner = ResearchBacktestRunner(alpha, cfg)
    result = runner.run()
    assert result.regime_metrics == {}
