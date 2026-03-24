import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _run_cli(args, cwd=None, extra_paths=None):
    env = os.environ.copy()
    extra = [str(p) for p in (extra_paths or [])]
    extra.append(str(ROOT / "src"))
    env["PYTHONPATH"] = os.pathsep.join(extra)
    return subprocess.run(
        [sys.executable, "-m", "hft_platform.cli", *args],
        cwd=str(cwd or ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.blackbox
def test_cli_check_and_diag():
    check = _run_cli(["check"])
    assert check.returncode == 0
    assert "Configuration is valid." in check.stdout

    diag = _run_cli(["diag"])
    assert diag.returncode == 0
    assert "Diag:" in diag.stdout


@pytest.mark.blackbox
def test_cli_init_and_export(tmp_path):
    init = _run_cli(["init", "--strategy-id", "bbx", "--symbol", "2330"], cwd=tmp_path)
    assert init.returncode == 0
    assert (tmp_path / "config/settings.py").exists()
    assert (tmp_path / "src/hft_platform/strategies/bbx.py").exists()
    assert (tmp_path / "tests/test_bbx.py").exists()

    export = _run_cli(["check", "--export", "json"], cwd=tmp_path)
    assert export.returncode == 0
    exported = tmp_path / "config/exported_settings.json"
    assert exported.exists()
    assert "symbols" in exported.read_text()

    export_yaml = _run_cli(["check", "--export", "yaml"], cwd=tmp_path)
    assert export_yaml.returncode == 0
    exported_yaml = tmp_path / "config/exported_settings.yaml"
    assert exported_yaml.exists()


@pytest.mark.blackbox
def test_cli_check_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_SYMBOLS", "AAA,BBB")
    monkeypatch.setenv("HFT_PROM_PORT", "12345")

    export = _run_cli(["check", "--export", "json"], cwd=tmp_path)
    assert export.returncode == 0

    exported = tmp_path / "config/exported_settings.json"
    payload = json.loads(exported.read_text())

    assert payload["symbols"] == ["AAA", "BBB"]
    assert payload["prometheus_port"] == 12345


@pytest.mark.blackbox
def test_cli_strat_test_simple_mm():
    result = _run_cli(
        [
            "strat",
            "test",
            "--module",
            "hft_platform.strategies.simple_mm",
            "--cls",
            "SimpleMarketMaker",
            "--strategy-id",
            "demo",
            "--symbol",
            "2330",
        ]
    )
    assert result.returncode == 0
    assert "Strategy emitted" in result.stdout


@pytest.mark.blackbox
def test_cli_feed_status_unreachable():
    result = _run_cli(["feed", "status", "--port", "9"])
    assert result.returncode == 0
    assert "Unable to reach metrics" in result.stdout


@pytest.mark.blackbox
def test_cli_ops_rearm_strategy_updates_runtime_state(tmp_path):
    state_path = tmp_path / "outputs/production_rollout/autonomy/runtime_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {
                    "strat_a": {"manual_rearm_required": True, "reason": "strategy_reject_spike"},
                },
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli(["ops", "rearm-strategy", "--strategy-id", "strat_a"], cwd=tmp_path)

    assert result.returncode == 0
    assert "strategy re-armed: strat_a" in result.stdout.lower()
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["strategies"]["strat_a"]["manual_rearm_required"] is False
    assert payload["strategies"]["strat_a"]["reason"] is None


@pytest.mark.blackbox
def test_cli_ops_rearm_platform_emits_success_message(tmp_path):
    state_path = tmp_path / "outputs/production_rollout/autonomy/runtime_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "platform": {"manual_rearm_required": True, "reason": "clickhouse_unhealthy"},
                "strategies": {},
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli(["ops", "rearm-platform"], cwd=tmp_path)

    assert result.returncode == 0
    assert "platform re-armed" in result.stdout.lower()
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["platform"]["manual_rearm_required"] is False
    assert payload["platform"]["reason"] is None


@pytest.mark.blackbox
def test_cli_backtest_convert_and_run(tmp_path):
    stub = tmp_path / "stub"
    pkg = stub / "hftbacktest"
    dist_info = stub / "hftbacktest-2.4.0.dist-info"
    pkg.mkdir(parents=True)
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: hftbacktest\nVersion: 2.4.0\n", encoding="utf-8")

    (pkg / "__init__.py").write_text(
        "\n".join(
            [
                "class BacktestAsset:",
                "    def data(self, *a, **k): return self",
                "    def linear_asset(self, *a, **k): return self",
                "    def constant_latency(self, *a, **k): return self",
                "    def power_prob_queue_model(self, *a, **k): return self",
                "    def int_order_id_converter(self): return self",
                "",
                "class ConstantLatency:",
                "    def __init__(self, *a, **k): pass",
                "",
                "class LinearAsset:",
                "    def __init__(self, *a, **k): pass",
                "",
                "class PowerProbQueueModel:",
                "    def __init__(self, *a, **k): pass",
                "",
                "class HashMapMarketDepthBacktest:",
                "    def __init__(self, *a, **k):",
                "        self.current_timestamp = 0",
                "    def wait_next_feed(self, *a, **k): return 1",
                "    def run(self): return False",
                "    def elapse(self, *a, **k): return False",
                "    def depth(self, *a, **k): raise RuntimeError('depth not used')",
                "    def position(self, *a, **k): return 0",
                "    def submit_buy_order(self, *a, **k): pass",
                "    def submit_sell_order(self, *a, **k): pass",
                "    def cancel(self, *a, **k): pass",
                "    def close(self): return True",
            ]
        )
        + "\n"
    )
    (pkg / "order.py").write_text("IOC=object()\nGTC=object()\nLIMIT=object()\nROD=object()\nLimit=object()\n")
    (pkg / "types.py").write_text(
        "\n".join(
            [
                "import numpy as np",
                "event_dtype = np.dtype([('ev','i4'),('exch','i8'),('local','i8'),('px','f8'),('qty','f8'),('r1','i8'),('r2','i8'),('r3','f8')])",
                "DEPTH_EVENT=1",
                "TRADE_EVENT=2",
                "EXCH_EVENT=4",
                "LOCAL_EVENT=8",
                "BUY_EVENT=16",
                "SELL_EVENT=32",
            ]
        )
        + "\n"
    )

    jsonl = tmp_path / "events.jsonl"
    jsonl.write_text(
        "\n".join(
            [
                '{"type":"BidAsk","ts":1,"bids":[{"price":10000,"volume":1}],"asks":[{"price":10100,"volume":2}]}',
                '{"type":"Tick","ts":2,"price":123400,"volume":3}',
            ]
        )
        + "\n"
    )

    out = tmp_path / "out.npz"
    convert = _run_cli(
        ["backtest", "convert", "--input", str(jsonl), "--output", str(out), "--scale", "10000"],
        cwd=tmp_path,
        extra_paths=[stub],
    )
    assert convert.returncode == 0
    assert out.exists()

    run = _run_cli(
        [
            "backtest",
            "run",
            "--data",
            str(out),
            "--strategy-module",
            "hft_platform.strategies.simple_mm",
            "--strategy-class",
            "SimpleMarketMaker",
            "--strategy-id",
            "demo",
            "--symbol",
            "2330",
            "--price-scale",
            "10000",
        ],
        cwd=tmp_path,
        extra_paths=[stub],
    )
    assert run.returncode == 0
