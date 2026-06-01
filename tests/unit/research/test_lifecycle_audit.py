"""Stage-6 (D6) lifecycle audit unit tests.

Drives ``research/tools/lifecycle_audit.py`` against synthetic trees so the
audit's drift codes are exercised without depending on the live repo state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from research.tools import lifecycle_audit


def _write_manifest(path: Path, *, alpha_id: str, status: str = "PROTOTYPE") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"alpha_id": alpha_id, "status": status, "hypothesis": "h",
                        "formula": "f", "paper_refs": [], "data_fields": [], "complexity": "O(1)"}),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _isolated_tree(tmp_path, monkeypatch):
    """Point lifecycle_audit module globals at a temp tree."""
    active = tmp_path / "research" / "alphas"
    archive = tmp_path / "research" / "archive"
    active.mkdir(parents=True)
    archive.mkdir(parents=True)
    monkeypatch.setattr(lifecycle_audit, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lifecycle_audit, "ACTIVE_DIR", active)
    monkeypatch.setattr(lifecycle_audit, "ARCHIVE_ROOT", archive)
    monkeypatch.setattr(lifecycle_audit, "KILL_LEDGER", active / "_kill_ledger.jsonl")
    monkeypatch.setattr(lifecycle_audit, "CLUSTER_ASSIGNMENTS", active / "_cluster_assignments.json")
    monkeypatch.setattr(lifecycle_audit, "PAPER_INDEX", tmp_path / "research" / "knowledge" / "paper_index.json")
    return tmp_path


def test_clean_tree_returns_zero(_isolated_tree, capsys):
    _write_manifest(_isolated_tree / "research/alphas/c100/manifest.yaml", alpha_id="c100")
    _write_manifest(
        _isolated_tree / "research/archive/alphas_2026-05-28/c99/manifest.yaml",
        alpha_id="c99", status="KILLED",
    )
    assert lifecycle_audit.run_audit() == 0
    out = capsys.readouterr().out
    assert "errors=0" in out


def test_terminal_status_in_active_is_error(_isolated_tree, capsys):
    _write_manifest(_isolated_tree / "research/alphas/c200/manifest.yaml",
                    alpha_id="c200", status="KILLED")
    assert lifecycle_audit.run_audit() == 1
    assert "terminal_in_active" in capsys.readouterr().out


def test_killed_in_ledger_but_active_is_error(_isolated_tree, capsys):
    _write_manifest(_isolated_tree / "research/alphas/c300/manifest.yaml",
                    alpha_id="c300", status="PROTOTYPE")
    ledger = _isolated_tree / "research/alphas/_kill_ledger.jsonl"
    ledger.write_text(json.dumps({"alpha_id": "c300", "gate": "A", "reason": "x"}) + "\n",
                      encoding="utf-8")
    assert lifecycle_audit.run_audit() == 1
    assert "killed_but_active" in capsys.readouterr().out


def test_ledger_orphan_is_warn_only(_isolated_tree, capsys):
    ledger = _isolated_tree / "research/alphas/_kill_ledger.jsonl"
    ledger.write_text(json.dumps({"alpha_id": "ghost", "gate": "A", "reason": "x"}) + "\n",
                      encoding="utf-8")
    assert lifecycle_audit.run_audit() == 0  # WARN only
    out = capsys.readouterr().out
    assert "ledger_orphan" in out and "errors=0" in out


def test_active_and_archived_duplicate_is_error(_isolated_tree, capsys):
    _write_manifest(_isolated_tree / "research/alphas/c400/manifest.yaml", alpha_id="c400")
    _write_manifest(
        _isolated_tree / "research/archive/alphas_2026-05-28/c400/manifest.yaml",
        alpha_id="c400", status="KILLED",
    )
    assert lifecycle_audit.run_audit() == 1
    assert "active_and_archived" in capsys.readouterr().out


def test_paper_index_orphan_is_warn(_isolated_tree, capsys):
    _write_manifest(_isolated_tree / "research/alphas/c500/manifest.yaml", alpha_id="c500")
    paper_idx = _isolated_tree / "research/knowledge/paper_index.json"
    paper_idx.parent.mkdir(parents=True, exist_ok=True)
    paper_idx.write_text(json.dumps({"P1": {"alphas": ["c500", "ghost_alpha"]}}),
                         encoding="utf-8")
    assert lifecycle_audit.run_audit() == 0
    out = capsys.readouterr().out
    assert "paper_index_orphan_refs" in out


def test_json_out_writes_report(_isolated_tree, tmp_path):
    _write_manifest(_isolated_tree / "research/alphas/c600/manifest.yaml",
                    alpha_id="c600", status="DEPRECATED")
    out_path = tmp_path / "report.json"
    lifecycle_audit.run_audit(json_out=out_path)
    body = json.loads(out_path.read_text(encoding="utf-8"))
    assert body["manifest_count"] == 1
    assert any(e["code"] == "terminal_in_active" for e in body["errors"])


def test_killed_enum_value_exists():
    from research.registry.schemas import AlphaStatus

    assert AlphaStatus("KILLED") is AlphaStatus.KILLED
