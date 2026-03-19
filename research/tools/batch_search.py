from __future__ import annotations

import time
import urllib.parse
from pathlib import Path
from typing import Any

from structlog import get_logger

import research.tools.fetch_paper as _fp

logger = get_logger("research.batch_search")

_DEFAULT_RATE_LIMIT_S = 3.0  # arXiv asks for 3s between requests


class BatchArxivSearcher:
    """Search arXiv with multiple queries, dedup, and batch-add to paper_index."""

    __slots__ = ("_rate_limit_s", "_max_per_query")

    def __init__(
        self,
        *,
        rate_limit_s: float = _DEFAULT_RATE_LIMIT_S,
        max_per_query: int = 50,
    ) -> None:
        self._rate_limit_s = float(rate_limit_s)
        self._max_per_query = int(max_per_query)

    def search_multi(
        self,
        queries: list[str],
        *,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Search arXiv with multiple queries, dedup, add to index.

        Returns list of dicts with ref, arxiv_id, title, status (new/existing).
        """
        index = _fp._load_index()
        all_results: list[dict[str, Any]] = []
        seen_arxiv_ids: set[str] = set()

        for qi, query in enumerate(queries):
            query = str(query).strip()
            if not query:
                continue
            logger.info("batch_search.query", query=query)

            encoded = urllib.parse.quote(query)
            url = (
                f"{_fp.ARXIV_API}?search_query=all:{encoded}"
                f"&start=0&max_results={self._max_per_query}"
                f"&sortBy=relevance&sortOrder=descending"
            )

            try:
                root = _fp._fetch_xml(url)
            except Exception as exc:
                logger.warning("batch_search.fetch_failed", query=query, error=str(exc))
                continue

            entries = root.findall("atom:entry", _fp.NS)
            logger.info("batch_search.results", query=query, count=len(entries))

            for entry in entries:
                paper = _fp._parse_entry(entry)
                arxiv_id = str(paper.get("arxiv_id", ""))
                if not arxiv_id or arxiv_id in seen_arxiv_ids:
                    continue
                seen_arxiv_ids.add(arxiv_id)

                existing_ref = _fp._find_existing_paper_ref(index, paper)
                if existing_ref is not None:
                    all_results.append({
                        "ref": existing_ref,
                        "arxiv_id": arxiv_id,
                        "title": paper.get("title", ""),
                        "status": "existing",
                    })
                    continue

                if dry_run:
                    all_results.append({
                        "ref": None,
                        "arxiv_id": arxiv_id,
                        "title": paper.get("title", ""),
                        "status": "would_add",
                    })
                    continue

                # Add to index
                ref = _fp._next_ref(index)
                title = str(paper.get("title", ""))
                index[ref] = {
                    "ref": ref,
                    "arxiv_id": arxiv_id,
                    "title": title,
                    "note_file": str(_fp._note_path(ref, title)),
                    "status": "indexed",
                    "alphas": [],
                    "tags": [],
                }

                # Write note file
                note_file = _fp._note_path(ref, title)
                note_file.parent.mkdir(parents=True, exist_ok=True)
                note_file.write_text(_fp._note_template(paper, ref), encoding="utf-8")

                all_results.append({
                    "ref": ref,
                    "arxiv_id": arxiv_id,
                    "title": title,
                    "status": "new",
                })
                logger.info("batch_search.added", ref=ref, arxiv_id=arxiv_id, title=title[:60])

            # Rate limit between queries
            if qi < len(queries) - 1:
                time.sleep(self._rate_limit_s)

        if not dry_run and all_results:
            _fp._save_index(index)
            logger.info(
                "batch_search.complete",
                total=len(all_results),
                new=sum(1 for r in all_results if r["status"] == "new"),
                existing=sum(1 for r in all_results if r["status"] == "existing"),
            )

        return all_results


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Batch search arXiv and add papers to index.")
    parser.add_argument("queries", nargs="+", help="Search queries")
    parser.add_argument("--max-per-query", type=int, default=50, help="Max results per query")
    parser.add_argument("--rate-limit", type=float, default=3.0, help="Seconds between queries")
    parser.add_argument("--dry-run", action="store_true", help="Show results without modifying index")
    parser.add_argument("--out", help="Optional JSON output path")
    args = parser.parse_args()

    searcher = BatchArxivSearcher(
        rate_limit_s=args.rate_limit,
        max_per_query=args.max_per_query,
    )
    results = searcher.search_multi(args.queries, dry_run=args.dry_run)

    payload = {
        "results": results,
        "total": len(results),
        "new": sum(1 for r in results if r["status"] == "new"),
        "existing": sum(1 for r in results if r["status"] == "existing"),
    }
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))  # noqa: T201
    return 0
