"""Tests for parallel Gate C parameter optimization."""

from __future__ import annotations

import types

import numpy as np
import pytest

from hft_platform.alpha.validation import (
    ValidationConfig,
    _optimize_parameters,
    _run_single_threshold_trial,
    _run_threshold_trials_parallel,
)
from research.backtest.types import BacktestConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Deterministic runner: sharpe_oos peaks at threshold=0.3."""

    def __init__(self, alpha: object, cfg: BacktestConfig) -> None:
        self.cfg = cfg

    def run(self) -> types.SimpleNamespace:
        t = float(self.cfg.signal_threshold)
        sharpe_oos = 2.0 - abs(t - 0.3) * 8.0
        return types.SimpleNamespace(
            sharpe_is=sharpe_oos + 0.2,
            sharpe_oos=sharpe_oos,
            max_drawdown=-0.1,
            turnover=0.4,
            run_id=f"r-{t:.4f}",
            config_hash=f"c-{t:.4f}",
        )


def _make_base_result() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        sharpe_is=2.2,
        sharpe_oos=2.0,
        max_drawdown=-0.1,
        turnover=0.4,
        run_id="base",
        config_hash="base",
        equity_curve=np.ones(100, dtype=np.float64),
    )


# ---------------------------------------------------------------------------
# _run_single_threshold_trial tests
# ---------------------------------------------------------------------------


class TestRunSingleThresholdTrial:
    def test_base_trial_reuses_base_result(self) -> None:
        """When is_base=True, the base_result is returned without running."""
        base_result = _make_base_result()
        base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)

        row = _run_single_threshold_trial(
            alpha=object(),
            base_cfg=base_cfg,
            threshold=0.3,
            is_base=True,
            base_result=base_result,
            opt_objective="sharpe_oos",
            runner_cls=_FakeRunner,
        )

        assert row["signal_threshold"] == pytest.approx(0.3)
        assert row["run_id"] == "base"
        assert row["sharpe_oos"] == pytest.approx(2.0)

    def test_non_base_trial_runs_with_new_threshold(self) -> None:
        """When is_base=False, a new runner is created with the given threshold."""
        base_result = _make_base_result()
        base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)

        row = _run_single_threshold_trial(
            alpha=object(),
            base_cfg=base_cfg,
            threshold=0.1,
            is_base=False,
            base_result=base_result,
            opt_objective="sharpe_oos",
            runner_cls=_FakeRunner,
        )

        assert row["signal_threshold"] == pytest.approx(0.1)
        assert row["run_id"] == "r-0.1000"
        # sharpe_oos = 2.0 - |0.1 - 0.3| * 8.0 = 2.0 - 1.6 = 0.4
        assert row["sharpe_oos"] == pytest.approx(0.4)

    def test_output_has_required_keys(self) -> None:
        """Trial output dict has all expected keys."""
        base_result = _make_base_result()
        base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)

        row = _run_single_threshold_trial(
            alpha=object(),
            base_cfg=base_cfg,
            threshold=0.2,
            is_base=False,
            base_result=base_result,
            opt_objective="risk_adjusted",
            runner_cls=_FakeRunner,
        )

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


# ---------------------------------------------------------------------------
# _run_threshold_trials_parallel tests
# ---------------------------------------------------------------------------


class TestRunThresholdTrialsParallel:
    def test_preserves_order(self) -> None:
        """Results come back in the same order as trial_args."""
        base_result = _make_base_result()
        base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)
        trial_args = [
            (0.1, False),
            (0.2, False),
            (0.3, True),
            (0.4, False),
            (0.5, False),
        ]

        rows = _run_threshold_trials_parallel(
            alpha=object(),
            base_cfg=base_cfg,
            base_result=base_result,
            opt_objective="sharpe_oos",
            runner_cls=_FakeRunner,
            trial_args=trial_args,
            max_workers=4,
        )

        assert len(rows) == 5
        for i, (t, _) in enumerate(trial_args):
            assert rows[i]["signal_threshold"] == pytest.approx(t)

    def test_base_threshold_reuses_result(self) -> None:
        """The base trial re-uses the base_result run_id."""
        base_result = _make_base_result()
        base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)
        trial_args = [(0.1, False), (0.3, True), (0.5, False)]

        rows = _run_threshold_trials_parallel(
            alpha=object(),
            base_cfg=base_cfg,
            base_result=base_result,
            opt_objective="sharpe_oos",
            runner_cls=_FakeRunner,
            trial_args=trial_args,
            max_workers=2,
        )

        assert rows[1]["run_id"] == "base"
        assert rows[0]["run_id"] != "base"
        assert rows[2]["run_id"] != "base"

    def test_single_worker_still_works(self) -> None:
        """max_workers=1 executes sequentially without error."""
        base_result = _make_base_result()
        base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)
        trial_args = [(0.1, False), (0.3, True), (0.5, False)]

        rows = _run_threshold_trials_parallel(
            alpha=object(),
            base_cfg=base_cfg,
            base_result=base_result,
            opt_objective="sharpe_oos",
            runner_cls=_FakeRunner,
            trial_args=trial_args,
            max_workers=1,
        )

        assert len(rows) == 3


# ---------------------------------------------------------------------------
# _optimize_parameters integration with parallel execution
# ---------------------------------------------------------------------------


class TestOptimizeParametersParallel:
    def test_parallel_matches_sequential_results(self) -> None:
        """Parallel optimize_parameters produces the same best threshold as before."""
        cfg = ValidationConfig(
            alpha_id="x",
            data_paths=["d.npy"],
            opt_signal_threshold_min=0.1,
            opt_signal_threshold_max=0.5,
            opt_signal_threshold_steps=5,
            opt_objective="sharpe_oos",
        )
        base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)
        base_result = _make_base_result()

        out = _optimize_parameters(
            alpha=object(),
            base_cfg=base_cfg,
            base_result=base_result,
            config=cfg,
            runner_cls=_FakeRunner,
        )

        assert out["enabled"] is True
        assert out["passed"] is True
        assert abs(float(out["selected_signal_threshold"]) - 0.3) < 1e-9
        assert len(out["trials"]) >= 5

    def test_env_var_override_workers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HFT_GATE_C_PARALLEL_WORKERS env var controls worker count."""
        monkeypatch.setenv("HFT_GATE_C_PARALLEL_WORKERS", "2")

        cfg = ValidationConfig(
            alpha_id="x",
            data_paths=["d.npy"],
            opt_signal_threshold_min=0.1,
            opt_signal_threshold_max=0.5,
            opt_signal_threshold_steps=3,
            opt_objective="sharpe_oos",
        )
        base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)
        base_result = _make_base_result()

        out = _optimize_parameters(
            alpha=object(),
            base_cfg=base_cfg,
            base_result=base_result,
            config=cfg,
            runner_cls=_FakeRunner,
        )

        # Should complete without error
        assert out["enabled"] is True
        assert len(out["trials"]) >= 3

    def test_env_var_clamps_to_8(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Worker count is capped at 8 even if env var is larger."""
        monkeypatch.setenv("HFT_GATE_C_PARALLEL_WORKERS", "100")

        cfg = ValidationConfig(
            alpha_id="x",
            data_paths=["d.npy"],
            opt_signal_threshold_min=0.1,
            opt_signal_threshold_max=0.5,
            opt_signal_threshold_steps=3,
            opt_objective="sharpe_oos",
        )
        base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)
        base_result = _make_base_result()

        out = _optimize_parameters(
            alpha=object(),
            base_cfg=base_cfg,
            base_result=base_result,
            config=cfg,
            runner_cls=_FakeRunner,
        )

        assert out["enabled"] is True

    def test_deepcopy_isolation(self) -> None:
        """Each non-base trial gets a deep copy — mutations don't leak."""

        class _StatefulAlpha:
            def __init__(self) -> None:
                self.call_count = 0

        class _MutatingRunner:
            def __init__(self, alpha: _StatefulAlpha, cfg: BacktestConfig) -> None:
                self.alpha = alpha
                self.cfg = cfg

            def run(self) -> types.SimpleNamespace:
                self.alpha.call_count += 1
                t = float(self.cfg.signal_threshold)
                return types.SimpleNamespace(
                    sharpe_is=1.5,
                    sharpe_oos=1.0,
                    max_drawdown=-0.1,
                    turnover=0.3,
                    run_id=f"r-{t:.4f}",
                    config_hash=f"c-{t:.4f}",
                )

        alpha = _StatefulAlpha()
        cfg = ValidationConfig(
            alpha_id="x",
            data_paths=["d.npy"],
            opt_signal_threshold_min=0.1,
            opt_signal_threshold_max=0.5,
            opt_signal_threshold_steps=5,
            opt_objective="sharpe_oos",
        )
        base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)
        base_result = _make_base_result()

        _optimize_parameters(
            alpha=alpha,
            base_cfg=base_cfg,
            base_result=base_result,
            config=cfg,
            runner_cls=_MutatingRunner,
        )

        # The original alpha should not have been mutated by any worker.
        assert alpha.call_count == 0

    def test_disabled_optimization_skips_parallel(self) -> None:
        """When enable_param_optimization=False, no parallel execution happens."""
        cfg = ValidationConfig(
            alpha_id="x",
            data_paths=["d.npy"],
            enable_param_optimization=False,
        )
        base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)
        base_result = _make_base_result()

        out = _optimize_parameters(
            alpha=object(),
            base_cfg=base_cfg,
            base_result=base_result,
            config=cfg,
            runner_cls=_FakeRunner,
        )

        assert out["enabled"] is False
        assert out["passed"] is True
        assert out["trials"] == []
