"""``docker stop`` must wait at least as long as the engine's own shutdown budget (candidate inventory N76).

Docker sends SIGKILL ``stop_grace_period`` after SIGTERM, 10 s unless the service sets one. The engine's shutdown
(``hft_platform.main``) waits up to ``HFT_SHUTDOWN_GRACE_TIMEOUT_S`` (default 90 s) for ``HFTSystem.stop_async``, and
that includes the recorder's own flush wait (``HFT_RECORDER_SHUTDOWN_TIMEOUT_S`` + 5 s). Once PID 1 became python
(``test_engine_compose_command_execs_python``) SIGTERM reached the shutdown path, but a slow flush was still killed at
10 s. These tests read the real compose file and the real defaults in the source, not copies of them.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
MAIN = REPO_ROOT / "src" / "hft_platform" / "main.py"

# Slack the process needs after its shutdown budget runs out: the budget is a timeout on the wait, and the
# interpreter still has to log, run exit handlers and return before Docker's clock ends.
MIN_MARGIN_S = 5.0

pytestmark = pytest.mark.unit


def _getenv_default(path: Path, name: str) -> float:
    """The default of ``os.getenv(name, "<default>")`` in ``path``, read from the syntax tree.

    Every call that reads ``name`` in the file must give the same default, or the answer would be ambiguous.
    """
    found = {
        float(node.args[1].value)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getenv"
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == name
        and isinstance(node.args[1], ast.Constant)
    }
    assert len(found) == 1, f"expected one default for os.getenv({name!r}, <default>) in {path}, found {sorted(found)}"
    return next(iter(found))


def _duration_s(value: object) -> float:
    """A compose duration (``100s``, ``1m40s``, ``1h``) in seconds. Compose wants a unit, so a bare number is refused."""
    text = str(value).strip()
    parts = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", text)
    assert parts is not None and any(parts.groups()), f"unrecognised compose duration: {value!r}"
    hours, minutes, seconds = (float(part) if part else 0.0 for part in parts.groups())
    return hours * 3600 + minutes * 60 + seconds


def _engine() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["hft-engine"]


def test_engine_stop_grace_period_is_set():
    assert "stop_grace_period" in _engine(), "hft-engine falls back to Docker's 10 s SIGKILL"


def test_engine_stop_grace_period_outlasts_the_shutdown_budget():
    budget_s = _getenv_default(MAIN, "HFT_SHUTDOWN_GRACE_TIMEOUT_S")
    grace_s = _duration_s(_engine().get("stop_grace_period", "10s"))

    assert grace_s >= budget_s + MIN_MARGIN_S, (
        f"stop_grace_period {grace_s:g}s must be at least {MIN_MARGIN_S:g}s above the {budget_s:g}s shutdown budget"
    )


def test_shutdown_budget_covers_the_recorder_flush_wait():
    """The recorder wait runs inside the shutdown budget, so a larger recorder wait would be cut off by it."""
    system = REPO_ROOT / "src" / "hft_platform" / "services" / "system.py"
    recorder_wait_s = _getenv_default(system, "HFT_RECORDER_SHUTDOWN_TIMEOUT_S") + 5.0

    assert recorder_wait_s < _getenv_default(MAIN, "HFT_SHUTDOWN_GRACE_TIMEOUT_S")


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("100s", 100.0), ("1m40s", 100.0), ("1h", 3600.0), ("1.5m", 90.0)],
)
def test_duration_parser_reads_compose_durations(text, seconds):
    assert _duration_s(text) == seconds


@pytest.mark.parametrize("value", ["soon", "", "90", 100])
def test_duration_parser_rejects_what_compose_would_reject(value):
    with pytest.raises(AssertionError):
        _duration_s(value)
