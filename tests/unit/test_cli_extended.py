import json
import sys
import types
from argparse import Namespace

import numpy as np
import pytest

import hft_platform.cli as cli


def test_resolve_default_mode_env(monkeypatch):
    monkeypatch.setenv("HFT_MODE", "real")
    assert cli._resolve_default_mode() == "live"
    monkeypatch.setenv("HFT_MODE", "replay")
    assert cli._resolve_default_mode() == "replay"
    monkeypatch.setenv("HFT_MODE", "unknown")
    assert cli._resolve_default_mode() == "sim"


def test_cmd_check_export_json(tmp_path, monkeypatch):
    settings = {"symbols": ["2330"], "strategy": {"id": "s1"}}
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: (settings, {}))
    monkeypatch.chdir(tmp_path)

    cli.cmd_check(Namespace(export="json"))

    out_path = tmp_path / "config" / "exported_settings.json"
    assert out_path.exists()
    assert json.loads(out_path.read_text())["symbols"] == ["2330"]


def test_cmd_check_export_yaml(tmp_path, monkeypatch):
    settings = {"symbols": ["2330"], "strategy": {"id": "s1"}}
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: (settings, {}))
    monkeypatch.chdir(tmp_path)

    cli.cmd_check(Namespace(export="yaml"))

    out_path = tmp_path / "config" / "exported_settings.yaml"
    assert out_path.exists()
    assert "symbols" in out_path.read_text()


def test_cmd_check_missing_exits(monkeypatch):
    settings = {"symbols": [], "strategy": {}}
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: (settings, {}))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_check(Namespace(export=None))
    assert exc.value.code == 1


def test_cmd_feed_status_success(monkeypatch, capsys):
    import urllib.request

    class DummyResp:
        def read(self):
            return b"feed_events_total 1"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: DummyResp())
    cli.cmd_feed_status(Namespace(port=9090))
    out = capsys.readouterr().out
    assert "feed metric present=True" in out


def test_cmd_feed_status_failure(monkeypatch, capsys):
    import urllib.request

    def _fail(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(urllib.request, "urlopen", _fail)
    cli.cmd_feed_status(Namespace(port=9090))
    out = capsys.readouterr().out
    assert "Unable to reach metrics" in out


def test_cmd_diag(capsys):
    cli.cmd_diag(Namespace())
    out = capsys.readouterr().out
    assert "Diag:" in out


def test_cmd_strat_test_import_failure(monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(cli, "import_module", lambda *_a, **_k: (_ for _ in ()).throw(ImportError("nope")))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_strat_test(Namespace(module="x", cls="Y", strategy_id="s", symbol="2330"))
    assert exc.value.code == 1


def test_cmd_strat_test_success(monkeypatch, capsys):
    class DummyStrategy:
        def __init__(self, strategy_id: str):
            self.strategy_id = strategy_id

        def handle_event(self, *_a, **_k):
            return []

    dummy_mod = types.SimpleNamespace(DummyStrategy=DummyStrategy)
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: ({"symbols": ["2330"]}, {}))
    monkeypatch.setattr(cli, "import_module", lambda *_a, **_k: dummy_mod)

    cli.cmd_strat_test(Namespace(module="dummy", cls="DummyStrategy", strategy_id="s", symbol="2330"))
    out = capsys.readouterr().out
    assert "Strategy emitted" in out


def test_cmd_backtest_requires_subcommand(capsys):
    with pytest.raises(SystemExit):
        cli.cmd_backtest(Namespace(backtest_cmd=None))
    assert "Please specify backtest subcommand" in capsys.readouterr().out


def test_cmd_backtest_convert(monkeypatch, capsys, tmp_path):
    import hft_platform.backtest.convert as convert_mod

    called = {}

    def _fake_convert(inp, out, scale):
        called["args"] = (inp, out, scale)

    monkeypatch.setattr(convert_mod, "convert_jsonl_to_npz", _fake_convert)
    args = Namespace(backtest_cmd="convert", input="in.jsonl", output=str(tmp_path / "out.npz"), scale=10000)
    cli.cmd_backtest(args)
    assert called["args"][0] == "in.jsonl"
    assert "Converted to" in capsys.readouterr().out


def test_cmd_backtest_run_adapter(monkeypatch, capsys):
    import hft_platform.backtest.adapter as adapter_mod

    calls = {}

    class DummyAdapter:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def run(self):
            calls["ran"] = True

    monkeypatch.setattr(adapter_mod, "StrategyHbtAdapter", DummyAdapter)

    args = Namespace(
        backtest_cmd="run",
        data=["data.npz"],
        strategy_module="mod",
        strategy_class=None,
        strategy_id=None,
        symbol=None,
        tick_size=0.01,
        lot_size=1.0,
        price_scale=10000,
        timeout=0,
        tick_sizes=None,
        lot_sizes=None,
        symbols=None,
        latency_entry=None,
        latency_resp=None,
        fee_maker=None,
        fee_taker=None,
        seed=42,
        no_partial_fill=False,
        strict_equity=False,
        record_out=None,
        report=False,
    )
    cli.cmd_backtest(args)
    assert calls.get("ran") is True
    assert "Strategy backtest completed." in capsys.readouterr().out


def test_cmd_backtest_run_adapter_rejects_multi_data():
    args = Namespace(
        backtest_cmd="run",
        data=["d1.npz", "d2.npz"],
        strategy_module="mod",
        strategy_class="C",
        strategy_id="s",
        symbol="2330",
        tick_size=0.01,
        lot_size=1.0,
        price_scale=10000,
        timeout=0,
        tick_sizes=None,
        lot_sizes=None,
        symbols=None,
        latency_entry=None,
        latency_resp=None,
        fee_maker=None,
        fee_taker=None,
        seed=42,
        no_partial_fill=False,
        strict_equity=False,
        record_out=None,
        report=False,
    )
    with pytest.raises(SystemExit):
        cli.cmd_backtest(args)


def test_cmd_backtest_run_runner_rejects_multi_data_without_strategy():
    args = Namespace(
        backtest_cmd="run",
        data=["d1.npz", "d2.npz"],
        strategy_module=None,
        strategy_class=None,
        strategy_id="s",
        symbol="2330",
        tick_size=0.01,
        lot_size=1.0,
        price_scale=10000,
        timeout=0,
        tick_sizes=None,
        lot_sizes=None,
        symbols=None,
        latency_entry=None,
        latency_resp=None,
        fee_maker=None,
        fee_taker=None,
        seed=42,
        no_partial_fill=False,
        strict_equity=False,
        record_out=None,
        report=False,
    )
    with pytest.raises(SystemExit):
        cli.cmd_backtest(args)


def test_cmd_run_replay_exits(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda *_a, **_k: ({"mode": "replay"}, {}))
    args = Namespace(
        mode=None,
        mode_flag=None,
        symbols=None,
        strategy=None,
        strategy_module=None,
        strategy_class=None,
    )
    cli.cmd_run(args)
    out = capsys.readouterr().out
    assert "Replay mode not yet wired" in out


def test_cmd_run_downgrades_live(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda *_a, **_k: ({"mode": "live", "prometheus_port": 9091}, {}))
    monkeypatch.setattr(cli, "detect_live_credentials", lambda: False)
    monkeypatch.setattr(cli, "summarize_settings", lambda *_a, **_k: "summary")

    import types as _types

    class DummySystem:
        def __init__(self, *_a, **_k):
            pass

        async def run(self):
            return None

    dummy_main = _types.SimpleNamespace(HFTSystem=DummySystem)
    monkeypatch.setitem(sys.modules, "hft_platform.main", dummy_main)

    def _run(coro):
        coro.close()

    monkeypatch.setattr(cli, "asyncio", types.SimpleNamespace(run=_run))
    monkeypatch.setitem(sys.modules, "prometheus_client", _types.SimpleNamespace(start_http_server=lambda *_a, **_k: None))

    args = Namespace(
        mode="live",
        mode_flag=None,
        symbols=None,
        strategy=None,
        strategy_module=None,
        strategy_class=None,
    )
    cli.cmd_run(args)
    out = capsys.readouterr().out
    assert "downgrading to sim mode" in out


def test_cmd_symbols_build_with_warnings(monkeypatch, capsys, tmp_path):
    result = types.SimpleNamespace(symbols=[{"code": "2330"}], errors=[], warnings=["warn"])
    validation = types.SimpleNamespace(errors=[], warnings=[])
    dummy_mod = types.SimpleNamespace(
        build_symbols=lambda *_a, **_k: result,
        validate_symbols=lambda *_a, **_k: validation,
        preview_lines=lambda *_a, **_k: ["preview"],
        write_symbols_yaml=lambda *_a, **_k: None,
        load_contract_cache=lambda *_a, **_k: None,
    )
    monkeypatch.setitem(sys.modules, "hft_platform.config.symbols", dummy_mod)

    args = Namespace(
        list_path="config/symbols.list",
        output=str(tmp_path / "symbols.yaml"),
        contracts="config/contracts.json",
        metrics=None,
        no_contracts=True,
        max_subscriptions=200,
        preview=True,
        sample=1,
    )
    cli.cmd_symbols_build(args)
    out = capsys.readouterr().out
    assert "Warnings:" in out
    assert "Written 1 symbols" in out


def test_cmd_symbols_build_errors_exit(monkeypatch):
    result = types.SimpleNamespace(symbols=[], errors=["bad"], warnings=[])
    validation = types.SimpleNamespace(errors=[], warnings=[])
    dummy_mod = types.SimpleNamespace(
        build_symbols=lambda *_a, **_k: result,
        validate_symbols=lambda *_a, **_k: validation,
        preview_lines=lambda *_a, **_k: [],
        write_symbols_yaml=lambda *_a, **_k: None,
        load_contract_cache=lambda *_a, **_k: None,
    )
    monkeypatch.setitem(sys.modules, "hft_platform.config.symbols", dummy_mod)

    args = Namespace(
        list_path="config/symbols.list",
        output="config/symbols.yaml",
        contracts="config/contracts.json",
        metrics=None,
        no_contracts=True,
        max_subscriptions=200,
        preview=False,
        sample=1,
    )
    with pytest.raises(SystemExit):
        cli.cmd_symbols_build(args)


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

    monkeypatch.setattr(cli.subprocess, "run", _run)
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

    monkeypatch.setitem(sys.modules, "research.combinatorial.search_engine", types.SimpleNamespace(AlphaSearchEngine=_Engine))
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

        @staticmethod
        def to_dict():
            return {"alpha_id": "ofi_mc", "approved": True, "promotion_config_path": "config/strategy_promotions/x/ofi_mc.yaml"}

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
            return {"alpha_id": "rl_ofi", "approved": True, "promotion_config_path": "config/strategy_promotions/x/rl_ofi.yaml"}

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

    monkeypatch.setitem(sys.modules, "hft_platform.alpha.experiments", types.SimpleNamespace(ExperimentTracker=_Tracker))

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

    monkeypatch.setitem(sys.modules, "hft_platform.alpha.experiments", types.SimpleNamespace(ExperimentTracker=_Tracker))
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

    monkeypatch.setitem(sys.modules, "hft_platform.alpha.experiments", types.SimpleNamespace(ExperimentTracker=_Tracker))

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
