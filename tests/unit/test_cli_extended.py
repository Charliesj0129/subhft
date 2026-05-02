import json
import sys
import types
from argparse import Namespace

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
    monkeypatch.setattr("hft_platform.cli._run.load_settings", lambda *a, **k: (settings, {}))
    monkeypatch.chdir(tmp_path)

    cli.cmd_check(Namespace(export="json"))

    out_path = tmp_path / "config" / "exported_settings.json"
    assert out_path.exists()
    assert json.loads(out_path.read_text())["symbols"] == ["2330"]


def test_cmd_check_export_yaml(tmp_path, monkeypatch):
    settings = {"symbols": ["2330"], "strategy": {"id": "s1"}}
    monkeypatch.setattr("hft_platform.cli._run.load_settings", lambda *a, **k: (settings, {}))
    monkeypatch.chdir(tmp_path)

    cli.cmd_check(Namespace(export="yaml"))

    out_path = tmp_path / "config" / "exported_settings.yaml"
    assert out_path.exists()
    assert "symbols" in out_path.read_text()


def test_cmd_check_missing_exits(monkeypatch):
    settings = {"symbols": [], "strategy": {}}
    monkeypatch.setattr("hft_platform.cli._run.load_settings", lambda *a, **k: (settings, {}))
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


def test_cmd_diag_timeline(tmp_path, capsys):
    trace_file = tmp_path / "t.jsonl"
    trace_file.write_text(
        json.dumps({"ts_ns": 1, "stage": "md_event", "trace_id": "x", "payload": {"a": 1}})
        + "\n"
        + json.dumps({"ts_ns": 2, "stage": "risk_approve", "trace_id": "x", "payload": {"b": 2}})
        + "\n",
        encoding="utf-8",
    )
    cli.cmd_diag(
        Namespace(
            trace_file=str(trace_file),
            trace_id="x",
            stage=None,
            limit=0,
            timeline=True,
            timeline_format="json",
            out=None,
        )
    )
    out = capsys.readouterr().out
    assert '"timeline"' in out
    assert "md_event" in out


def test_cmd_strat_test_import_failure(monkeypatch):
    monkeypatch.setattr("hft_platform.cli._ops.load_settings", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(
        "hft_platform.cli._ops.import_module", lambda *_a, **_k: (_ for _ in ()).throw(ImportError("nope"))
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_strat_test(Namespace(module="x", cls="Y", strategy_id="s", symbol="2330"))
    assert exc.value.code == 1


def test_feature_rollout_cli_roundtrip(tmp_path, capsys):
    profiles = tmp_path / "feature_profiles.yaml"
    profiles.write_text(
        "default_profile_id: p1\nprofiles:\n"
        "  - profile_id: p1\n    feature_set_id: lob_shared_v1\n    state: active\n"
        "  - profile_id: p2\n    feature_set_id: lob_shared_v1\n    state: shadow\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "rollout_state.json"
    cli.cmd_feature_rollout_set(
        Namespace(
            profiles=str(profiles),
            state_path=str(state_path),
            feature_set="lob_shared_v1",
            state="active",
            profile_id="p1",
            actor="test",
            notes="",
        )
    )
    cli.cmd_feature_rollout_set(
        Namespace(
            profiles=str(profiles),
            state_path=str(state_path),
            feature_set="lob_shared_v1",
            state="active",
            profile_id="p2",
            actor="test",
            notes="switch",
        )
    )
    cli.cmd_feature_rollout_rollback(
        Namespace(state_path=str(state_path), feature_set="lob_shared_v1", actor="test", notes="rb")
    )
    cli.cmd_feature_rollout_status(
        Namespace(profiles=str(profiles), state_path=str(state_path), feature_set="lob_shared_v1")
    )
    out = capsys.readouterr().out
    assert "lob_shared_v1" in out
    assert "resolved_profile_id" in out


def test_cmd_strat_test_success(monkeypatch, capsys):
    class DummyStrategy:
        def __init__(self, strategy_id: str):
            self.strategy_id = strategy_id

        def handle_event(self, *_a, **_k):
            return []

    dummy_mod = types.SimpleNamespace(DummyStrategy=DummyStrategy)
    monkeypatch.setattr("hft_platform.cli._ops.load_settings", lambda *a, **k: ({"symbols": ["2330"]}, {}))
    monkeypatch.setattr("hft_platform.cli._ops.import_module", lambda *_a, **_k: dummy_mod)

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
    monkeypatch.setattr("hft_platform.cli._run.load_settings", lambda *_a, **_k: ({"mode": "replay"}, {}))
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


@pytest.mark.skip(reason="requires full prometheus_client sub-module mock; tracks as known fragile test")
def test_cmd_run_downgrades_live(monkeypatch, capsys):
    monkeypatch.setattr(
        "hft_platform.cli._run.load_settings", lambda *_a, **_k: ({"mode": "live", "prometheus_port": 9091}, {})
    )
    monkeypatch.setattr("hft_platform.cli._run.detect_live_credentials", lambda: False)
    monkeypatch.setattr("hft_platform.cli._run.summarize_settings", lambda *_a, **_k: "summary")

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

    monkeypatch.setattr("hft_platform.cli._run.asyncio", types.SimpleNamespace(run=_run))
    # Build a stub that satisfies all prometheus_client imports the CLI may trigger
    _fake_metric = lambda *a, **k: _types.SimpleNamespace(
        labels=lambda **_: _types.SimpleNamespace(inc=lambda *_: None, set=lambda *_: None, observe=lambda *_: None),
        inc=lambda *_: None,
        set=lambda *_: None,
        observe=lambda *_: None,
    )
    _prom_stub = _types.SimpleNamespace(
        start_http_server=lambda *_a, **_k: None,
        REGISTRY=_types.SimpleNamespace(unregister=lambda *_: None),
        Counter=_fake_metric,
        Gauge=_fake_metric,
        Histogram=_fake_metric,
        Summary=_fake_metric,
        CollectorRegistry=type("CollectorRegistry", (), {}),
    )
    monkeypatch.setitem(sys.modules, "prometheus_client", _prom_stub)

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
