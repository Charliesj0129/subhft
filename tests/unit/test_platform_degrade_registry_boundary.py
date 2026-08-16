from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import Any

from hft_platform.ops.platform_degrade_registry import (
    get_or_create_shared_controller,
    reset_shared_controller,
    try_force_clear_shared_controller,
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def test_manual_rearm_uses_registry_without_concrete_controller_import() -> None:
    source_path = Path(__file__).parents[2] / "src/hft_platform/ops/manual_rearm.py"
    imported = _imported_modules(source_path)

    assert "hft_platform.ops.platform_degrade_registry" in imported
    assert "hft_platform.ops.platform_degrade" not in imported


def test_platform_degrade_registry_preserves_singleton_update_and_force_clear() -> None:
    class Controller:
        def __init__(self) -> None:
            self.metrics: Any | None = None
            self.sync_count = 0
            self.force_clear_reasons: list[str] = []

        def _sync_metrics(self) -> None:
            self.sync_count += 1

        def force_clear(self, *, reason: str = "manual_rearm") -> object | None:
            self.force_clear_reasons.append(reason)
            return None

    reset_shared_controller()
    try:
        controller = get_or_create_shared_controller(Controller, metrics=None)
        metrics = object()
        same_controller = get_or_create_shared_controller(Controller, metrics=metrics)

        assert same_controller is controller
        assert controller.metrics is metrics
        assert controller.sync_count == 1
        assert try_force_clear_shared_controller(reason="test") is True
        assert controller.force_clear_reasons == ["test"]
    finally:
        reset_shared_controller()

    assert try_force_clear_shared_controller(reason="after_reset") is False


def test_platform_degrade_registry_serializes_concurrent_first_access() -> None:
    class Controller:
        metrics: Any | None = None

        def _sync_metrics(self) -> None:
            return None

        def force_clear(self, *, reason: str = "manual_rearm") -> object | None:
            return None

    callers_ready = Barrier(3)
    factory_entered = Event()
    release_factory = Event()
    count_lock = Lock()
    factory_calls = 0

    def factory() -> Controller:
        nonlocal factory_calls
        with count_lock:
            factory_calls += 1
        factory_entered.set()
        assert release_factory.wait(timeout=1.0)
        return Controller()

    def get_controller() -> Controller:
        callers_ready.wait(timeout=1.0)
        return get_or_create_shared_controller(factory, metrics=None)

    reset_shared_controller()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(get_controller) for _ in range(2)]
            callers_ready.wait(timeout=1.0)
            assert factory_entered.wait(timeout=1.0)
            release_factory.set()
            controllers = [future.result(timeout=1.0) for future in futures]

        assert factory_calls == 1
        assert controllers[0] is controllers[1]
    finally:
        release_factory.set()
        reset_shared_controller()
