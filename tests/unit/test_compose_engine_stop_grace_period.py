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
SRC = REPO_ROOT / "src" / "hft_platform"
SYSTEM = SRC / "services" / "system.py"

SHUTDOWN_BUDGET_VAR = "HFT_SHUTDOWN_GRACE_TIMEOUT_S"
RECORDER_TIMEOUT_VAR = "HFT_RECORDER_SHUTDOWN_TIMEOUT_S"

# Slack the process needs after its shutdown budget runs out: the budget is a timeout on the wait, and the
# interpreter still has to log, run exit handlers and return before Docker's clock ends.
MIN_MARGIN_S = 5.0

pytestmark = pytest.mark.unit


def _getenv_default_of(node: ast.AST, name: str) -> float | None:
    """The default of a call ``os.getenv(name, "<default>")``, or ``None`` if ``node`` is not that call."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getenv"
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == name
        and isinstance(node.args[1], ast.Constant)
    ):
        return float(node.args[1].value)
    return None


def _one_default(name: str) -> float:
    """The default every ``os.getenv(name, "<default>")`` under ``src/`` gives, read from the syntax trees.

    A second reader with another default (the recorder's own flush wait is read in ``recorder/worker.py`` as well as
    in ``services/system.py``) would make the answer ambiguous, so they must all agree.
    """
    found: dict[str, float] = {}
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if name not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            default = _getenv_default_of(node, name)
            if default is not None:
                found[f"{path.relative_to(REPO_ROOT)}:{node.lineno}"] = default
    assert found, f"nothing under src/ reads os.getenv({name!r}, <default>)"
    assert len(set(found.values())) == 1, f"readers of {name} disagree on the default: {found}"
    return next(iter(found.values()))


def _recorder_wait_s() -> float:
    """What ``stop_async`` waits for the recorder: ``float(os.getenv(NAME, "<default>")) + <slack>`` in system.py."""
    found = set()
    for node in ast.walk(ast.parse(SYSTEM.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and isinstance(node.right, ast.Constant)):
            continue
        call = node.left
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "float" and call.args:
            default = _getenv_default_of(call.args[0], RECORDER_TIMEOUT_VAR)
            if default is not None:
                found.add(default + float(node.right.value))
    assert len(found) == 1, (
        f"expected one 'float(os.getenv({RECORDER_TIMEOUT_VAR!r}, ...)) + slack' in system.py: {found}"
    )
    return next(iter(found))


_UNIT_S = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6}
_DURATION_PART = r"\d+(?:\.\d+)?(?:h|ms|us|m|s)"


def _duration_s(value: object) -> float:
    """A compose duration (``100s``, ``1m40s``, ``100000ms``) in seconds. Compose wants a unit, so a bare number is refused."""
    text = str(value).strip()
    assert re.fullmatch(rf"(?:{_DURATION_PART})+", text), f"unrecognised compose duration: {value!r}"
    return sum(float(number) * _UNIT_S[unit] for number, unit in re.findall(r"(\d+(?:\.\d+)?)(h|ms|us|m|s)", text))


def _engine() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["hft-engine"]


def test_engine_stop_grace_period_is_set():
    assert "stop_grace_period" in _engine(), "hft-engine falls back to Docker's 10 s SIGKILL"


def test_engine_stop_grace_period_outlasts_the_shutdown_budget():
    budget_s = _one_default(SHUTDOWN_BUDGET_VAR)
    grace_s = _duration_s(_engine().get("stop_grace_period", "10s"))

    assert grace_s >= budget_s + MIN_MARGIN_S, (
        f"stop_grace_period {grace_s:g}s must be at least {MIN_MARGIN_S:g}s above the {budget_s:g}s shutdown budget"
    )


def test_shutdown_budget_covers_the_recorder_flush_wait():
    """The recorder wait runs inside the shutdown budget, so a larger recorder wait would be cut off by it."""
    assert _recorder_wait_s() < _one_default(SHUTDOWN_BUDGET_VAR)


def test_the_recorders_own_flush_wait_fits_inside_the_wait_for_it():
    """``recorder/worker.py`` reads the same variable for its own flush; if its default were larger than what
    ``stop_async`` waits, the flush would be cut off by the caller while the recorder thinks it still has time."""
    assert _one_default(RECORDER_TIMEOUT_VAR) < _recorder_wait_s()


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("100s", 100.0),
        ("1m40s", 100.0),
        ("1h", 3600.0),
        ("1.5m", 90.0),
        ("100000ms", 100.0),
        ("1m30s500ms", 90.5),
        ("1500000us", 1.5),
    ],
)
def test_duration_parser_reads_compose_durations(text, seconds):
    assert _duration_s(text) == seconds


@pytest.mark.parametrize("value", ["soon", "", "90", 100])
def test_duration_parser_rejects_what_compose_would_reject(value):
    with pytest.raises(AssertionError):
        _duration_s(value)
