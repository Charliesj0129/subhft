from __future__ import annotations

import types

import numpy as np
import pytest

from hft_platform.alpha.validation import (
    _optimize_parameters,
    _run_grid_parallel,
    _run_single_grid_point,
)
from research.backtest.types import BacktestConfig

# ---------------------------------------------------------------------------
# Deterministic runner that returns threshold-dependent results.
# Must be top-level (picklable) for ProcessPoolExecutor.
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Fake backtest runner that produces deterministic results from threshold."""

    def __init__(self, alpha: object, cfg: BacktestConfig) -> None:
        self.cfg = cfg

    def run(self) -> types.SimpleNamespace:
        t = float(self.cfg.signal_threshold)
        # Peak sharpe at threshold=0.3, symmetric falloff
        sharpe_oos = 2.0 - abs(t - 0.3) * 8.0
        return types.SimpleNamespace(
            sharpe_is=sharpe_oos + 0.2,
            sharpe_oos=sharpe_oos,
            max_drawdown=-0.1,
            turnover=0.4,
            run_id=f"r-{t:.4f}",
            config_hash=f"c-{t:.4f}",
        )


def _make_validation_config(**overrides):
    from hft_platform.alpha.validation import ValidationConfig

    defaults = {
        "alpha_id": "test",
        "data_paths": ["d.npy"],
        "opt_signal_threshold_min": 0.1,
        "opt_signal_threshold_max": 0.5,
        "opt_signal_threshold_steps": 5,
        "opt_objective": "sharpe_oos",
    }
    defaults.update(overrides)
    return ValidationConfig(**defaults)


def _make_base_result(threshold: float = 0.3):
    runner = _FakeRunner(object(), BacktestConfig(data_paths=[], signal_threshold=threshold))
    r = runner.run()
    return types.SimpleNamespace(
        sharpe_is=r.sharpe_is,
        sharpe_oos=r.sharpe_oos,
        max_drawdown=r.max_drawdown,
        turnover=r.turnover,
        run_id="base",
        config_hash="base",
        equity_curve=np.ones(100, dtype=np.float64),
    )


# ---------------------------------------------------------------------------
# _run_single_grid_point
# ---------------------------------------------------------------------------


class TestRunSingleGridPoint:
    def test_returns_expected_keys(self):
        row = _run_single_grid_point(object(), _FakeRunner, BacktestConfig(data_paths=[]), 0.2, "sharpe_oos")
        expected_keys = {
            "signal_threshold",
            "sharpe_is",
            "sharpe_oos",
            "max_drawdown",
            "turnover",
            "objective",
            "run_id",
            "config_hash",
        }
        assert set(row.keys()) == expected_keys

    def test_threshold_propagated(self):
        row = _run_single_grid_point(object(), _FakeRunner, BacktestConfig(data_paths=[]), 0.15, "sharpe_oos")
        assert row["signal_threshold"] == pytest.approx(0.15)

    def test_objective_uses_sharpe_oos_mode(self):
        row = _run_single_grid_point(object(), _FakeRunner, BacktestConfig(data_paths=[]), 0.3, "sharpe_oos")
        assert row["objective"] == pytest.approx(row["sharpe_oos"])


# ---------------------------------------------------------------------------
# _run_grid_parallel
# ---------------------------------------------------------------------------


class TestRunGridParallel:
    def test_reuses_base_result(self):
        """Base threshold row should use pre-computed base_result, not re-run."""
        grid = np.array([0.1, 0.3, 0.5], dtype=np.float64)
        base_result = _make_base_result(0.3)
        rows = _run_grid_parallel(
            alpha=object(),
            runner_cls=_FakeRunner,
            base_cfg=BacktestConfig(data_paths=[], signal_threshold=0.3),
            base_result=base_result,
            base_threshold=0.3,
            grid=grid,
            objective_mode="sharpe_oos",
        )
        base_row = [r for r in rows if abs(r["signal_threshold"] - 0.3) < 1e-12][0]
        assert base_row["run_id"] == "base"
        assert base_row["config_hash"] == "base"

    def test_all_grid_points_present(self):
        grid = np.array([0.1, 0.2, 0.3, 0.4, 0.5], dtype=np.float64)
        rows = _run_grid_parallel(
            alpha=object(),
            runner_cls=_FakeRunner,
            base_cfg=BacktestConfig(data_paths=[], signal_threshold=0.3),
            base_result=_make_base_result(0.3),
            base_threshold=0.3,
            grid=grid,
            objective_mode="sharpe_oos",
        )
        assert len(rows) == 5
        thresholds = [r["signal_threshold"] for r in rows]
        for t in [0.1, 0.2, 0.3, 0.4, 0.5]:
            assert any(abs(th - t) < 1e-9 for th in thresholds)

    def test_sorted_by_threshold(self):
        grid = np.array([0.5, 0.1, 0.3, 0.2, 0.4], dtype=np.float64)
        rows = _run_grid_parallel(
            alpha=object(),
            runner_cls=_FakeRunner,
            base_cfg=BacktestConfig(data_paths=[], signal_threshold=0.3),
            base_result=_make_base_result(0.3),
            base_threshold=0.3,
            grid=grid,
            objective_mode="sharpe_oos",
        )
        thresholds = [r["signal_threshold"] for r in rows]
        assert thresholds == sorted(thresholds)

    def test_only_base_in_grid(self):
        grid = np.array([0.3], dtype=np.float64)
        rows = _run_grid_parallel(
            alpha=object(),
            runner_cls=_FakeRunner,
            base_cfg=BacktestConfig(data_paths=[], signal_threshold=0.3),
            base_result=_make_base_result(0.3),
            base_threshold=0.3,
            grid=grid,
            objective_mode="sharpe_oos",
        )
        assert len(rows) == 1
        assert rows[0]["run_id"] == "base"


# ---------------------------------------------------------------------------
# _optimize_parameters — output format parity
# ---------------------------------------------------------------------------


class TestOptimizeParametersParallel:
    def test_output_format_matches_sequential(self):
        """Verify output dict has all expected top-level keys."""
        cfg = _make_validation_config()
        out = _optimize_parameters(
            alpha=object(),
            base_cfg=BacktestConfig(data_paths=[], signal_threshold=0.3),
            base_result=_make_base_result(),
            config=cfg,
            runner_cls=_FakeRunner,
        )
        required_keys = {
            "enabled",
            "passed",
            "objective",
            "selected_signal_threshold",
            "selected_row",
            "base_signal_threshold",
            "grid",
            "trials",
            "top_k",
            "deflated_sharpe",
            "selection_penalty",
            "neighbor_objective_ratio",
            "risks",
        }
        assert required_keys.issubset(set(out.keys()))

    def test_all_grid_points_in_trials(self):
        """Each unique threshold in the grid produces exactly one trial row."""
        cfg = _make_validation_config(
            opt_signal_threshold_min=0.1,
            opt_signal_threshold_max=0.5,
            opt_signal_threshold_steps=5,
        )
        out = _optimize_parameters(
            alpha=object(),
            base_cfg=BacktestConfig(data_paths=[], signal_threshold=0.3),
            base_result=_make_base_result(),
            config=cfg,
            runner_cls=_FakeRunner,
        )
        # Every trial threshold must appear in the grid
        for row in out["trials"]:
            t = row["signal_threshold"]
            assert any(abs(t - g) < 1e-9 for g in out["grid"]), f"trial threshold {t} not found in grid {out['grid']}"
        # No duplicate thresholds in trials (within tolerance)
        trial_ts = [r["signal_threshold"] for r in out["trials"]]
        for i, t in enumerate(trial_ts):
            for j in range(i + 1, len(trial_ts)):
                assert abs(t - trial_ts[j]) >= 1e-12, f"duplicate threshold {t} in trials"

    def test_selects_best_threshold(self):
        """Best threshold should be at 0.3 (peak sharpe in _FakeRunner)."""
        cfg = _make_validation_config()
        out = _optimize_parameters(
            alpha=object(),
            base_cfg=BacktestConfig(data_paths=[], signal_threshold=0.3),
            base_result=_make_base_result(),
            config=cfg,
            runner_cls=_FakeRunner,
        )
        assert abs(out["selected_signal_threshold"] - 0.3) < 1e-9

    def test_disabled_returns_early(self):
        cfg = _make_validation_config(enable_param_optimization=False)
        out = _optimize_parameters(
            alpha=object(),
            base_cfg=BacktestConfig(data_paths=[], signal_threshold=0.3),
            base_result=_make_base_result(),
            config=cfg,
            runner_cls=_FakeRunner,
        )
        assert out["enabled"] is False
        assert out["passed"] is True
        assert out["grid"] == []


# ---------------------------------------------------------------------------
# HFT_OPT_WORKERS env var
# ---------------------------------------------------------------------------


class TestOptWorkersEnv:
    def test_workers_1_sequential(self, monkeypatch):
        """HFT_OPT_WORKERS=1 should still produce correct results."""
        monkeypatch.setenv("HFT_OPT_WORKERS", "1")
        cfg = _make_validation_config(opt_signal_threshold_steps=4)
        out = _optimize_parameters(
            alpha=object(),
            base_cfg=BacktestConfig(data_paths=[], signal_threshold=0.3),
            base_result=_make_base_result(),
            config=cfg,
            runner_cls=_FakeRunner,
        )
        assert out["enabled"] is True
        assert len(out["trials"]) >= 4
        assert abs(out["selected_signal_threshold"] - 0.3) < 1e-9

    def test_workers_4_parallel(self, monkeypatch):
        """HFT_OPT_WORKERS=4 should produce identical results."""
        monkeypatch.setenv("HFT_OPT_WORKERS", "4")
        cfg = _make_validation_config(opt_signal_threshold_steps=8)
        out = _optimize_parameters(
            alpha=object(),
            base_cfg=BacktestConfig(data_paths=[], signal_threshold=0.3),
            base_result=_make_base_result(),
            config=cfg,
            runner_cls=_FakeRunner,
        )
        assert out["enabled"] is True
        assert len(out["trials"]) >= 8
        # Trials should be sorted by threshold
        thresholds = [r["signal_threshold"] for r in out["trials"]]
        assert thresholds == sorted(thresholds)

    def test_workers_1_and_4_produce_same_results(self, monkeypatch):
        """Sequential and parallel produce identical results."""
        results = {}
        for workers in ("1", "4"):
            monkeypatch.setenv("HFT_OPT_WORKERS", workers)
            cfg = _make_validation_config(opt_signal_threshold_steps=5)
            out = _optimize_parameters(
                alpha=object(),
                base_cfg=BacktestConfig(data_paths=[], signal_threshold=0.3),
                base_result=_make_base_result(),
                config=cfg,
                runner_cls=_FakeRunner,
            )
            results[workers] = out

        r1, r4 = results["1"], results["4"]
        assert r1["grid"] == r4["grid"]
        assert len(r1["trials"]) == len(r4["trials"])
        for t1, t4 in zip(r1["trials"], r4["trials"]):
            assert t1["signal_threshold"] == pytest.approx(t4["signal_threshold"])
            assert t1["sharpe_oos"] == pytest.approx(t4["sharpe_oos"])
            assert t1["objective"] == pytest.approx(t4["objective"])
        assert r1["selected_signal_threshold"] == pytest.approx(r4["selected_signal_threshold"])
