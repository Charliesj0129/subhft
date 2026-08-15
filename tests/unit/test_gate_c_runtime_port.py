from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import cast

from hft_platform.alpha import _gate_c
from hft_platform.alpha.gate_c_runtime import GateCMakerRuntime, GateCRuntime, GateCTakerRuntime
from research.backtest.gate_c_runtime import build_gate_c_runtime

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _research_imports(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None and node.module.startswith("research."):
            imports.append((node.module, node.lineno))
        elif isinstance(node, ast.Import):
            imports.extend((alias.name, node.lineno) for alias in node.names if alias.name.startswith("research."))
    return sorted(imports)


def test_gate_c_platform_policy_has_only_explicit_compatibility_adapter_edge() -> None:
    path = _REPO_ROOT / "src" / "hft_platform" / "alpha" / "_gate_c.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    loader = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_load_gate_c_runtime"
    )
    gate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_gate_c")

    assert [module for module, _ in _research_imports(path)] == ["research.backtest.gate_c_runtime"]
    assert any(
        isinstance(node, ast.ImportFrom) and node.module == "research.backtest.gate_c_runtime"
        for node in ast.walk(loader)
    )
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module is not None and node.module.startswith("research.")
        for node in ast.walk(gate)
    )


def test_research_adapter_binds_all_gate_c_runtime_dependencies() -> None:
    from research.backtest.cost_models import load_cost_profile
    from research.backtest.fill_models import QueueDepletionFill
    from research.backtest.hft_native_runner import HftNativeRunner, ensure_hftbt_npz
    from research.backtest.maker_engine import ClickHouseSource, LatencyProfile, MakerEngine
    from research.backtest.result_store import ResultStore
    from research.backtest.taker_engine import TakerEngine
    from research.backtest.types import BacktestConfig, WalkForwardConfig
    from research.registry.scorecard import compute_scorecard

    runtime = build_gate_c_runtime()

    assert isinstance(runtime, GateCRuntime)
    assert runtime.hft_native_runner is HftNativeRunner
    assert runtime.ensure_hftbt_npz is ensure_hftbt_npz
    assert runtime.backtest_config is BacktestConfig
    assert runtime.walk_forward_config is WalkForwardConfig
    assert runtime.compute_scorecard is compute_scorecard

    maker_runtime = runtime.load_maker_runtime()
    assert isinstance(maker_runtime, GateCMakerRuntime)
    assert maker_runtime.load_cost_profile is load_cost_profile
    assert maker_runtime.queue_depletion_fill is QueueDepletionFill
    assert maker_runtime.clickhouse_source is ClickHouseSource
    assert maker_runtime.latency_profile is LatencyProfile
    assert maker_runtime.maker_engine is MakerEngine
    assert maker_runtime.result_store is ResultStore

    taker_runtime = runtime.load_taker_runtime()
    assert isinstance(taker_runtime, GateCTakerRuntime)
    assert taker_runtime.result_store is ResultStore
    assert taker_runtime.taker_engine is TakerEngine


def test_gate_c_runtime_prefers_injected_port(monkeypatch) -> None:
    injected = cast(GateCRuntime, object())

    monkeypatch.setattr(
        _gate_c,
        "_load_gate_c_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("compatibility adapter must not load")),
    )

    assert _gate_c._resolve_gate_c_runtime(injected) is injected


def test_gate_c_runtime_loads_compatibility_adapter_when_not_injected(monkeypatch) -> None:
    loaded = cast(GateCRuntime, object())
    monkeypatch.setattr(_gate_c, "_load_gate_c_runtime", lambda: loaded)

    assert _gate_c._resolve_gate_c_runtime(None) is loaded


def test_gate_c_common_runtime_does_not_eagerly_load_lane_implementations() -> None:
    probe = "\n".join(
        (
            "import sys",
            "from research.backtest.gate_c_runtime import build_gate_c_runtime",
            "build_gate_c_runtime()",
            "assert 'research.backtest.maker_engine' not in sys.modules",
            "assert 'research.backtest.fill_models' not in sys.modules",
            "assert 'research.backtest.taker_engine' not in sys.modules",
            "assert 'research.backtest.result_store' not in sys.modules",
            "assert 'research.backtest.cost_models' not in sys.modules",
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_gate_c_lane_runtime_loaders_remain_isolated() -> None:
    maker_probe = "\n".join(
        (
            "import sys",
            "from research.backtest.gate_c_runtime import build_gate_c_runtime",
            "build_gate_c_runtime().load_maker_runtime()",
            "assert 'research.backtest.maker_engine' in sys.modules",
            "assert 'research.backtest.taker_engine' not in sys.modules",
        )
    )
    taker_probe = "\n".join(
        (
            "import sys",
            "from research.backtest.gate_c_runtime import build_gate_c_runtime",
            "build_gate_c_runtime().load_taker_runtime()",
            "assert 'research.backtest.taker_engine' in sys.modules",
            "assert 'research.backtest.maker_engine' not in sys.modules",
            "assert 'research.backtest.fill_models' not in sys.modules",
            "assert 'research.backtest.cost_models' not in sys.modules",
        )
    )

    maker_completed = subprocess.run(
        [sys.executable, "-c", maker_probe],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    taker_completed = subprocess.run(
        [sys.executable, "-c", taker_probe],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert maker_completed.returncode == 0, maker_completed.stderr
    assert taker_completed.returncode == 0, taker_completed.stderr
