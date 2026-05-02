"""Unit tests for src/hft_platform/cli/_alpha.py — coverage-focused."""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ns(**kwargs) -> argparse.Namespace:
    """Build a Namespace with defaults that won't accidentally trigger optional paths."""
    defaults: dict = {
        "out": None,
        "force": False,
        "paper": [],
        "complexity": 1,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# _parse_param_grid
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import _parse_param_grid  # noqa: E402


class TestParseParamGrid:
    def test_empty_string_returns_empty_dict(self):
        assert _parse_param_grid("") == {}

    def test_none_returns_empty_dict(self):
        assert _parse_param_grid(None) == {}

    def test_single_int_value(self):
        result = _parse_param_grid("n=10")
        assert result == {"n": [10]}

    def test_multiple_int_values(self):
        result = _parse_param_grid("n=1,2,3")
        assert result == {"n": [1, 2, 3]}

    def test_float_values(self):
        result = _parse_param_grid("alpha=0.1,0.5")
        assert result["alpha"] == pytest.approx([0.1, 0.5])

    def test_string_values(self):
        result = _parse_param_grid("method=pearson,spearman")
        assert result == {"method": ["pearson", "spearman"]}

    def test_multiple_keys(self):
        result = _parse_param_grid("n=5;alpha=0.1,0.2")
        assert result["n"] == [5]
        assert result["alpha"] == pytest.approx([0.1, 0.2])

    def test_int_takes_priority_over_float(self):
        result = _parse_param_grid("x=3")
        assert isinstance(result["x"][0], int)

    def test_invalid_token_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid grid token"):
            _parse_param_grid("no_equals_sign")


# ---------------------------------------------------------------------------
# cmd_alpha_scaffold
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_scaffold  # noqa: E402


class TestCmdAlphaScaffold:
    def test_success_prints_stdout(self, capsys):
        mock_proc = MagicMock(returncode=0, stdout="Scaffolded!\n", stderr="")
        with patch("subprocess.run", return_value=mock_proc):
            cmd_alpha_scaffold(_ns(alpha_id="test_alpha", complexity=1, paper=[], force=False))
        out = capsys.readouterr().out
        assert "Scaffolded!" in out

    def test_failure_exits_nonzero(self):
        mock_proc = MagicMock(returncode=1, stdout="", stderr="Some error")
        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_scaffold(_ns(alpha_id="bad_alpha", complexity=1, paper=[], force=False))
        assert exc_info.value.code == 1

    def test_failure_prints_stderr(self, capsys):
        mock_proc = MagicMock(returncode=2, stdout="", stderr="Error detail")
        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(SystemExit):
                cmd_alpha_scaffold(_ns(alpha_id="bad_alpha", complexity=1, paper=[], force=False))
        out = capsys.readouterr().out
        assert "Error detail" in out

    def test_force_flag_appended(self):
        mock_proc = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            cmd_alpha_scaffold(_ns(alpha_id="a", complexity=1, paper=[], force=True))
        cmd = mock_run.call_args[0][0]
        assert "--force" in cmd

    def test_paper_refs_appended(self):
        mock_proc = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            cmd_alpha_scaffold(_ns(alpha_id="a", complexity=2, paper=["ref1", "ref2"], force=False))
        cmd = mock_run.call_args[0][0]
        assert "--paper" in cmd
        assert "ref1" in cmd
        assert "ref2" in cmd


# ---------------------------------------------------------------------------
# cmd_alpha_list
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_list  # noqa: E402


class TestCmdAlphaList:
    def _make_registry(self):
        registry = MagicMock()
        registry.errors = []
        m1 = MagicMock()
        m1.manifest.status.value = "validated"
        m1.manifest.tier.value = "a"
        registry.discover.return_value = {"alpha_one": m1}
        return registry

    def test_lists_alphas(self, capsys):
        registry = self._make_registry()
        with patch("hft_platform.cli._alpha.import_module") as mock_import:
            mock_mod = MagicMock()
            mock_mod.AlphaRegistry.return_value = registry
            mock_import.return_value = mock_mod
            # Directly patch the inner import via builtins would be complex;
            # instead patch using sys.modules insertion
            fake_mod = MagicMock()
            fake_mod.AlphaRegistry = MagicMock(return_value=registry)
            with patch.dict("sys.modules", {"research.registry.alpha_registry": fake_mod}):
                cmd_alpha_list(_ns())
        out = capsys.readouterr().out
        assert "alpha_one" in out
        assert "validated" in out

    def test_import_failure_exits(self, capsys):
        with patch.dict("sys.modules", {"research.registry.alpha_registry": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_list(_ns())
        assert exc_info.value.code == 1

    def test_no_alphas_discovered(self, capsys):
        registry = MagicMock()
        registry.errors = []
        registry.discover.return_value = {}
        fake_mod = MagicMock()
        fake_mod.AlphaRegistry = MagicMock(return_value=registry)
        with patch.dict("sys.modules", {"research.registry.alpha_registry": fake_mod}):
            cmd_alpha_list(_ns())
        out = capsys.readouterr().out
        assert "No alpha artifacts" in out

    def test_discovery_warnings_printed(self, capsys):
        registry = MagicMock()
        registry.errors = ["warn1", "warn2"]
        m1 = MagicMock()
        m1.manifest.status.value = "candidate"
        m1.manifest.tier = None
        registry.discover.return_value = {"a": m1}
        fake_mod = MagicMock()
        fake_mod.AlphaRegistry = MagicMock(return_value=registry)
        with patch.dict("sys.modules", {"research.registry.alpha_registry": fake_mod}):
            cmd_alpha_list(_ns())
        out = capsys.readouterr().out
        assert "warn1" in out


# ---------------------------------------------------------------------------
# cmd_alpha_canary_status
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_canary_status  # noqa: E402


class TestCmdAlphaCanaryStatus:
    def test_no_canaries(self, capsys):
        monitor = MagicMock()
        monitor.load_active_canaries.return_value = []
        fake_mod = MagicMock()
        fake_mod.CanaryMonitor = MagicMock(return_value=monitor)
        with patch.dict("sys.modules", {"hft_platform.alpha.canary": fake_mod}):
            cmd_alpha_canary_status(_ns(promotions_dir="p"))
        out = capsys.readouterr().out
        assert "No active canaries" in out

    def test_canaries_listed(self, capsys):
        monitor = MagicMock()
        monitor.load_active_canaries.return_value = [
            {"alpha_id": "a1", "weight": 0.5, "enabled": True, "_path": "/some/path"}
        ]
        fake_mod = MagicMock()
        fake_mod.CanaryMonitor = MagicMock(return_value=monitor)
        with patch.dict("sys.modules", {"hft_platform.alpha.canary": fake_mod}):
            cmd_alpha_canary_status(_ns(promotions_dir="p"))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["count"] == 1
        assert data["canaries"][0]["alpha_id"] == "a1"

    def test_import_failure_exits(self, capsys):
        with patch.dict("sys.modules", {"hft_platform.alpha.canary": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_canary_status(_ns(promotions_dir="p"))
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_alpha_canary_evaluate
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_canary_evaluate  # noqa: E402


class TestCmdAlphaCanaryEvaluate:
    def _make_args(self, **kwargs):
        defaults = dict(
            promotions_dir="p",
            alpha_id="a1",
            slippage_bps=0.5,
            dd_contrib=0.01,
            error_rate=0.02,
            sessions=10,
            sharpe_live=None,
            apply=False,
            out=None,
        )
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_basic_evaluate(self, capsys):
        monitor = MagicMock()
        status = MagicMock()
        status.to_dict.return_value = {"decision": "hold", "alpha_id": "a1"}
        monitor.evaluate.return_value = status
        fake_mod = MagicMock()
        fake_mod.CanaryMonitor = MagicMock(return_value=monitor)
        with patch.dict("sys.modules", {"hft_platform.alpha.canary": fake_mod}):
            cmd_alpha_canary_evaluate(self._make_args())
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["decision"] == "hold"

    def test_apply_calls_apply_decision(self, capsys):
        monitor = MagicMock()
        status = MagicMock()
        status.to_dict.return_value = {"decision": "graduate", "alpha_id": "a1"}
        monitor.evaluate.return_value = status
        fake_mod = MagicMock()
        fake_mod.CanaryMonitor = MagicMock(return_value=monitor)
        with patch.dict("sys.modules", {"hft_platform.alpha.canary": fake_mod}):
            cmd_alpha_canary_evaluate(self._make_args(apply=True))
        monitor.apply_decision.assert_called_once_with(status)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["applied"] is True

    def test_sharpe_live_included(self, capsys):
        monitor = MagicMock()
        status = MagicMock()
        status.to_dict.return_value = {}
        monitor.evaluate.return_value = status
        fake_mod = MagicMock()
        fake_mod.CanaryMonitor = MagicMock(return_value=monitor)
        with patch.dict("sys.modules", {"hft_platform.alpha.canary": fake_mod}):
            cmd_alpha_canary_evaluate(self._make_args(sharpe_live=1.5))
        _, call_kwargs = monitor.evaluate.call_args
        assert "sharpe_live" in monitor.evaluate.call_args[0][1]

    def test_out_writes_file(self, tmp_path, capsys):
        out_file = tmp_path / "result.json"
        monitor = MagicMock()
        status = MagicMock()
        status.to_dict.return_value = {"decision": "rollback"}
        monitor.evaluate.return_value = status
        fake_mod = MagicMock()
        fake_mod.CanaryMonitor = MagicMock(return_value=monitor)
        with patch.dict("sys.modules", {"hft_platform.alpha.canary": fake_mod}):
            cmd_alpha_canary_evaluate(self._make_args(out=str(out_file)))
        assert out_file.exists()
        assert json.loads(out_file.read_text())["decision"] == "rollback"


# ---------------------------------------------------------------------------
# cmd_alpha_ab_compare
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_ab_compare  # noqa: E402


class TestCmdAlphaAbCompare:
    def test_fewer_than_two_runs_exits(self, capsys):
        tracker = MagicMock()
        tracker.compare.return_value = [{"run_id": "r1"}]
        fake_mod = MagicMock()
        fake_mod.ExperimentTracker = MagicMock(return_value=tracker)
        with patch.dict("sys.modules", {"hft_platform.alpha.experiments": fake_mod}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_ab_compare(_ns(run_id_a="r1", run_id_b="r2", base_dir="d"))
        assert exc_info.value.code == 1

    def test_two_runs_prints_comparison(self, capsys):
        tracker = MagicMock()
        tracker.compare.return_value = [
            {"run_id": "r1", "sharpe": 1.0, "pnl": 100.0},
            {"run_id": "r2", "sharpe": 1.5, "pnl": 150.0},
        ]
        fake_mod = MagicMock()
        fake_mod.ExperimentTracker = MagicMock(return_value=tracker)
        with patch.dict("sys.modules", {"hft_platform.alpha.experiments": fake_mod}):
            cmd_alpha_ab_compare(_ns(run_id_a="r1", run_id_b="r2", base_dir="d"))
        out = capsys.readouterr().out
        assert "r1" in out
        assert "r2" in out
        assert "sharpe" in out

    def test_out_writes_payload(self, tmp_path, capsys):
        out_file = tmp_path / "cmp.json"
        tracker = MagicMock()
        tracker.compare.return_value = [
            {"run_id": "r1", "sharpe": 1.0},
            {"run_id": "r2", "sharpe": 2.0},
        ]
        fake_mod = MagicMock()
        fake_mod.ExperimentTracker = MagicMock(return_value=tracker)
        with patch.dict("sys.modules", {"hft_platform.alpha.experiments": fake_mod}):
            cmd_alpha_ab_compare(_ns(run_id_a="r1", run_id_b="r2", base_dir="d", out=str(out_file)))
        assert out_file.exists()
        data = json.loads(out_file.read_text())
        assert "run_a" in data and "run_b" in data

    def test_import_failure_exits(self, capsys):
        with patch.dict("sys.modules", {"hft_platform.alpha.experiments": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_ab_compare(_ns(run_id_a="a", run_id_b="b", base_dir="d"))
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_alpha_experiments_compare / list / best
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import (  # noqa: E402
    cmd_alpha_experiments_best,
    cmd_alpha_experiments_compare,
    cmd_alpha_experiments_list,
)


class TestCmdAlphaExperiments:
    def _fake_tracker_mod(self, compare_rows=None, list_rows=None, best_rows=None):
        tracker = MagicMock()
        if compare_rows is not None:
            tracker.compare.return_value = compare_rows
        if list_rows is not None:
            run_objs = []
            for r in list_rows:
                obj = MagicMock()
                obj.to_dict.return_value = r
                run_objs.append(obj)
            tracker.list_runs.return_value = run_objs
        if best_rows is not None:
            tracker.best_by_metric.return_value = best_rows
        fake_mod = MagicMock()
        fake_mod.ExperimentTracker = MagicMock(return_value=tracker)
        return fake_mod, tracker

    def test_experiments_compare(self, capsys):
        rows = [{"run_id": "r1", "sharpe": 1.0}]
        fake_mod, _ = self._fake_tracker_mod(compare_rows=rows)
        with patch.dict("sys.modules", {"hft_platform.alpha.experiments": fake_mod}):
            cmd_alpha_experiments_compare(_ns(base_dir="d", run_ids=["r1"]))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["count"] == 1

    def test_experiments_list(self, capsys):
        rows = [{"run_id": "r1"}]
        fake_mod, _ = self._fake_tracker_mod(list_rows=rows)
        with patch.dict("sys.modules", {"hft_platform.alpha.experiments": fake_mod}):
            cmd_alpha_experiments_list(_ns(base_dir="d", alpha_id=None))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["count"] == 1

    def test_experiments_best(self, capsys):
        rows = [{"run_id": "r1", "sharpe": 2.0}]
        fake_mod, _ = self._fake_tracker_mod(best_rows=rows)
        with patch.dict("sys.modules", {"hft_platform.alpha.experiments": fake_mod}):
            cmd_alpha_experiments_best(_ns(base_dir="d", metric="sharpe", top=5, alpha_id=None))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["metric"] == "sharpe"
        assert data["count"] == 1

    def test_experiments_compare_import_failure_exits(self):
        with patch.dict("sys.modules", {"hft_platform.alpha.experiments": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_experiments_compare(_ns(base_dir="d", run_ids=[]))
        assert exc_info.value.code == 1

    def test_experiments_list_out_writes_file(self, tmp_path, capsys):
        out_file = tmp_path / "runs.json"
        fake_mod, _ = self._fake_tracker_mod(list_rows=[])
        with patch.dict("sys.modules", {"hft_platform.alpha.experiments": fake_mod}):
            cmd_alpha_experiments_list(_ns(base_dir="d", alpha_id=None, out=str(out_file)))
        assert out_file.exists()

    def test_experiments_best_import_failure_exits(self):
        with patch.dict("sys.modules", {"hft_platform.alpha.experiments": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_experiments_best(_ns(base_dir="d", metric="sharpe", top=5, alpha_id=None))
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_alpha_pool
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_pool  # noqa: E402


class TestCmdAlphaPool:
    def _pool_args(self, pool_cmd="matrix", **kwargs):
        defaults = dict(
            pool_cmd=pool_cmd,
            base_dir="research/experiments",
            threshold=None,
            method="equal_weight",
            ridge_alpha=0.1,
            min_uplift=0.05,
            alpha_id=None,
            redundant=False,
            corr_metric="pearson",
            out=None,
        )
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def _mock_pool_mod(self):
        pool_mod = MagicMock()
        pool_mod.compute_pool_matrix.return_value = {"corr": [[1.0]]}
        pool_mod.flag_redundant_pairs.return_value = []
        opt_result = MagicMock()
        opt_result.to_dict.return_value = {"weights": {"a1": 0.5}}
        pool_mod.optimize_pool_weights.return_value = opt_result
        pool_mod.evaluate_marginal_alpha.return_value = {"marginal_sharpe": 0.1}
        return pool_mod

    def test_matrix_subcommand(self, capsys):
        pool_mod = self._mock_pool_mod()
        with patch.dict("sys.modules", {"hft_platform.alpha.pool": pool_mod}):
            cmd_alpha_pool(self._pool_args("matrix"))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "matrix" in data

    def test_redundant_subcommand(self, capsys):
        pool_mod = self._mock_pool_mod()
        with patch.dict("sys.modules", {"hft_platform.alpha.pool": pool_mod}):
            cmd_alpha_pool(self._pool_args("redundant", threshold=0.7))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "redundant" in data
        assert "threshold" in data

    def test_redundant_legacy_signature_fallback(self, capsys):
        """Test TypeError fallback when flag_redundant_pairs doesn't accept metric arg."""
        pool_mod = self._mock_pool_mod()
        pool_mod.flag_redundant_pairs.side_effect = [TypeError("unexpected kwarg"), []]
        with patch.dict("sys.modules", {"hft_platform.alpha.pool": pool_mod}):
            cmd_alpha_pool(self._pool_args("redundant", threshold=0.7))
        # Should have called twice: first with metric (TypeError), then without
        assert pool_mod.flag_redundant_pairs.call_count == 2

    def test_optimize_subcommand(self, capsys):
        pool_mod = self._mock_pool_mod()
        with patch.dict("sys.modules", {"hft_platform.alpha.pool": pool_mod}):
            cmd_alpha_pool(self._pool_args("optimize"))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "optimization" in data

    def test_marginal_requires_alpha_id(self, capsys):
        pool_mod = self._mock_pool_mod()
        with patch.dict("sys.modules", {"hft_platform.alpha.pool": pool_mod}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_pool(self._pool_args("marginal", alpha_id=None))
        assert exc_info.value.code == 2

    def test_marginal_with_alpha_id(self, capsys):
        pool_mod = self._mock_pool_mod()
        with patch.dict("sys.modules", {"hft_platform.alpha.pool": pool_mod}):
            cmd_alpha_pool(self._pool_args("marginal", alpha_id="a1"))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "marginal" in data

    def test_import_failure_exits(self, capsys):
        with patch.dict("sys.modules", {"hft_platform.alpha.pool": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_pool(self._pool_args())
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_alpha_batch_correlation
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_batch_correlation  # noqa: E402


class TestCmdAlphaBatchCorrelation:
    def test_basic(self, capsys):
        fake_mod = MagicMock()
        fake_mod.batch_compute_correlations.return_value = [{"pair": ("a1", "a2"), "corr": 0.9}]
        with patch.dict("sys.modules", {"hft_platform.alpha.batch_correlation": fake_mod}):
            cmd_alpha_batch_correlation(_ns(experiments_dir="e", dry_run=False))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["count"] == 1

    def test_import_failure_exits(self):
        with patch.dict("sys.modules", {"hft_platform.alpha.batch_correlation": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_batch_correlation(_ns(experiments_dir="e", dry_run=False))
        assert exc_info.value.code == 1

    def test_out_writes_file(self, tmp_path, capsys):
        out_file = tmp_path / "corr.json"
        fake_mod = MagicMock()
        fake_mod.batch_compute_correlations.return_value = []
        with patch.dict("sys.modules", {"hft_platform.alpha.batch_correlation": fake_mod}):
            cmd_alpha_batch_correlation(_ns(experiments_dir="e", dry_run=True, out=str(out_file)))
        assert out_file.exists()


# ---------------------------------------------------------------------------
# cmd_alpha_paper_trade_batch
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_paper_trade_batch  # noqa: E402


class TestCmdAlphaPaperTradeBatch:
    def test_discover_action(self, capsys):
        fake_mod = MagicMock()
        fake_mod.discover_gate_d_candidates.return_value = ["a1", "a2"]
        fake_mod.batch_record_sessions.return_value = []
        with patch.dict("sys.modules", {"hft_platform.alpha.paper_trade_batch": fake_mod}):
            cmd_alpha_paper_trade_batch(
                _ns(paper_trade_action="discover", experiments_dir="e", top_n=5, min_sharpe_oos=1.0)
            )
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["count"] == 2

    def test_record_action_no_alpha_ids_exits(self, capsys):
        fake_mod = MagicMock()
        fake_mod.discover_gate_d_candidates.return_value = []
        fake_mod.batch_record_sessions.return_value = []
        with patch.dict("sys.modules", {"hft_platform.alpha.paper_trade_batch": fake_mod}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_paper_trade_batch(_ns(paper_trade_action="record", experiments_dir="e", alpha_ids=[]))
        assert exc_info.value.code == 2

    def test_record_action_with_ids(self, capsys):
        fake_mod = MagicMock()
        fake_mod.batch_record_sessions.return_value = [{"alpha_id": "a1", "sessions": 3}]
        with patch.dict("sys.modules", {"hft_platform.alpha.paper_trade_batch": fake_mod}):
            cmd_alpha_paper_trade_batch(
                _ns(
                    paper_trade_action="record",
                    experiments_dir="e",
                    alpha_ids=["a1"],
                    sessions_per_alpha=3,
                    base_date=None,
                    seed=42,
                )
            )
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["count"] == 1

    def test_unknown_action_exits(self, capsys):
        fake_mod = MagicMock()
        with patch.dict("sys.modules", {"hft_platform.alpha.paper_trade_batch": fake_mod}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_paper_trade_batch(_ns(paper_trade_action="unknown", experiments_dir="e"))
        assert exc_info.value.code == 2

    def test_import_failure_exits(self):
        with patch.dict("sys.modules", {"hft_platform.alpha.paper_trade_batch": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_paper_trade_batch(_ns(paper_trade_action="discover", experiments_dir="e"))
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_alpha_promote_batch
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_promote_batch  # noqa: E402


class TestCmdAlphaPromoteBatch:
    def _make_args(self, **kwargs):
        defaults = dict(
            experiments_dir="e",
            owner="batch",
            min_sharpe_oos=1.0,
            max_abs_drawdown=0.2,
            max_correlation=0.7,
            alpha_ids=None,
            dry_run=True,
            top_n=10,
            out=None,
        )
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_all_approved(self, capsys):
        promoter = MagicMock()
        promoter.run_fleet.return_value = [{"alpha_id": "a1", "approved": True}]
        fake_mod = MagicMock()
        fake_mod.BatchPromoter = MagicMock(return_value=promoter)
        with patch.dict("sys.modules", {"hft_platform.alpha.batch_promote": fake_mod}):
            cmd_alpha_promote_batch(self._make_args())
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["approved"] == 1

    def test_none_approved_exits_2(self, capsys):
        promoter = MagicMock()
        promoter.run_fleet.return_value = [{"alpha_id": "a1", "approved": False}]
        fake_mod = MagicMock()
        fake_mod.BatchPromoter = MagicMock(return_value=promoter)
        with patch.dict("sys.modules", {"hft_platform.alpha.batch_promote": fake_mod}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_promote_batch(self._make_args())
        assert exc_info.value.code == 2

    def test_empty_results_does_not_exit(self, capsys):
        promoter = MagicMock()
        promoter.run_fleet.return_value = []
        fake_mod = MagicMock()
        fake_mod.BatchPromoter = MagicMock(return_value=promoter)
        with patch.dict("sys.modules", {"hft_platform.alpha.batch_promote": fake_mod}):
            cmd_alpha_promote_batch(self._make_args())  # no SystemExit
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["total"] == 0

    def test_import_failure_exits(self):
        with patch.dict("sys.modules", {"hft_platform.alpha.batch_promote": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_promote_batch(self._make_args())
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_alpha_validate  — import failure path
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_validate  # noqa: E402


class TestCmdAlphaValidate:
    def test_import_failure_exits(self):
        with patch.dict("sys.modules", {"hft_platform.alpha.validation": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_validate(
                    _ns(
                        alpha_id="a1",
                        data=["d.npz"],
                        is_oos_split=0.2,
                        signal_threshold=0.1,
                        max_position=100,
                        min_sharpe_oos=1.0,
                        max_abs_drawdown=0.2,
                        skip_gate_b_tests=False,
                        pytest_timeout=60,
                        experiments_dir="e",
                    )
                )
        assert exc_info.value.code == 1

    def test_validation_pass_outputs_json(self, capsys):
        fake_config_cls = MagicMock()
        fake_result = MagicMock()
        fake_result.passed = True
        fake_result.to_dict.return_value = {"passed": True, "alpha_id": "a1"}
        fake_mod = MagicMock()
        fake_mod.ValidationConfig = fake_config_cls
        fake_mod.run_alpha_validation.return_value = fake_result
        with patch.dict("sys.modules", {"hft_platform.alpha.validation": fake_mod}):
            cmd_alpha_validate(
                _ns(
                    alpha_id="a1",
                    data=["d.npz"],
                    is_oos_split=0.2,
                    signal_threshold=0.1,
                    max_position=100,
                    min_sharpe_oos=1.0,
                    max_abs_drawdown=0.2,
                    skip_gate_b_tests=False,
                    pytest_timeout=60,
                    experiments_dir="e",
                )
            )
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["passed"] is True

    def test_validation_fail_exits_2(self, capsys):
        fake_result = MagicMock()
        fake_result.passed = False
        fake_result.to_dict.return_value = {"passed": False}
        fake_mod = MagicMock()
        fake_mod.ValidationConfig = MagicMock()
        fake_mod.run_alpha_validation.return_value = fake_result
        with patch.dict("sys.modules", {"hft_platform.alpha.validation": fake_mod}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_validate(
                    _ns(
                        alpha_id="a1",
                        data=["d.npz"],
                        is_oos_split=0.2,
                        signal_threshold=0.1,
                        max_position=100,
                        min_sharpe_oos=1.0,
                        max_abs_drawdown=0.2,
                        skip_gate_b_tests=False,
                        pytest_timeout=60,
                        experiments_dir="e",
                    )
                )
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# cmd_alpha_promote — import failure path
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_promote  # noqa: E402


class TestCmdAlphaPromote:
    def _make_args(self):
        return _ns(
            alpha_id="a1",
            owner="dev",
            experiments_dir="e",
            scorecard="sc.json",
            shadow_sessions=5,
            min_shadow_sessions=3,
            drift_alerts=0,
            execution_reject_rate=0.01,
            max_execution_reject_rate=0.05,
            min_sharpe_oos=1.0,
            max_abs_drawdown=0.2,
            max_turnover=5.0,
            max_correlation=0.7,
            canary_weight=None,
            expiry_days=30,
            max_live_slippage_bps=5.0,
            max_live_drawdown_contribution=0.1,
            max_execution_error_rate=0.01,
        )

    def test_import_failure_exits(self):
        with patch.dict("sys.modules", {"hft_platform.alpha.promotion": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_promote(self._make_args())
        assert exc_info.value.code == 1

    def test_approved_result(self, capsys):
        fake_result = MagicMock()
        fake_result.approved = True
        fake_result.checklist = None
        fake_result.to_dict.return_value = {"approved": True}
        fake_mod = MagicMock()
        fake_mod.PromotionConfig = MagicMock()
        fake_mod.promote_alpha.return_value = fake_result
        with patch.dict("sys.modules", {"hft_platform.alpha.promotion": fake_mod}):
            cmd_alpha_promote(self._make_args())
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["approved"] is True

    def test_rejected_result_exits_2(self, capsys):
        fake_result = MagicMock()
        fake_result.approved = False
        fake_result.checklist = None
        fake_result.to_dict.return_value = {"approved": False}
        fake_mod = MagicMock()
        fake_mod.PromotionConfig = MagicMock()
        fake_mod.promote_alpha.return_value = fake_result
        with patch.dict("sys.modules", {"hft_platform.alpha.promotion": fake_mod}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_promote(self._make_args())
        assert exc_info.value.code == 2

    def test_checklist_items_printed(self, capsys):
        fake_result = MagicMock()
        fake_result.approved = True
        checklist_item = MagicMock()
        checklist_item.passed = True
        checklist_item.label = "Gate D"
        checklist_item.detail = "Sharpe OK"
        fake_result.checklist.items = [checklist_item]
        fake_result.to_dict.return_value = {"approved": True}
        fake_mod = MagicMock()
        fake_mod.PromotionConfig = MagicMock()
        fake_mod.promote_alpha.return_value = fake_result
        with patch.dict("sys.modules", {"hft_platform.alpha.promotion": fake_mod}):
            cmd_alpha_promote(self._make_args())
        out = capsys.readouterr().out
        assert "Gate D" in out
        assert "PASS" in out


# ---------------------------------------------------------------------------
# cmd_alpha_rl_promote — import failure / success
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_rl_promote  # noqa: E402


class TestCmdAlphaRlPromote:
    def _make_args(self):
        return _ns(
            alpha_id="rl_alpha",
            owner="dev",
            base_dir="research/rl",
            project_root=".",
            shadow_sessions=5,
            min_shadow_sessions=3,
            drift_alerts=0,
            execution_reject_rate=0.01,
        )

    def test_import_failure_exits(self):
        with patch.dict("sys.modules", {"research.rl.lifecycle": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_rl_promote(self._make_args())
        assert exc_info.value.code == 1

    def test_approved_result(self, capsys):
        fake_result = MagicMock()
        fake_result.approved = True
        fake_result.to_dict.return_value = {"approved": True}
        fake_mod = MagicMock()
        fake_mod.promote_latest_rl_run = MagicMock(return_value=fake_result)
        with patch.dict("sys.modules", {"research.rl.lifecycle": fake_mod}):
            cmd_alpha_rl_promote(self._make_args())
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["approved"] is True

    def test_rejected_exits_2(self, capsys):
        fake_result = MagicMock()
        fake_result.approved = False
        fake_result.to_dict.return_value = {"approved": False}
        fake_mod = MagicMock()
        fake_mod.promote_latest_rl_run = MagicMock(return_value=fake_result)
        with patch.dict("sys.modules", {"research.rl.lifecycle": fake_mod}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_rl_promote(self._make_args())
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# cmd_alpha_canary_auto_evaluate — import failure / success
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_canary_auto_evaluate  # noqa: E402


class TestCmdAlphaCanaryAutoEvaluate:
    def test_import_failure_exits(self):
        with patch.dict(
            "sys.modules", {"hft_platform.alpha.canary": None, "hft_platform.alpha.canary_scheduler": None}
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_canary_auto_evaluate(_ns(promotions_dir="p", dry_run=True))
        assert exc_info.value.code == 1

    def test_success_prints_summary(self, capsys):
        status = MagicMock()
        status.to_dict.return_value = {"alpha_id": "a1", "decision": "hold"}
        monitor = MagicMock()
        scheduler = MagicMock()

        async def _fake_evaluate_all():
            return [status]

        scheduler.evaluate_all = _fake_evaluate_all

        fake_canary_mod = MagicMock()
        fake_canary_mod.CanaryMonitor = MagicMock(return_value=monitor)
        fake_scheduler_mod = MagicMock()
        fake_scheduler_mod.CanaryAutoScheduler = MagicMock(return_value=scheduler)

        with patch.dict(
            "sys.modules",
            {"hft_platform.alpha.canary": fake_canary_mod, "hft_platform.alpha.canary_scheduler": fake_scheduler_mod},
        ):
            cmd_alpha_canary_auto_evaluate(_ns(promotions_dir="p", dry_run=True))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["count"] == 1
        assert data["dry_run"] is True


# ---------------------------------------------------------------------------
# cmd_alpha_validate_batch — key branches
# ---------------------------------------------------------------------------


from hft_platform.cli._alpha import cmd_alpha_validate_batch  # noqa: E402


class TestCmdAlphaValidateBatch:
    def _base_args(self, **kwargs):
        defaults = dict(
            alphas_dir="research/alphas",
            alpha_ids=None,
            gates="ABC",
            data=["d.npz"],
            min_sharpe_oos=0.0,
            max_abs_drawdown=0.3,
            experiments_dir="e",
            fail_fast=False,
            out=None,
        )
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def _mock_modules(self, alpha_ids, pass_result=True):
        registry = MagicMock()
        m = MagicMock()
        registry.discover.return_value = {a: m for a in alpha_ids}
        registry.errors = []
        fake_registry_mod = MagicMock()
        fake_registry_mod.AlphaRegistry = MagicMock(return_value=registry)

        result = MagicMock()
        result.passed = pass_result
        result.gate_a = MagicMock(passed=True)
        result.gate_b = MagicMock(passed=True)
        result.to_dict.return_value = {"passed": pass_result, "alpha_id": "placeholder"}

        fake_validation_mod = MagicMock()
        fake_validation_mod.ValidationConfig = MagicMock()
        fake_validation_mod.run_alpha_validation.return_value = result

        return fake_registry_mod, fake_validation_mod

    def test_import_registry_failure_exits(self):
        with patch.dict("sys.modules", {"hft_platform.alpha.validation": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_validate_batch(self._base_args())
        assert exc_info.value.code == 1

    def test_batch_all_pass(self, capsys):
        reg_mod, val_mod = self._mock_modules(["a1", "a2"], pass_result=True)
        with patch.dict(
            "sys.modules",
            {"hft_platform.alpha.validation": val_mod, "research.registry.alpha_registry": reg_mod},
        ):
            cmd_alpha_validate_batch(self._base_args())
        out = capsys.readouterr().out
        assert "passed" in out

    def test_batch_fail_fast(self, capsys):
        reg_mod, val_mod = self._mock_modules(["a1", "a2"], pass_result=False)
        with patch.dict(
            "sys.modules",
            {"hft_platform.alpha.validation": val_mod, "research.registry.alpha_registry": reg_mod},
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_validate_batch(self._base_args(fail_fast=True))
        assert exc_info.value.code == 2

    def test_batch_alpha_ids_filter(self, capsys):
        reg_mod, val_mod = self._mock_modules(["a1", "a2", "a3"], pass_result=True)
        with patch.dict(
            "sys.modules",
            {"hft_platform.alpha.validation": val_mod, "research.registry.alpha_registry": reg_mod},
        ):
            cmd_alpha_validate_batch(self._base_args(alpha_ids=["a1"]))
        # Only 1 alpha validated
        out = capsys.readouterr().out
        assert "1 alphas" in out

    def test_batch_error_handling(self, capsys):
        """Alpha that raises an exception goes into errored_ids."""
        reg_mod = MagicMock()
        m = MagicMock()
        reg_mod.AlphaRegistry.return_value.discover.return_value = {"a1": m}
        val_mod = MagicMock()
        val_mod.ValidationConfig = MagicMock()
        val_mod.run_alpha_validation.side_effect = RuntimeError("boom")
        with patch.dict(
            "sys.modules",
            {"hft_platform.alpha.validation": val_mod, "research.registry.alpha_registry": reg_mod},
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_alpha_validate_batch(self._base_args())
        assert exc_info.value.code == 2

    def test_batch_out_writes_report(self, tmp_path, capsys):
        out_file = tmp_path / "report.json"
        reg_mod, val_mod = self._mock_modules(["a1"], pass_result=True)
        with patch.dict(
            "sys.modules",
            {"hft_platform.alpha.validation": val_mod, "research.registry.alpha_registry": reg_mod},
        ):
            cmd_alpha_validate_batch(self._base_args(out=str(out_file)))
        assert out_file.exists()
        data = json.loads(out_file.read_text())
        assert "summary" in data
