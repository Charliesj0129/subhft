from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from structlog import get_logger

from research.tools.fetch_paper import _load_index
from research.tools.paper_autofill import infer_spec_from_text, suggest_alpha_id

logger = get_logger("research.hypothesis_queue")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUEUE_PATH = PROJECT_ROOT / "research" / "knowledge" / "hypothesis_queue.json"


@dataclass
class Hypothesis:
    paper_ref: str
    arxiv_id: str
    title: str
    hypothesis: str
    formula: str
    data_fields: tuple[str, ...]
    suggested_alpha_id: str
    composite_score: float = 0.0
    status: str = "pending"  # pending | scaffolded | rejected
    reject_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["data_fields"] = list(d["data_fields"])
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Hypothesis:
        d = dict(d)
        d["data_fields"] = tuple(d.get("data_fields", ()))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _compute_composite_score(
    spec_data_fields: tuple[str, ...],
    paper_status: str,
    has_alphas: bool,
) -> float:
    """Compute a simple composite score for hypothesis ranking.

    Scoring heuristic:
    - More data fields = more concrete hypothesis (+0.1 per field, cap 1.0)
    - Paper reviewed status bonus (+0.5)
    - No existing alphas = fresh opportunity (+0.3)
    """
    field_score = min(1.0, len(spec_data_fields) * 0.1)
    status_score = 0.5 if paper_status in ("reviewed", "annotated") else 0.0
    novelty_score = 0.3 if not has_alphas else 0.0
    return round(field_score + status_score + novelty_score, 3)


class HypothesisQueue:
    """Manages a ranked queue of research hypotheses extracted from papers."""

    __slots__ = ("_queue_path", "_hypotheses")

    def __init__(self, queue_path: str | Path | None = None) -> None:
        self._queue_path = Path(queue_path) if queue_path else DEFAULT_QUEUE_PATH
        self._hypotheses: list[Hypothesis] = []
        self._load()

    def _load(self) -> None:
        if not self._queue_path.exists():
            self._hypotheses = []
            return
        try:
            data = json.loads(self._queue_path.read_text(encoding="utf-8"))
            self._hypotheses = [
                Hypothesis.from_dict(h) for h in data.get("hypotheses", [])
            ]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("hypothesis_queue.load_failed", error=str(exc))
            self._hypotheses = []

    def _save(self) -> None:
        self._queue_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "hypotheses": [h.to_dict() for h in self._hypotheses],
            "count": len(self._hypotheses),
        }
        self._queue_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )

    def ingest_from_index(
        self,
        paper_index_path: str | Path | None = None,
    ) -> int:
        """Scan papers in paper_index, extract hypotheses, add new ones.

        Returns number of new hypotheses added.
        """
        if paper_index_path:
            try:
                index = json.loads(
                    Path(paper_index_path).read_text(encoding="utf-8"),
                )
            except (json.JSONDecodeError, OSError):
                index = {}
        else:
            index = _load_index()

        existing_refs = {h.paper_ref for h in self._hypotheses}
        added = 0

        for ref, paper in index.items():
            if not isinstance(paper, dict):
                continue
            if ref in existing_refs:
                continue

            title = str(paper.get("title", ""))
            arxiv_id = str(paper.get("arxiv_id", ""))
            alphas = paper.get("alphas", [])
            status = str(paper.get("status", "indexed"))

            spec = infer_spec_from_text(
                title,
                arxiv_ids=(arxiv_id,) if arxiv_id else (),
            )

            score = _compute_composite_score(
                spec.data_fields,
                status,
                bool(alphas),
            )

            hyp = Hypothesis(
                paper_ref=str(ref),
                arxiv_id=arxiv_id,
                title=title,
                hypothesis=spec.hypothesis,
                formula=spec.formula,
                data_fields=spec.data_fields,
                suggested_alpha_id=suggest_alpha_id(title),
                composite_score=score,
                status="pending",
            )
            self._hypotheses.append(hyp)
            added += 1

        if added > 0:
            self._save()
            logger.info(
                "hypothesis_queue.ingested",
                added=added,
                total=len(self._hypotheses),
            )

        return added

    def rank(self) -> list[Hypothesis]:
        """Return all hypotheses sorted by composite score (descending)."""
        return sorted(
            self._hypotheses,
            key=lambda h: h.composite_score,
            reverse=True,
        )

    def top(self, n: int = 5) -> list[Hypothesis]:
        """Return top-N pending hypotheses by score."""
        pending = [h for h in self.rank() if h.status == "pending"]
        return pending[:n]

    def mark_scaffolded(self, ref: str) -> bool:
        """Mark hypothesis as scaffolded. Returns True if found."""
        for h in self._hypotheses:
            if h.paper_ref == ref:
                h.status = "scaffolded"
                self._save()
                return True
        return False

    def mark_rejected(self, ref: str, reason: str = "") -> bool:
        """Mark hypothesis as rejected. Returns True if found."""
        for h in self._hypotheses:
            if h.paper_ref == ref:
                h.status = "rejected"
                h.reject_reason = str(reason)
                self._save()
                return True
        return False

    def all_hypotheses(self) -> list[Hypothesis]:
        """Return all hypotheses (unordered)."""
        return list(self._hypotheses)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Hypothesis queue management.")
    sub = parser.add_subparsers(dest="action")

    ingest = sub.add_parser("ingest", help="Ingest hypotheses from paper index")
    ingest.add_argument("--paper-index", help="Override paper_index.json path")
    ingest.add_argument("--queue", help="Override hypothesis_queue.json path")

    list_cmd = sub.add_parser("list", help="List all hypotheses")
    list_cmd.add_argument("--queue", help="Override hypothesis_queue.json path")
    list_cmd.add_argument(
        "--status",
        help="Filter by status (pending/scaffolded/rejected)",
    )

    top_cmd = sub.add_parser("top", help="Show top-N pending hypotheses")
    top_cmd.add_argument(
        "-n",
        type=int,
        default=5,
        help="Number of top hypotheses",
    )
    top_cmd.add_argument("--queue", help="Override hypothesis_queue.json path")

    args = parser.parse_args()

    if args.action == "ingest":
        queue = HypothesisQueue(queue_path=getattr(args, "queue", None))
        added = queue.ingest_from_index(
            paper_index_path=getattr(args, "paper_index", None),
        )
        print(json.dumps({"added": added, "total": len(queue.all_hypotheses())}))  # noqa: T201
        return 0

    if args.action == "list":
        queue = HypothesisQueue(queue_path=getattr(args, "queue", None))
        status_filter = getattr(args, "status", None)
        hypotheses = queue.rank()
        if status_filter:
            hypotheses = [h for h in hypotheses if h.status == status_filter]
        payload = [h.to_dict() for h in hypotheses]
        print(json.dumps(payload, indent=2))  # noqa: T201
        return 0

    if args.action == "top":
        queue = HypothesisQueue(queue_path=getattr(args, "queue", None))
        top_n = queue.top(n=args.n)
        payload = [h.to_dict() for h in top_n]
        print(json.dumps(payload, indent=2))  # noqa: T201
        return 0

    parser.print_help()
    return 1
