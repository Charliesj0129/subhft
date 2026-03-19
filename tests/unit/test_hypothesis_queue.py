from __future__ import annotations

import json
from pathlib import Path

from research.tools.hypothesis_queue import (
    Hypothesis,
    HypothesisQueue,
    _compute_composite_score,
)


class TestCompositeScore:
    def test_reviewed_paper_scores_higher(self) -> None:
        s1 = _compute_composite_score(("a", "b", "c"), "reviewed", False)
        s2 = _compute_composite_score(("a", "b", "c"), "indexed", False)
        assert s1 > s2

    def test_no_existing_alphas_bonus(self) -> None:
        s1 = _compute_composite_score(("a",), "indexed", False)
        s2 = _compute_composite_score(("a",), "indexed", True)
        assert s1 > s2

    def test_more_fields_scores_higher(self) -> None:
        s1 = _compute_composite_score(("a", "b", "c", "d", "e"), "indexed", False)
        s2 = _compute_composite_score(("a",), "indexed", False)
        assert s1 > s2

    def test_field_score_capped(self) -> None:
        s1 = _compute_composite_score(
            tuple(f"f{i}" for i in range(20)),
            "indexed",
            True,
        )
        # field_score maxes at 1.0
        assert s1 <= 2.0


class TestHypothesis:
    def test_round_trip(self) -> None:
        h = Hypothesis(
            paper_ref="120",
            arxiv_id="2408.03594",
            title="Test Paper",
            hypothesis="Test hypothesis",
            formula="alpha_t = x",
            data_fields=("a", "b"),
            suggested_alpha_id="test_paper",
            composite_score=0.5,
        )
        d = h.to_dict()
        h2 = Hypothesis.from_dict(d)
        assert h2.paper_ref == h.paper_ref
        assert h2.data_fields == h.data_fields
        assert h2.composite_score == h.composite_score


class TestHypothesisQueue:
    def test_ingest_from_index(self, tmp_path: Path) -> None:
        index = {
            "120": {
                "ref": "120",
                "arxiv_id": "2408.03594",
                "title": "Order Flow Imbalance Paper",
                "status": "reviewed",
                "alphas": [],
                "tags": [],
            },
            "121": {
                "ref": "121",
                "arxiv_id": "2409.00001",
                "title": "Market Making Strategy",
                "status": "indexed",
                "alphas": ["mm_v1"],
                "tags": [],
            },
        }
        index_path = tmp_path / "paper_index.json"
        index_path.write_text(json.dumps(index))
        queue_path = tmp_path / "hypothesis_queue.json"

        queue = HypothesisQueue(queue_path=queue_path)
        added = queue.ingest_from_index(paper_index_path=index_path)
        assert added == 2
        assert len(queue.all_hypotheses()) == 2

    def test_dedup_on_reingest(self, tmp_path: Path) -> None:
        index = {
            "120": {
                "ref": "120",
                "arxiv_id": "2408.03594",
                "title": "OFI Paper",
                "status": "reviewed",
                "alphas": [],
                "tags": [],
            },
        }
        index_path = tmp_path / "paper_index.json"
        index_path.write_text(json.dumps(index))
        queue_path = tmp_path / "hypothesis_queue.json"

        queue = HypothesisQueue(queue_path=queue_path)
        queue.ingest_from_index(paper_index_path=index_path)
        queue.ingest_from_index(paper_index_path=index_path)
        assert len(queue.all_hypotheses()) == 1

    def test_top_returns_pending_only(self, tmp_path: Path) -> None:
        queue_path = tmp_path / "hypothesis_queue.json"
        queue = HypothesisQueue(queue_path=queue_path)

        # Manually add hypotheses
        queue._hypotheses = [
            Hypothesis(
                paper_ref="1",
                arxiv_id="a",
                title="A",
                hypothesis="h",
                formula="f",
                data_fields=("x",),
                suggested_alpha_id="a",
                composite_score=0.9,
                status="scaffolded",
            ),
            Hypothesis(
                paper_ref="2",
                arxiv_id="b",
                title="B",
                hypothesis="h",
                formula="f",
                data_fields=("x",),
                suggested_alpha_id="b",
                composite_score=0.8,
                status="pending",
            ),
            Hypothesis(
                paper_ref="3",
                arxiv_id="c",
                title="C",
                hypothesis="h",
                formula="f",
                data_fields=("x",),
                suggested_alpha_id="c",
                composite_score=0.7,
                status="pending",
            ),
        ]

        top = queue.top(n=5)
        assert len(top) == 2
        assert all(h.status == "pending" for h in top)

    def test_mark_scaffolded(self, tmp_path: Path) -> None:
        queue_path = tmp_path / "hypothesis_queue.json"
        queue = HypothesisQueue(queue_path=queue_path)
        queue._hypotheses = [
            Hypothesis(
                paper_ref="120",
                arxiv_id="a",
                title="A",
                hypothesis="h",
                formula="f",
                data_fields=("x",),
                suggested_alpha_id="a",
                composite_score=0.9,
            ),
        ]
        assert queue.mark_scaffolded("120")
        assert queue._hypotheses[0].status == "scaffolded"

    def test_mark_rejected(self, tmp_path: Path) -> None:
        queue_path = tmp_path / "hypothesis_queue.json"
        queue = HypothesisQueue(queue_path=queue_path)
        queue._hypotheses = [
            Hypothesis(
                paper_ref="120",
                arxiv_id="a",
                title="A",
                hypothesis="h",
                formula="f",
                data_fields=("x",),
                suggested_alpha_id="a",
                composite_score=0.9,
            ),
        ]
        assert queue.mark_rejected("120", "low IC")
        assert queue._hypotheses[0].status == "rejected"
        assert queue._hypotheses[0].reject_reason == "low IC"

    def test_mark_nonexistent_returns_false(self, tmp_path: Path) -> None:
        queue_path = tmp_path / "hypothesis_queue.json"
        queue = HypothesisQueue(queue_path=queue_path)
        assert not queue.mark_scaffolded("999")

    def test_rank_order(self, tmp_path: Path) -> None:
        queue_path = tmp_path / "hypothesis_queue.json"
        queue = HypothesisQueue(queue_path=queue_path)
        queue._hypotheses = [
            Hypothesis(
                paper_ref="1",
                arxiv_id="a",
                title="A",
                hypothesis="h",
                formula="f",
                data_fields=("x",),
                suggested_alpha_id="a",
                composite_score=0.3,
            ),
            Hypothesis(
                paper_ref="2",
                arxiv_id="b",
                title="B",
                hypothesis="h",
                formula="f",
                data_fields=("x",),
                suggested_alpha_id="b",
                composite_score=0.9,
            ),
        ]
        ranked = queue.rank()
        assert ranked[0].composite_score > ranked[1].composite_score

    def test_persistence(self, tmp_path: Path) -> None:
        index = {
            "120": {
                "ref": "120",
                "arxiv_id": "2408.03594",
                "title": "OFI Paper",
                "status": "reviewed",
                "alphas": [],
                "tags": [],
            },
        }
        index_path = tmp_path / "paper_index.json"
        index_path.write_text(json.dumps(index))
        queue_path = tmp_path / "hypothesis_queue.json"

        queue = HypothesisQueue(queue_path=queue_path)
        queue.ingest_from_index(paper_index_path=index_path)

        # Reload from disk
        queue2 = HypothesisQueue(queue_path=queue_path)
        assert len(queue2.all_hypotheses()) == 1
        assert queue2.all_hypotheses()[0].paper_ref == "120"
