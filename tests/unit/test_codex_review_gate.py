"""What the push gate credits as a review, and what it refuses to.

The gate exists because "every deliverable gets a Codex review" was a
behavioural commitment with nothing behind it. These tests pin the one property
that makes it a control rather than a second commitment: the credit has to rest
on evidence a reviewer produced.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

HOOK_PATH = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "codex_review_gate.py"


def _load_hook(monkeypatch: pytest.MonkeyPatch, reports: Path) -> Any:
    spec = importlib.util.spec_from_file_location("codex_review_gate", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "REPORTS", reports)
    return module


def _report_dir(reports: Path, name: str, sha: str, body: str) -> Path:
    d = reports / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "REVIEWED_SHA").write_text(sha + "\n", encoding="utf-8")
    (d / "adversarial.md").write_text(body, encoding="utf-8")
    return d


_COMPLETED = "# Codex Adversarial Review\n\nTarget: branch diff\nVerdict: needs-attention\n\nFindings:\n- ...\n"
_DIED = "# Codex Adversarial Review\n\nCodex did not return valid structured JSON.\n\n- Parse error: usage limit\n"
_NO_RESPONSE = "# Codex Review\n\nTarget: branch diff against origin/main\n\nReviewer failed to output a response.\n"

SHA = "6d56494c17a0af15cb6a4756b6f2864fb8db48a4"


def test_a_completed_review_of_this_sha_is_credited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = _load_hook(monkeypatch, tmp_path)
    _report_dir(tmp_path, "20260827-1713-round5", SHA, _COMPLETED)
    assert hook._reviewed(SHA) is not None


def test_a_reviewer_that_died_mid_run_is_not_credited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stamp is written at launch, not at completion.

    Measured 2026-08-27: both reviewers hit a Codex usage limit, REVIEWED_SHA
    matched HEAD, and the push cleared a gate no reviewer had ever looked at.
    An empty review reads like an approval; it is not one.
    """
    hook = _load_hook(monkeypatch, tmp_path)
    _report_dir(tmp_path, "20260827-1713-round5", SHA, _DIED)
    assert hook._reviewed(SHA) is None


def test_a_reviewer_that_returned_nothing_is_not_credited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = _load_hook(monkeypatch, tmp_path)
    _report_dir(tmp_path, "20260827-1713-round5", SHA, _NO_RESPONSE)
    assert hook._reviewed(SHA) is None


def test_one_reviewer_reporting_is_enough_when_the_other_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plain reviewer has failed on every observed run; requiring both would
    block every push for a reason unrelated to the diff."""
    hook = _load_hook(monkeypatch, tmp_path)
    d = _report_dir(tmp_path, "20260827-1713-round5", SHA, _COMPLETED)
    (d / "review.md").write_text(_NO_RESPONSE, encoding="utf-8")
    assert hook._reviewed(SHA) is not None


def test_a_completed_review_of_a_different_sha_is_not_credited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = _load_hook(monkeypatch, tmp_path)
    _report_dir(tmp_path, "20260827-1010-earlier", "0" * 40, _COMPLETED)
    assert hook._reviewed(SHA) is None


def test_no_reports_directory_at_all_is_not_credited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = _load_hook(monkeypatch, tmp_path / "does-not-exist")
    assert hook._reviewed(SHA) is None


# --------------------------------------------------------------------------- #
# End to end: the hook's stdin/exit-code contract
# --------------------------------------------------------------------------- #


def _run_hook(payload: dict[str, Any], reports: Path) -> subprocess.CompletedProcess[str]:
    script = (
        "import importlib.util,sys,pathlib\n"
        f"spec=importlib.util.spec_from_file_location('g',{str(HOOK_PATH)!r})\n"
        "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"m.REPORTS=pathlib.Path({str(reports)!r})\n"
        "sys.exit(m.main())\n"
    )
    return subprocess.run(
        [__import__("sys").executable, "-c", script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_a_command_that_publishes_nothing_is_never_gated(tmp_path: Path) -> None:
    result = _run_hook({"tool_input": {"command": "git status --short"}}, tmp_path)
    assert result.returncode == 0


def test_an_explicit_override_is_allowed_and_says_so(tmp_path: Path) -> None:
    result = _run_hook({"tool_input": {"command": "CODEX_REVIEW_OVERRIDE=1 git push origin HEAD"}}, tmp_path)
    assert result.returncode == 0
    assert "overridden explicitly" in result.stderr


def test_malformed_stdin_allows_rather_than_wedging_the_session(tmp_path: Path) -> None:
    """A gate that blocks every push when its own parsing breaks costs more than
    the reviews it protects -- see the relative-path hook incident."""
    result = subprocess.run(
        [__import__("sys").executable, str(HOOK_PATH)],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
