from __future__ import annotations

import ast
from pathlib import Path
from typing import get_type_hints

from hft_platform.recorder.wal_scheduler import WALFlushTarget, WALScheduler


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def test_wal_scheduler_depends_on_flush_protocol_not_concrete_loader() -> None:
    source_path = Path(__file__).parents[2] / "src/hft_platform/recorder/wal_scheduler.py"

    assert "hft_platform.recorder.loader" not in _imported_modules(source_path)
    assert get_type_hints(WALScheduler.__init__)["loader"] is WALFlushTarget


def test_wal_scheduler_accepts_structural_flush_target() -> None:
    class FlushTarget:
        def __init__(self) -> None:
            self.force_values: list[bool] = []

        def process_files(self, force: bool = False) -> None:
            self.force_values.append(force)

    target = FlushTarget()
    scheduler = WALScheduler(target)

    assert scheduler.trigger_flush() is True
    assert target.force_values == [True]
