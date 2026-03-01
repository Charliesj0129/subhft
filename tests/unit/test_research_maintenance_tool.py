from __future__ import annotations

import argparse
import json
from pathlib import Path

import research.tools.maintenance as maintenance


def test_audit_and_backfill_note_citations(tmp_path: Path) -> None:
    root = tmp_path
    note_dir = root / "research" / "knowledge" / "notes"
    note_dir.mkdir(parents=True, exist_ok=True)
    note_path = note_dir / "001_sample.md"
    note_path.write_text(
        "\n".join(
            [
                "# Sample Note",
                "",
                "• **作者**： Alice, Bob",
                "• **年份**： 2024",
                "ArXiv:2408.03594v1",
            ]
        ),
        encoding="utf-8",
    )
    index_path = root / "research" / "knowledge" / "paper_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "001": {
                    "ref": "001",
                    "title": "sample",
                    "note_file": "research/knowledge/notes/001_sample.md",
                    "status": "reviewed",
                    "alphas": [],
                    "tags": [],
                }
            }
        ),
        encoding="utf-8",
    )

    rc = maintenance.cmd_audit_note_citations(
        argparse.Namespace(
            index=str(index_path),
            project_root=str(root),
            max_examples=10,
            out=None,
        )
    )
    assert rc == 0

    rc = maintenance.cmd_backfill_note_citations(
        argparse.Namespace(
            index=str(index_path),
            project_root=str(root),
            limit=0,
            dry_run=False,
            out=None,
        )
    )
    assert rc == 0
    updated = note_path.read_text(encoding="utf-8")
    assert "ref: 001" in updated
    assert "arxiv: https://arxiv.org/abs/2408.03594v1" in updated
    assert "Authors: Alice, Bob" in updated
    assert "Published: 2024" in updated
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["001"]["arxiv_id"] == "2408.03594v1"


def test_triage_pyspy_parses_svg_titles(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    svg = reports / "pyspy_demo.svg"
    svg.write_text(
        (
            "<svg><title>all (100 samples, 100%)</title>"
            "<title>process_event (lob_engine.py:171) (40 samples, 40.00%)</title>"
            "<title>labels (prometheus_client/metrics.py:176) (20 samples, 20.00%)</title></svg>"
        ),
        encoding="utf-8",
    )

    out = tmp_path / "triage.json"
    rc = maintenance.cmd_triage_pyspy(
        argparse.Namespace(
            root_reports=str(reports),
            pattern="pyspy*.svg",
            top=5,
            out=str(out),
            md=None,
        )
    )
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["scanned_svg_files"] == 1
    top_frames = payload["aggregate_top_frames"]
    assert top_frames
    assert "lob_engine.py" in top_frames[0]["frame"]
