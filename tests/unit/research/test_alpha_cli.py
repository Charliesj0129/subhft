"""research/registry/alpha_cli.py: the bodies behind the forwarded `hft alpha` research subcommands."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hft_platform.alpha.gate_c_runtime import GateCRuntime
from hft_platform.cli._parser import build_parser
from hft_platform.cli._research import forward_to_research
from research.registry import alpha_cli

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _forwarded_paths() -> set[tuple[str, str | None]]:
    alpha = _subparsers(build_parser())["alpha"]
    found: set[tuple[str, str | None]] = set()
    for name, sub in _subparsers(alpha).items():
        nested = _subparsers(sub)
        leaves = nested.items() if nested else [(None, sub)]
        for leaf_name, leaf in leaves:
            if leaf.get_default("func") is forward_to_research:
                found.add((name, leaf_name))
    return found


def test_every_forwarded_platform_subcommand_has_exactly_one_research_body() -> None:
    assert _forwarded_paths() == set(alpha_cli.HANDLERS)


@pytest.mark.parametrize(
    ("argv", "body"),
    [
        (["validate", "--alpha-id", "a1", "--data", "d.npz"], "cmd_alpha_validate"),
        (["mine", "status", "--run-dir", "runs/x"], "cmd_alpha_mine_status"),
        (["pipeline", "triage", "--alpha-id", "a1", "--owner", "o", "--data", "d.npy"], "cmd_alpha_pipeline_triage"),
        (["batch-correlation", "--dry-run"], "cmd_alpha_batch_correlation"),
        (["promote-batch", "--profile", "vm_ul6_strict_v2"], "cmd_alpha_promote_batch"),
    ],
)
def test_handler_for_maps_a_parsed_command_to_its_body(argv: list[str], body: str) -> None:
    args = build_parser().parse_args(["alpha", *argv])

    assert alpha_cli.handler_for(args) is getattr(alpha_cli, body)


def test_main_returns_the_body_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def _exit_2(_args):
        sys.exit(2)

    monkeypatch.setitem(alpha_cli.HANDLERS, ("mine", "status"), _exit_2)

    assert alpha_cli.main(["mine", "status", "--run-dir", "x"]) == 2


def test_main_refuses_a_platform_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert alpha_cli.main(["list"]) == 2

    assert "runs only the research subcommands" in capsys.readouterr().err


def test_validate_injects_the_research_gate_c_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def _run(config, *, gate_c_runtime=None):
        seen["runtime"] = gate_c_runtime
        return types.SimpleNamespace(passed=True, to_dict=lambda: {"passed": True})

    monkeypatch.setitem(
        sys.modules,
        "hft_platform.alpha.validation",
        types.SimpleNamespace(ValidationConfig=lambda **kw: kw, run_alpha_validation=_run),
    )
    monkeypatch.setattr(
        alpha_cli._platform_alpha, "_load_strict_validation_profile", lambda _arg: types.SimpleNamespace()
    )
    args = build_parser().parse_args(
        ["alpha", "validate", "--alpha-id", "a1", "--data", "d.npz", "--profile", "vm_ul6_strict_v2"]
    )

    alpha_cli.cmd_alpha_validate(args)

    assert isinstance(seen["runtime"], GateCRuntime)


def test_hft_alpha_research_subcommand_runs_end_to_end_in_a_child_process(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_run"
    probe = f"import sys; from hft_platform.cli import main; sys.exit(main(['alpha', 'mine', 'status', '--run-dir', {str(missing)!r}]))"

    done = subprocess.run(
        [sys.executable, "-c", probe], cwd=tmp_path, capture_output=True, text=True, check=False, timeout=25
    )

    # The child ran research code although cwd is outside the checkout: the forward set its PYTHONPATH.
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["run_dir"] == str(missing)
