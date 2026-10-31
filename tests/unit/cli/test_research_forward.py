"""`hft alpha` research subcommands run as a `python -m research alpha` child process (research v2 C5)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import hft_platform.cli as cli
from hft_platform.cli import _research
from hft_platform.cli._parser import build_parser
from hft_platform.cli._research import forward_to_research, run_research_alpha


@pytest.fixture
def child(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {}

    def _run(cmd, *, env, check):
        seen["cmd"] = cmd
        seen["env"] = env
        seen["check"] = check
        return SimpleNamespace(returncode=seen.get("rc", 0))

    monkeypatch.setattr(_research.subprocess, "run", _run)
    # cli.main() calls configure_logging(), whose cache_logger_on_first_use=True
    # blinds structlog.testing.capture_logs for later tests on the same worker
    # (tests/unit/conftest.py:module_log_sink); patch it as the neighbours do.
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)
    return seen


@pytest.mark.parametrize(
    "argv",
    [
        ["alpha", "validate", "--alpha-id", "a1", "--data", "d.npz", "--profile", "vm_ul6_strict_v2"],
        ["alpha", "mine", "status", "--run-dir", "runs/x"],
        ["alpha", "pipeline", "run", "--alpha-id", "a1", "--owner", "o", "--data", "d.npy"],
        ["alpha", "scaffold", "new_alpha", "--complexity", "ON"],
    ],
)
def test_research_subcommand_forwards_its_exact_argv_to_the_research_entry(child: dict, argv: list[str]) -> None:
    assert cli.main(list(argv)) == 0

    assert child["cmd"] == [sys.executable, "-m", "research", "alpha", *argv[1:]]
    assert child["check"] is False


def test_research_subcommand_returns_the_child_exit_code(child: dict) -> None:
    child["rc"] = 2

    assert cli.main(["alpha", "mine", "status", "--run-dir", "runs/x"]) == 2


def test_forward_puts_the_checkout_first_on_the_child_pythonpath(
    child: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "__main__.py").write_text("")
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")

    assert run_research_alpha(["validate"], root=tmp_path) == 0

    assert child["env"]["PYTHONPATH"].split(":") == [str(tmp_path), "/elsewhere"]


def test_forward_without_a_research_checkout_fails_with_a_message_and_runs_nothing(
    child: dict, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_research_alpha(["validate", "--alpha-id", "a1"], root=tmp_path) == 2

    assert "cmd" not in child
    assert "needs the research/ checkout" in capsys.readouterr().err


def test_platform_alpha_subcommand_runs_in_process_and_is_not_forwarded(
    child: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list = []
    monkeypatch.setattr("hft_platform.cli._parser.cmd_alpha_list", lambda args: ran.append(args))

    assert cli.main(["alpha", "list"]) == 0

    assert len(ran) == 1
    assert "cmd" not in child


def test_forwarded_subcommands_keep_their_platform_argument_errors(child: dict) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["alpha", "validate", "--no-such-flag"])

    assert exc.value.code == 2
    assert "cmd" not in child


def test_forward_marker_is_never_called_as_a_command() -> None:
    args = build_parser().parse_args(["alpha", "validate", "--alpha-id", "a1", "--data", "d.npz"])

    assert args.func is forward_to_research
    with pytest.raises(RuntimeError, match="dispatched by hft_platform.cli.main"):
        args.func(args)
