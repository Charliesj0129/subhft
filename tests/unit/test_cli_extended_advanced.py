import json
import sys
import types
from argparse import Namespace

import numpy as np
import pytest

import hft_platform.cli as cli


def test_cmd_alpha_list(monkeypatch, capsys):
    class _Manifest:
        status = types.SimpleNamespace(value="DRAFT")
        tier = types.SimpleNamespace(value="TIER_2")

    class _Alpha:
        manifest = _Manifest()

    class _Registry:
        def __init__(self):
            self.errors = ()

        def discover(self, _path):
            return {"ofi_mc": _Alpha()}

    monkeypatch.setitem(sys.modules, "research.registry.alpha_registry", types.SimpleNamespace(AlphaRegistry=_Registry))
    cli.cmd_alpha_list(Namespace())
    out = capsys.readouterr().out
    assert "ofi_mc" in out
    assert "status=DRAFT" in out


def test_cmd_alpha_scaffold(monkeypatch):
    calls = {}

    class _Proc:
        returncode = 0
        stdout = "Scaffolded alpha artifact: research/alphas/ofi_mc_v2\n"
        stderr = ""

    def _run(cmd, cwd, capture_output, text, check):
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        calls["capture_output"] = capture_output
        calls["text"] = text
        calls["check"] = check
        return _Proc()

    import hft_platform.cli._alpha as _alpha_mod

    monkeypatch.setattr(_alpha_mod.subprocess, "run", _run)
    args = Namespace(alpha_id="ofi_mc_v2", paper=["018"], complexity="O1", force=False)
    cli.cmd_alpha_scaffold(args)
    assert calls["cmd"][0] == sys.executable
    assert calls["cmd"][1:3] == ["-m", "research.tools.alpha_scaffold"]
    assert "ofi_mc_v2" in calls["cmd"]


def test_cmd_alpha_search_random(monkeypatch, capsys, tmp_path):
    path = tmp_path / "feed.npy"
    arr = np.zeros(6, dtype=[("ofi", "f8"), ("ret", "f8")])
    arr["ofi"] = np.array([0.1, 0.2, -0.1, 0.05, 0.3, -0.2], dtype=np.float64)
    arr["ret"] = np.array([0.01, -0.01, 0.02, 0.0, 0.015, -0.005], dtype=np.float64)
    np.save(path, arr)

    class _Result:
        def to_dict(self):
            return {
                "expression": "zscore(ts_delta(ofi, 5), 5)",
                "score": 1.2,
                "sharpe_oos": 1.3,
                "correlation_pool_max": 0.2,
                "passed": True,
                "metadata": {"depth": 2},
            }

    class _Engine:
        def __init__(self, *, features, returns, random_seed):
            assert "ofi" in features
            assert returns is not None
            assert random_seed == 7

        def random_search(self, n_trials):
            assert n_trials == 5
            return [_Result()]

    monkeypatch.setitem(
        sys.modules, "research.combinatorial.search_engine", types.SimpleNamespace(AlphaSearchEngine=_Engine)
    )
    args = Namespace(
        mode="random",
        data=str(path),
        feature_fields="ofi",
        returns_field="ret",
        trials=5,
        template=None,
        grid=None,
        population=40,
        generations=10,
        seed=7,
        top=1,
        save_results=None,
        out=None,
    )
    cli.cmd_alpha_search(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "random"
    assert payload["count"] == 1
    assert payload["results"][0]["passed"] is True


def test_cmd_alpha_search_template_requires_template(tmp_path):
    path = tmp_path / "feed.npy"
    arr = np.zeros(6, dtype=[("ofi", "f8"), ("ret", "f8")])
    np.save(path, arr)
    args = Namespace(
        mode="template",
        data=str(path),
        feature_fields="ofi",
        returns_field=None,
        trials=5,
        template=None,
        grid=None,
        population=40,
        generations=10,
        seed=7,
        top=1,
        save_results=None,
        out=None,
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_alpha_search(args)
    assert exc.value.code == 2


def test_cmd_alpha_validate(monkeypatch, capsys, tmp_path):
    calls = {}

    class _ValidationConfig:
        def __init__(self, **kwargs):
            calls["config"] = kwargs

    class _Result:
        passed = True

        @staticmethod
        def to_dict():
            return {"alpha_id": "ofi_mc", "passed": True, "gate_a": {"passed": True}}

    def _run_alpha_validation(cfg):
        calls["validate_called"] = isinstance(cfg, _ValidationConfig)
        return _Result()

    monkeypatch.setitem(
        sys.modules,
        "hft_platform.alpha.validation",
        types.SimpleNamespace(ValidationConfig=_ValidationConfig, run_alpha_validation=_run_alpha_validation),
    )
    # L6: bypass strict-profile guard with a fake strict profile.
    from hft_platform.cli import _alpha as _alpha_mod

    monkeypatch.setattr(
        _alpha_mod,
        "_load_strict_validation_profile",
        lambda _arg: types.SimpleNamespace(is_strict=True),
    )

    out_file = tmp_path / "summary.json"
    args = Namespace(
        alpha_id="ofi_mc",
        data=["feed.npy"],
        is_oos_split=0.7,
        signal_threshold=0.3,
        max_position=5,
        min_sharpe_oos=0.0,
        max_abs_drawdown=0.3,
        skip_gate_b_tests=False,
        pytest_timeout=300,
        experiments_dir="research/experiments",
        out=str(out_file),
        profile="vm_ul6_strict",
    )
    cli.cmd_alpha_validate(args)

    assert calls["validate_called"]
    assert calls["config"]["alpha_id"] == "ofi_mc"
    assert calls["config"]["data_paths"] == ["feed.npy"]
    assert calls["config"]["experiments_dir"] == "research/experiments"
    payload = json.loads(out_file.read_text())
    assert payload["alpha_id"] == "ofi_mc"
    assert "gate_a" in capsys.readouterr().out


def test_cmd_alpha_promote(monkeypatch, capsys, tmp_path):
    calls = {}

    class _PromotionConfig:
        def __init__(self, **kwargs):
            calls["config"] = kwargs

    class _Result:
        approved = True
        checklist = None

        @staticmethod
        def to_dict():
            return {
                "alpha_id": "ofi_mc",
                "approved": True,
                "promotion_config_path": "config/strategy_promotions/x/ofi_mc.yaml",
            }

    def _promote_alpha(cfg):
        calls["promote_called"] = isinstance(cfg, _PromotionConfig)
        return _Result()

    monkeypatch.setitem(
        sys.modules,
        "hft_platform.alpha.promotion",
        types.SimpleNamespace(PromotionConfig=_PromotionConfig, promote_alpha=_promote_alpha),
    )

    out_file = tmp_path / "promote.json"
    args = Namespace(
        alpha_id="ofi_mc",
        owner="charlie",
        scorecard=None,
        shadow_sessions=8,
        min_shadow_sessions=5,
        drift_alerts=0,
        execution_reject_rate=0.0,
        max_execution_reject_rate=0.01,
        min_sharpe_oos=1.0,
        max_abs_drawdown=0.2,
        max_turnover=2.0,
        max_correlation=0.7,
        canary_weight=None,
        expiry_days=30,
        max_live_slippage_bps=3.0,
        max_live_drawdown_contribution=0.02,
        max_execution_error_rate=0.01,
        force=False,
        config_version="v1",
        parent_config_version=None,
        out=str(out_file),
    )
    cli.cmd_alpha_promote(args)
    assert calls["promote_called"]
    assert calls["config"]["owner"] == "charlie"
    payload = json.loads(out_file.read_text())
    assert payload["approved"] is True
    assert "promotion_config_path" in capsys.readouterr().out


def test_cmd_alpha_rl_promote(monkeypatch, capsys, tmp_path):
    calls = {}

    class _Result:
        approved = True

        @staticmethod
        def to_dict():
            return {
                "alpha_id": "rl_ofi",
                "approved": True,
                "promotion_config_path": "config/strategy_promotions/x/rl_ofi.yaml",
            }

    def _promote_latest_rl_run(**kwargs):
        calls["kwargs"] = kwargs
        return _Result()

    monkeypatch.setitem(
        sys.modules,
        "research.rl.lifecycle",
        types.SimpleNamespace(promote_latest_rl_run=_promote_latest_rl_run),
    )

    out_file = tmp_path / "rl_promote.json"
    args = Namespace(
        alpha_id="rl_ofi",
        owner="charlie",
        base_dir="research/experiments",
        project_root=".",
        shadow_sessions=4,
        min_shadow_sessions=1,
        drift_alerts=0,
        execution_reject_rate=0.0,
        force=False,
        out=str(out_file),
    )
    cli.cmd_alpha_rl_promote(args)
    assert calls["kwargs"]["alpha_id"] == "rl_ofi"
    assert calls["kwargs"]["owner"] == "charlie"
    payload = json.loads(out_file.read_text())
    assert payload["approved"] is True
    assert "rl_ofi" in capsys.readouterr().out


def test_cmd_alpha_pool(monkeypatch, capsys, tmp_path):
    def _compute_pool_matrix(base_dir):
        assert base_dir == "research/experiments"
        return {"alpha_ids": ["a", "b"], "matrix": [[1.0, 0.8], [0.8, 1.0]]}

    def _flag_redundant_pairs(matrix_payload, threshold=0.7):
        assert threshold == 0.7
        assert "matrix" in matrix_payload
        return [{"alpha_a": "a", "alpha_b": "b", "correlation": 0.8}]

    monkeypatch.setitem(
        sys.modules,
        "hft_platform.alpha.pool",
        types.SimpleNamespace(compute_pool_matrix=_compute_pool_matrix, flag_redundant_pairs=_flag_redundant_pairs),
    )

    out_file = tmp_path / "pool.json"
    args = Namespace(
        base_dir="research/experiments",
        pool_cmd="matrix",
        redundant=True,
        threshold=0.7,
        out=str(out_file),
    )
    cli.cmd_alpha_pool(args)
    payload = json.loads(out_file.read_text())
    assert payload["matrix"]["alpha_ids"] == ["a", "b"]
    assert payload["redundant"][0]["alpha_a"] == "a"
    assert "redundant" in capsys.readouterr().out


def test_cmd_alpha_pool_matrix_only(monkeypatch, capsys):
    def _compute_pool_matrix(base_dir):
        assert base_dir == "research/experiments"
        return {"alpha_ids": ["a", "b"], "matrix": [[1.0, 0.4], [0.4, 1.0]]}

    def _flag_redundant_pairs(*_args, **_kwargs):
        raise AssertionError("redundant pairs should not be computed for matrix-only command")

    monkeypatch.setitem(
        sys.modules,
        "hft_platform.alpha.pool",
        types.SimpleNamespace(compute_pool_matrix=_compute_pool_matrix, flag_redundant_pairs=_flag_redundant_pairs),
    )
    args = Namespace(base_dir="research/experiments", pool_cmd="matrix", redundant=False, threshold=None, out=None)
    cli.cmd_alpha_pool(args)
    out = json.loads(capsys.readouterr().out)
    assert "redundant" not in out
    assert out["matrix"]["alpha_ids"] == ["a", "b"]


def test_cmd_alpha_pool_optimize(monkeypatch, capsys):
    class _Result:
        @staticmethod
        def to_dict():
            return {
                "method": "ridge",
                "alpha_ids": ["a", "b"],
                "weights": {"a": 0.6, "b": 0.4},
                "returns_used": True,
                "diagnostics": {"strategy": "ridge"},
            }

    class _Mod:
        @staticmethod
        def optimize_pool_weights(base_dir, method, ridge_alpha):
            assert base_dir == "research/experiments"
            assert method == "ridge"
            assert ridge_alpha == 0.2
            return _Result()

    monkeypatch.setitem(sys.modules, "hft_platform.alpha.pool", _Mod)
    args = Namespace(
        base_dir="research/experiments",
        pool_cmd="optimize",
        method="ridge",
        ridge_alpha=0.2,
        threshold=None,
        redundant=False,
        out=None,
    )
    cli.cmd_alpha_pool(args)
    out = json.loads(capsys.readouterr().out)
    assert out["optimization"]["weights"]["a"] == 0.6


def test_cmd_alpha_pool_marginal(monkeypatch, capsys):
    class _Mod:
        @staticmethod
        def evaluate_marginal_alpha(alpha_id, base_dir, method, min_uplift, ridge_alpha):
            assert alpha_id == "ofi_mc"
            assert base_dir == "research/experiments"
            assert method == "mean_variance"
            assert min_uplift == 0.1
            assert ridge_alpha == 0.3
            return {"alpha_id": alpha_id, "passed": True, "uplift": 0.2}

    monkeypatch.setitem(sys.modules, "hft_platform.alpha.pool", _Mod)
    args = Namespace(
        base_dir="research/experiments",
        pool_cmd="marginal",
        alpha_id="ofi_mc",
        method="mean_variance",
        min_uplift=0.1,
        ridge_alpha=0.3,
        threshold=None,
        redundant=False,
        out=None,
    )
    cli.cmd_alpha_pool(args)
    out = json.loads(capsys.readouterr().out)
    assert out["marginal"]["passed"] is True


def test_cmd_alpha_pool_marginal_requires_alpha_id():
    args = Namespace(
        base_dir="research/experiments",
        pool_cmd="marginal",
        alpha_id=None,
        method="equal_weight",
        min_uplift=0.05,
        ridge_alpha=0.1,
        threshold=None,
        redundant=False,
        out=None,
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_alpha_pool(args)
    assert exc.value.code == 2


def test_cmd_alpha_experiments_compare(monkeypatch, capsys, tmp_path):
    class _Tracker:
        def __init__(self, base_dir):
            assert base_dir == "research/experiments"

        def compare(self, run_ids):
            assert run_ids == ["r1", "r2"]
            return [{"run_id": "r1"}, {"run_id": "r2"}]

    monkeypatch.setitem(
        sys.modules, "hft_platform.alpha.experiments", types.SimpleNamespace(ExperimentTracker=_Tracker)
    )

    out_file = tmp_path / "compare.json"
    args = Namespace(base_dir="research/experiments", run_ids=["r1", "r2"], out=str(out_file))
    cli.cmd_alpha_experiments_compare(args)
    payload = json.loads(out_file.read_text())
    assert payload["count"] == 2
    assert payload["runs"][0]["run_id"] == "r1"
    assert "count" in capsys.readouterr().out


def test_cmd_alpha_experiments_list(monkeypatch, capsys, tmp_path):
    class _Run:
        def to_dict(self):
            return {"run_id": "r1", "alpha_id": "ofi_mc"}

    class _Tracker:
        def __init__(self, base_dir):
            assert base_dir == "research/experiments"

        def list_runs(self, alpha_id=None):
            assert alpha_id == "ofi_mc"
            return [_Run()]

    monkeypatch.setitem(
        sys.modules, "hft_platform.alpha.experiments", types.SimpleNamespace(ExperimentTracker=_Tracker)
    )
    out_file = tmp_path / "list.json"
    args = Namespace(base_dir="research/experiments", alpha_id="ofi_mc", out=str(out_file))
    cli.cmd_alpha_experiments_list(args)
    payload = json.loads(out_file.read_text())
    assert payload["count"] == 1
    assert payload["runs"][0]["run_id"] == "r1"
    assert "count" in capsys.readouterr().out


def test_cmd_alpha_experiments_best(monkeypatch, capsys, tmp_path):
    class _Tracker:
        def __init__(self, base_dir):
            assert base_dir == "research/experiments"

        def best_by_metric(self, metric, n, alpha_id):
            assert metric == "sharpe_oos"
            assert n == 3
            assert alpha_id == "ofi_mc"
            return [{"run_id": "r2", "value": 1.5}]

    monkeypatch.setitem(
        sys.modules, "hft_platform.alpha.experiments", types.SimpleNamespace(ExperimentTracker=_Tracker)
    )

    out_file = tmp_path / "best.json"
    args = Namespace(
        base_dir="research/experiments",
        metric="sharpe_oos",
        top=3,
        alpha_id="ofi_mc",
        out=str(out_file),
    )
    cli.cmd_alpha_experiments_best(args)
    payload = json.loads(out_file.read_text())
    assert payload["count"] == 1
    assert payload["runs"][0]["run_id"] == "r2"
    assert "metric" in capsys.readouterr().out
