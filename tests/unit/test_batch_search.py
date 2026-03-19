from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest

import research.tools.fetch_paper as _fp


class TestBatchArxivSearcher:
    def _make_xml_response(self, entries: list[dict]) -> ET.Element:
        """Build a minimal Atom XML response."""
        ns = "http://www.w3.org/2005/Atom"
        root = ET.Element(f"{{{ns}}}feed")
        for entry_data in entries:
            entry = ET.SubElement(root, f"{{{ns}}}entry")
            id_el = ET.SubElement(entry, f"{{{ns}}}id")
            id_el.text = f"http://arxiv.org/abs/{entry_data['arxiv_id']}"
            title_el = ET.SubElement(entry, f"{{{ns}}}title")
            title_el.text = entry_data.get("title", "Test Paper")
            summary_el = ET.SubElement(entry, f"{{{ns}}}summary")
            summary_el.text = entry_data.get("abstract", "Test abstract")
            pub_el = ET.SubElement(entry, f"{{{ns}}}published")
            pub_el.text = "2026-01-01T00:00:00Z"
            upd_el = ET.SubElement(entry, f"{{{ns}}}updated")
            upd_el.text = "2026-01-01T00:00:00Z"
            auth = ET.SubElement(entry, f"{{{ns}}}author")
            name = ET.SubElement(auth, f"{{{ns}}}name")
            name.text = "Test Author"
        return root

    def test_dedup_across_queries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same paper in two queries should only appear once."""
        monkeypatch.setattr(_fp, "PAPER_INDEX", tmp_path / "paper_index.json")
        monkeypatch.setattr(_fp, "NOTES_DIR", tmp_path / "notes")
        monkeypatch.setattr(_fp, "KNOWLEDGE_DIR", tmp_path)

        xml_resp = self._make_xml_response(
            [
                {"arxiv_id": "2408.03594", "title": "OFI Paper"},
            ]
        )

        from research.tools.batch_search import BatchArxivSearcher

        with patch.object(_fp, "_fetch_xml", return_value=xml_resp):
            searcher = BatchArxivSearcher(rate_limit_s=0.0, max_per_query=10)
            results = searcher.search_multi(["query1", "query2"], dry_run=True)

        # Should only have one result even though two queries
        assert len(results) == 1
        assert results[0]["arxiv_id"] == "2408.03594"

    def test_existing_paper_marked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Papers already in index should be marked as existing."""
        index_path = tmp_path / "paper_index.json"
        index_path.write_text(
            json.dumps(
                {
                    "120": {
                        "ref": "120",
                        "arxiv_id": "2408.03594",
                        "title": "OFI Paper",
                        "status": "reviewed",
                        "alphas": [],
                        "tags": [],
                    },
                }
            )
        )
        monkeypatch.setattr(_fp, "PAPER_INDEX", index_path)
        monkeypatch.setattr(_fp, "NOTES_DIR", tmp_path / "notes")
        monkeypatch.setattr(_fp, "KNOWLEDGE_DIR", tmp_path)

        xml_resp = self._make_xml_response(
            [
                {"arxiv_id": "2408.03594", "title": "OFI Paper"},
            ]
        )

        from research.tools.batch_search import BatchArxivSearcher

        with patch.object(_fp, "_fetch_xml", return_value=xml_resp):
            searcher = BatchArxivSearcher(rate_limit_s=0.0, max_per_query=10)
            results = searcher.search_multi(["order flow"], dry_run=False)

        assert len(results) == 1
        assert results[0]["status"] == "existing"
        assert results[0]["ref"] == "120"

    def test_new_paper_added(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """New papers should be added to index."""
        index_path = tmp_path / "paper_index.json"
        index_path.write_text("{}")
        monkeypatch.setattr(_fp, "PAPER_INDEX", index_path)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        monkeypatch.setattr(_fp, "NOTES_DIR", notes_dir)
        monkeypatch.setattr(_fp, "KNOWLEDGE_DIR", tmp_path)

        xml_resp = self._make_xml_response(
            [
                {"arxiv_id": "2501.00001", "title": "New Trading Paper"},
            ]
        )

        from research.tools.batch_search import BatchArxivSearcher

        with patch.object(_fp, "_fetch_xml", return_value=xml_resp):
            searcher = BatchArxivSearcher(rate_limit_s=0.0, max_per_query=10)
            results = searcher.search_multi(["trading"], dry_run=False)

        assert len(results) == 1
        assert results[0]["status"] == "new"
        assert results[0]["ref"] is not None

        # Verify index was saved
        saved_index = json.loads(index_path.read_text())
        assert len(saved_index) == 1

    def test_rate_limiting(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify rate limiting parameter is respected (via mock)."""
        monkeypatch.setattr(_fp, "PAPER_INDEX", tmp_path / "paper_index.json")
        monkeypatch.setattr(_fp, "NOTES_DIR", tmp_path / "notes")
        monkeypatch.setattr(_fp, "KNOWLEDGE_DIR", tmp_path)

        xml_resp = self._make_xml_response([])

        from research.tools.batch_search import BatchArxivSearcher

        with patch.object(_fp, "_fetch_xml", return_value=xml_resp):
            searcher = BatchArxivSearcher(rate_limit_s=0.0, max_per_query=5)
            results = searcher.search_multi(["q1", "q2"], dry_run=True)

        assert results == []

    def test_empty_queries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty query list should return empty results."""
        monkeypatch.setattr(_fp, "PAPER_INDEX", tmp_path / "paper_index.json")
        monkeypatch.setattr(_fp, "NOTES_DIR", tmp_path / "notes")
        monkeypatch.setattr(_fp, "KNOWLEDGE_DIR", tmp_path)

        from research.tools.batch_search import BatchArxivSearcher

        searcher = BatchArxivSearcher(rate_limit_s=0.0)
        results = searcher.search_multi([], dry_run=True)
        assert results == []

    def test_dry_run_does_not_modify_index(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dry run should not write to the index file."""
        index_path = tmp_path / "paper_index.json"
        index_path.write_text("{}")
        monkeypatch.setattr(_fp, "PAPER_INDEX", index_path)
        monkeypatch.setattr(_fp, "NOTES_DIR", tmp_path / "notes")
        monkeypatch.setattr(_fp, "KNOWLEDGE_DIR", tmp_path)

        xml_resp = self._make_xml_response(
            [
                {"arxiv_id": "2501.00001", "title": "Some Paper"},
            ]
        )

        from research.tools.batch_search import BatchArxivSearcher

        with patch.object(_fp, "_fetch_xml", return_value=xml_resp):
            searcher = BatchArxivSearcher(rate_limit_s=0.0, max_per_query=10)
            results = searcher.search_multi(["test"], dry_run=True)

        assert len(results) == 1
        assert results[0]["status"] == "would_add"

        # Index should still be empty
        saved_index = json.loads(index_path.read_text())
        assert len(saved_index) == 0

    def test_fetch_failure_skips_query(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed fetch should skip that query and continue."""
        monkeypatch.setattr(_fp, "PAPER_INDEX", tmp_path / "paper_index.json")
        monkeypatch.setattr(_fp, "NOTES_DIR", tmp_path / "notes")
        monkeypatch.setattr(_fp, "KNOWLEDGE_DIR", tmp_path)

        good_xml = self._make_xml_response(
            [
                {"arxiv_id": "2501.00002", "title": "Good Paper"},
            ]
        )

        call_count = 0

        def _side_effect(url: str) -> ET.Element:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Network error")
            return good_xml

        from research.tools.batch_search import BatchArxivSearcher

        with patch.object(_fp, "_fetch_xml", side_effect=_side_effect):
            searcher = BatchArxivSearcher(rate_limit_s=0.0, max_per_query=10)
            results = searcher.search_multi(["bad_query", "good_query"], dry_run=True)

        assert len(results) == 1
        assert results[0]["arxiv_id"] == "2501.00002"
