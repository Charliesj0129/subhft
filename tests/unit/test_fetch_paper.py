from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import research.tools.fetch_paper as fetch_paper


def _entry_xml(arxiv_id: str, title: str, abstract: str, *, author: str = "Alice") -> str:
    return f"""
<entry>
  <id>http://arxiv.org/abs/{arxiv_id}</id>
  <updated>2025-01-01T00:00:00Z</updated>
  <published>2024-12-31T00:00:00Z</published>
  <title>{title}</title>
  <summary>{abstract}</summary>
  <author><name>{author}</name></author>
</entry>
""".strip()


def _feed_xml(entries: list[str]) -> str:
    body = "\n".join(entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  {body}
</feed>
"""


class _MockResp:
    def __init__(self, payload: str) -> None:
        self._payload = payload.encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_MockResp":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _patch_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path
    knowledge = project_root / "research" / "knowledge"
    index = knowledge / "paper_index.json"
    notes = knowledge / "notes"
    monkeypatch.setattr(fetch_paper, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(fetch_paper, "KNOWLEDGE_DIR", knowledge)
    monkeypatch.setattr(fetch_paper, "PAPER_INDEX", index)
    monkeypatch.setattr(fetch_paper, "NOTES_DIR", notes)
    return index, notes


def test_fetch_paper_creates_index_entry(monkeypatch, tmp_path: Path) -> None:
    index, _ = _patch_paths(monkeypatch, tmp_path)
    xml = _feed_xml([_entry_xml("2408.03594", "Order Flow Imbalance", "A useful abstract.")])
    monkeypatch.setattr(fetch_paper.urllib.request, "urlopen", lambda *a, **k: _MockResp(xml))

    rc = fetch_paper.cmd_fetch_paper(argparse.Namespace(arxiv_id="2408.03594"))
    assert rc == 0

    payload = json.loads(index.read_text(encoding="utf-8"))
    assert len(payload) == 1
    ref, row = next(iter(payload.items()))
    assert ref == row["ref"]
    assert row["arxiv_id"] == "2408.03594"


def test_fetch_paper_creates_note_file(monkeypatch, tmp_path: Path) -> None:
    _, notes = _patch_paths(monkeypatch, tmp_path)
    xml = _feed_xml([_entry_xml("2501.00001", "Latency-Aware Alpha", "Abstract body text here.")])
    monkeypatch.setattr(fetch_paper.urllib.request, "urlopen", lambda *a, **k: _MockResp(xml))

    rc = fetch_paper.cmd_fetch_paper(argparse.Namespace(arxiv_id="2501.00001"))
    assert rc == 0

    note_files = list(notes.glob("*.md"))
    assert len(note_files) == 1
    content = note_files[0].read_text(encoding="utf-8")
    assert "Latency-Aware Alpha" in content
    assert "Abstract body text here." in content
    assert "Hypothesis (TODO)" not in content
    assert "TODO: map to data_fields" not in content
    assert "Candidate Formula" in content
    assert "Relevant Features (lob_shared_v1)" in content


def test_fetch_paper_dedup(monkeypatch, tmp_path: Path) -> None:
    index, _ = _patch_paths(monkeypatch, tmp_path)
    xml = _feed_xml([_entry_xml("2502.00002", "Duplicate Test", "Same paper.")])
    monkeypatch.setattr(fetch_paper.urllib.request, "urlopen", lambda *a, **k: _MockResp(xml))

    rc1 = fetch_paper.cmd_fetch_paper(argparse.Namespace(arxiv_id="2502.00002"))
    rc2 = fetch_paper.cmd_fetch_paper(argparse.Namespace(arxiv_id="2502.00002"))
    assert rc1 == 0
    assert rc2 == 0

    payload = json.loads(index.read_text(encoding="utf-8"))
    assert len(payload) == 1


def test_fetch_paper_dedup_normalizes_arxiv_versions(monkeypatch, tmp_path: Path) -> None:
    index, _ = _patch_paths(monkeypatch, tmp_path)
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        json.dumps(
            {
                "120": {
                    "ref": "120",
                    "arxiv_id": "2408.03594",
                    "title": "Forecasting High Frequency Order Flow Imbalance",
                    "note_file": "research/knowledge/notes/120_dummy.md",
                    "status": "reviewed",
                    "alphas": [],
                    "tags": [],
                }
            }
        ),
        encoding="utf-8",
    )
    xml = _feed_xml([_entry_xml("2408.03594v2", "Forecasting High Frequency Order Flow Imbalance", "Update.")])
    monkeypatch.setattr(fetch_paper.urllib.request, "urlopen", lambda *a, **k: _MockResp(xml))

    rc = fetch_paper.cmd_fetch_paper(argparse.Namespace(arxiv_id="2408.03594v2"))
    assert rc == 0
    payload = json.loads(index.read_text(encoding="utf-8"))
    assert len(payload) == 1


def test_fetch_paper_dedup_by_title_when_arxiv_missing(monkeypatch, tmp_path: Path) -> None:
    index, _ = _patch_paths(monkeypatch, tmp_path)
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        json.dumps(
            {
                "018": {
                    "ref": "018",
                    "title": "Forecasting High Frequency Order Flow Imbalance",
                    "note_file": "research/knowledge/notes/018_dummy.md",
                    "status": "implemented",
                    "alphas": ["ofi_mc"],
                    "tags": ["microstructure"],
                }
            }
        ),
        encoding="utf-8",
    )
    xml = _feed_xml([_entry_xml("2408.03594v1", "Forecasting High Frequency Order Flow Imbalance", "Abstract.")])
    monkeypatch.setattr(fetch_paper.urllib.request, "urlopen", lambda *a, **k: _MockResp(xml))

    rc = fetch_paper.cmd_fetch_paper(argparse.Namespace(arxiv_id="2408.03594"))
    assert rc == 0
    payload = json.loads(index.read_text(encoding="utf-8"))
    assert len(payload) == 1
    assert payload["018"]["arxiv_id"] == "2408.03594v1"


def test_search_papers_prints_results(monkeypatch, capsys) -> None:
    xml = _feed_xml(
        [
            _entry_xml("2503.00003", "Paper A", "abstract a"),
            _entry_xml("2503.00004", "Paper B", "abstract b"),
        ]
    )
    monkeypatch.setattr(fetch_paper.urllib.request, "urlopen", lambda *a, **k: _MockResp(xml))

    rc = fetch_paper.cmd_search_papers(argparse.Namespace(query="order flow", max=5))
    out = capsys.readouterr().out
    assert rc == 0
    assert "2503.00003" in out
    assert "Paper B" in out


def test_slug_normalization() -> None:
    slug = fetch_paper._slug("A/B\\C: OFI + Latency? *Test*")
    assert slug
    assert re.fullmatch(r"[a-z0-9_]+", slug) is not None
