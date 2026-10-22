"""PID 1 in the engine container must be python, not a shell around it.

2026-10-03: ``sh -lc "VAR=x python -m hft_platform.main"`` left ``sh`` as PID 1
with python as its child. PID 1 gets no default signal action, and sh handles
only SIGINT/SIGCHLD, so ``docker stop``'s SIGTERM was dropped and every stop
ended in SIGKILL after 10 s (exit 137), skipping the engine's shutdown path.
"""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def test_engine_command_replaces_the_shell_with_python():
    command = yaml.safe_load(COMPOSE.read_text())["services"]["hft-engine"]["command"]

    assert command[:2] == ["sh", "-lc"]
    assert command[2].rstrip().endswith("exec python -m hft_platform.main")
