"""Slice-D Task 15: tests for ``scripts/migrate_alpha_manifests.py``.

The script backfills the kill ledger from the 2026-04-17 archive sweep.
Tests cover dry-run discipline, apply path, idempotency, malformed
manifest fallback, summary dedupe, and live corpus counts (DoD-D2).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from hft_platform.alpha import audit, kill_ledger

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "migrate_alpha_manifests.py"


def _load_script_module() -> Any:
    """Import the script as a module under a stable name."""
    spec = importlib.util.spec_from_file_location("_migrate_alpha_manifests_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script() -> Any:
    return _load_script_module()


@pytest.fixture(autouse=True)
def _isolated_jsonl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Force kill_ledger jsonl path to tmp_path; force CH off."""
    jsonl = tmp_path / "_kill_ledger.jsonl"
    monkeypatch.setenv("HFT_ALPHA_KILL_LEDGER_PATH", str(jsonl))
    monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "0")
    audit._ENABLED = None  # noqa: SLF001 -- re-read env on next call
    kill_ledger._reset_cache_for_tests()
    return jsonl


def _make_archive(tmp_path: Path, with_manifest: list[str], without: list[str]) -> Path:
    """Build a synthetic archive layout under tmp_path."""
    archive = tmp_path / "archive_under_test"
    archive.mkdir()
    for alpha_id in with_manifest:
        d = archive / alpha_id
        d.mkdir()
        (d / "manifest.yaml").write_text(
            f"alpha_id: {alpha_id}\nformula: x\nnotes: synthetic test fixture\n",
            encoding="utf-8",
        )
    for alpha_id in without:
        d = archive / alpha_id
        d.mkdir()
        (d / "README.md").write_text("placeholder", encoding="utf-8")
    return archive


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    script: Any,
    *,
    archive_dir: Path,
    summary_path: Path,
    apply: bool,
) -> int:
    argv = [
        "migrate_alpha_manifests",
        "--archive-dir",
        str(archive_dir),
        "--summary-path",
        str(summary_path),
    ]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    return script.main()


def test_dry_run_writes_nothing(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolated_jsonl: Path,
) -> None:
    archive = _make_archive(tmp_path, with_manifest=["a"], without=["b"])
    summary = tmp_path / "summary.jsonl"
    rc = _run_main(monkeypatch, script, archive_dir=archive, summary_path=summary, apply=False)
    assert rc == 0
    assert not summary.exists(), "dry run must not write summary"
    assert not _isolated_jsonl.exists(), "dry run must not write ledger"


def test_apply_inserts_ledger_rows(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolated_jsonl: Path,
) -> None:
    archive = _make_archive(tmp_path, with_manifest=["alpha_with"], without=["alpha_without"])
    summary = tmp_path / "summary.jsonl"
    rc = _run_main(monkeypatch, script, archive_dir=archive, summary_path=summary, apply=True)
    assert rc == 0

    assert _isolated_jsonl.exists()
    ledger_lines = [
        json.loads(line) for line in _isolated_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(ledger_lines) == 1
    assert ledger_lines[0]["alpha_id"] == "alpha_with"
    assert ledger_lines[0]["gate"] == "manual"
    assert ledger_lines[0]["killed_by"] == "migration:archive_2026_04_17"
    assert ledger_lines[0]["killed_at"] == script.KILLED_AT_NS
    # "notes: synthetic test fixture" picked up by _derive_reason.
    assert "synthetic test fixture" in ledger_lines[0]["reason"]
    assert ledger_lines[0]["stable_artifact_hash"]  # non-empty hex

    assert summary.exists()
    summary_lines = [json.loads(line) for line in summary.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(summary_lines) == 1
    assert summary_lines[0] == {
        "alpha_id": "alpha_without",
        "killed_at_iso": "2026-04-17T00:00:00Z",
        "reason": "archived_2026_04_17_no_manifest",
    }


def test_re_run_is_idempotent(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolated_jsonl: Path,
) -> None:
    archive = _make_archive(tmp_path, with_manifest=["alpha_with"], without=["alpha_without"])
    summary = tmp_path / "summary.jsonl"

    rc1 = _run_main(monkeypatch, script, archive_dir=archive, summary_path=summary, apply=True)
    assert rc1 == 0
    ledger_after_first = _isolated_jsonl.read_text(encoding="utf-8")
    summary_after_first = summary.read_text(encoding="utf-8")

    # Reset the kill_ledger in-memory cache so the second run must re-read disk.
    kill_ledger._reset_cache_for_tests()

    rc2 = _run_main(monkeypatch, script, archive_dir=archive, summary_path=summary, apply=True)
    assert rc2 == 0
    assert _isolated_jsonl.read_text(encoding="utf-8") == ledger_after_first
    assert summary.read_text(encoding="utf-8") == summary_after_first


def test_corrupt_manifest_falls_back_to_default_reason(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolated_jsonl: Path,
) -> None:
    archive = tmp_path / "archive_under_test"
    archive.mkdir()
    bad = archive / "bad_alpha"
    bad.mkdir()
    # Malformed YAML (unclosed bracket).
    (bad / "manifest.yaml").write_text("alpha_id: [oops\n", encoding="utf-8")
    summary = tmp_path / "summary.jsonl"

    rc = _run_main(monkeypatch, script, archive_dir=archive, summary_path=summary, apply=True)
    assert rc == 0
    ledger_lines = [
        json.loads(line) for line in _isolated_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(ledger_lines) == 1
    assert ledger_lines[0]["alpha_id"] == "bad_alpha"
    assert ledger_lines[0]["reason"] == "archived_2026_04_17"
    # Hash is empty when parse failed.
    assert ledger_lines[0]["stable_artifact_hash"] == ""


def test_summary_dedupes_on_alpha_id(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolated_jsonl: Path,
) -> None:
    summary = tmp_path / "summary.jsonl"
    summary.write_text(
        json.dumps(
            {
                "alpha_id": "alpha_x",
                "killed_at_iso": "2026-04-17T00:00:00Z",
                "reason": "preexisting",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    archive = _make_archive(tmp_path, with_manifest=[], without=["alpha_x", "alpha_y"])

    rc = _run_main(monkeypatch, script, archive_dir=archive, summary_path=summary, apply=True)
    assert rc == 0
    rows = [json.loads(line) for line in summary.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 2
    by_alpha = {r["alpha_id"]: r for r in rows}
    # Pre-existing row preserved verbatim.
    assert by_alpha["alpha_x"]["reason"] == "preexisting"
    # New row for alpha_y has the migration reason.
    assert by_alpha["alpha_y"]["reason"] == "archived_2026_04_17_no_manifest"


def test_real_archive_corpus_counts_match_expected(script: Any) -> None:
    """DoD-D2 live verification: scan the real archive in dry-run mode.

    Asserts 25 ledger rows + 21 summary rows from the actual corpus.
    No --apply: ``_scan`` is read-only.
    """
    real_archive = REPO_ROOT / "research" / "archive" / "alphas_2026-04-17"
    if not real_archive.exists():
        pytest.skip(f"real archive not present: {real_archive}")

    ledger_rows, summary_rows = script._scan(real_archive)
    assert len(ledger_rows) == 25, f"expected 25 ledger rows from real archive, got {len(ledger_rows)}"
    assert len(summary_rows) == 21, f"expected 21 summary rows from real archive, got {len(summary_rows)}"
    for row in ledger_rows:
        assert row["gate"] == "manual"
        assert row["killed_by"] == "migration:archive_2026_04_17"
        assert row["killed_at"] == script.KILLED_AT_NS
        assert isinstance(row["alpha_id"], str) and row["alpha_id"]
    for row in summary_rows:
        assert row["killed_at_iso"] == "2026-04-17T00:00:00Z"
        assert row["reason"] == "archived_2026_04_17_no_manifest"
        assert isinstance(row["alpha_id"], str) and row["alpha_id"]


def test_archive_dir_missing_returns_error(
    script: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "does_not_exist"
    summary = tmp_path / "summary.jsonl"
    rc = _run_main(monkeypatch, script, archive_dir=missing, summary_path=summary, apply=False)
    assert rc == 1
    captured = capsys.readouterr()
    assert "Archive dir not found" in captured.err
