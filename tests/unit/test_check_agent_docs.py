"""Behavior tests for scripts/check_agent_docs.py (agent-docs consistency gate)."""

from __future__ import annotations

from pathlib import Path

from scripts.check_agent_docs import main


def _make_repo(tmp_path: Path) -> Path:
    """Minimal consistent repo: every check passes on this baseline."""
    (tmp_path / ".agent/rules").mkdir(parents=True)
    (tmp_path / ".agent/skills/demo-skill").mkdir(parents=True)
    (tmp_path / ".agent/memory").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "CLAUDE.md").write_text("Read `docs/guide.md` first.\n", encoding="utf-8")
    (tmp_path / "docs/guide.md").write_text("guide\n", encoding="utf-8")
    (tmp_path / ".agent/skills/demo-skill/SKILL.md").write_text("# demo\n", encoding="utf-8")
    (tmp_path / ".agent/skills/00-index.md").write_text(
        "| Skill | When |\n|---|---|\n| `demo-skill` | demo |\n", encoding="utf-8"
    )
    (tmp_path / ".agent/memory/README.md").write_text(
        "| File | Purpose |\n|---|---|\n| `notes.md` | notes |\n", encoding="utf-8"
    )
    (tmp_path / ".agent/memory/notes.md").write_text("notes\n", encoding="utf-8")
    return tmp_path


def _run(root: Path) -> int:
    return main(["--root", str(root)])


def test_consistent_repo_passes(tmp_path: Path) -> None:
    assert _run(_make_repo(tmp_path)) == 0


def test_missing_referenced_path_fails_gate(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / "CLAUDE.md").write_text("See `docs/gone.md` for details.\n", encoding="utf-8")

    assert _run(root) == 1
    assert "`docs/gone.md` does not exist" in capsys.readouterr().out


def test_accepts_existing_path_referenced_with_line_number_suffix(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    (root / "CLAUDE.md").write_text("Fixed at `docs/guide.md:7` earlier.\n", encoding="utf-8")

    assert _run(root) == 0


def test_globs_placeholders_and_code_tokens_are_not_path_claims(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    (root / "CLAUDE.md").write_text(
        "Never mutate `research/experiments/**`; config in `config/<broker>.yaml`;\n"
        "avoid `datetime.now()`; flow is `OrderIntent -> RiskDecision`.\n",
        encoding="utf-8",
    )

    assert _run(root) == 0


def test_skill_directory_missing_from_index_fails(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / ".agent/skills/orphan-skill").mkdir()
    (root / ".agent/skills/orphan-skill/SKILL.md").write_text("# orphan\n", encoding="utf-8")

    assert _run(root) == 1
    assert "directory `orphan-skill` has no row" in capsys.readouterr().out


def test_index_row_without_skill_directory_fails(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / ".agent/skills/00-index.md").write_text(
        "| Skill | When |\n|---|---|\n| `demo-skill` | demo |\n| `ghost-skill` | gone |\n",
        encoding="utf-8",
    )

    assert _run(root) == 1
    assert "row `ghost-skill` has no" in capsys.readouterr().out


def test_memory_file_not_in_readme_table_fails(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / ".agent/memory/stray.md").write_text("stray\n", encoding="utf-8")

    assert _run(root) == 1
    assert "file `stray.md` is not in the README routing table" in capsys.readouterr().out


def test_readme_listing_missing_memory_file_fails(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / ".agent/memory/README.md").write_text(
        "| File | Purpose |\n|---|---|\n| `notes.md` | notes |\n| `phantom.md` | gone |\n",
        encoding="utf-8",
    )

    assert _run(root) == 1
    assert "lists `phantom.md` which does not exist" in capsys.readouterr().out


def test_known_drift_baseline_tolerates_finding(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / "CLAUDE.md").write_text("See `docs/gone.md`.\n", encoding="utf-8")
    (root / ".agent/agent-docs-known-drift.txt").write_text("# tolerated\npath docs/gone.md\n", encoding="utf-8")

    assert _run(root) == 0
    out = capsys.readouterr().out
    assert "1 tolerated known-drift" in out
    assert "ERROR" not in out


def test_stale_baseline_entry_warns_but_passes(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / ".agent/agent-docs-known-drift.txt").write_text("path docs/long-since-fixed.md\n", encoding="utf-8")

    assert _run(root) == 0
    out = capsys.readouterr().out
    assert "stale known-drift entry" in out
    assert "path docs/long-since-fixed.md" in out
