"""auto_scaffold.py — Auto-scaffold alpha packages from top hypothesis queue entries.

Reads top-N pending hypotheses from the hypothesis queue, scaffolds each one using
the existing alpha_scaffold infrastructure, and marks them as scaffolded.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from structlog import get_logger

from research.tools.hypothesis_queue import HypothesisQueue

logger = get_logger("research.auto_scaffold")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALPHAS_DIR = PROJECT_ROOT / "research" / "alphas"


class AutoScaffoldPipeline:
    """Scaffold alpha packages from top hypothesis queue entries."""

    __slots__ = ("_queue",)

    def __init__(self, queue: HypothesisQueue) -> None:
        self._queue = queue

    def scaffold_top(
        self,
        n: int = 5,
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Scaffold top-N pending hypotheses.

        Returns list of result dicts with alpha_id, paper_ref, status.
        """
        top_hyps = self._queue.top(n=n)
        results: list[dict[str, Any]] = []

        for hyp in top_hyps:
            alpha_id = hyp.suggested_alpha_id
            if not alpha_id:
                logger.warning(
                    "auto_scaffold.skip_no_id",
                    paper_ref=hyp.paper_ref,
                    title=hyp.title[:60],
                )
                results.append({
                    "alpha_id": None,
                    "paper_ref": hyp.paper_ref,
                    "status": "skipped",
                    "reason": "no suggested alpha_id",
                })
                continue

            alpha_dir = ALPHAS_DIR / alpha_id
            if alpha_dir.exists() and not force:
                logger.info(
                    "auto_scaffold.exists",
                    alpha_id=alpha_id,
                    paper_ref=hyp.paper_ref,
                )
                # Still mark as scaffolded since it exists
                self._queue.mark_scaffolded(hyp.paper_ref)
                results.append({
                    "alpha_id": alpha_id,
                    "paper_ref": hyp.paper_ref,
                    "status": "already_exists",
                })
                continue

            if dry_run:
                results.append({
                    "alpha_id": alpha_id,
                    "paper_ref": hyp.paper_ref,
                    "status": "would_scaffold",
                    "hypothesis": hyp.hypothesis[:100],
                })
                continue

            # Scaffold via subprocess to reuse existing CLI
            cmd = [
                sys.executable,
                "-m",
                "research",
                "scaffold",
                alpha_id,
            ]
            if hyp.paper_ref:
                cmd.extend(["--paper", str(hyp.paper_ref)])
            if force:
                cmd.append("--force")

            try:
                result = subprocess.run(  # noqa: S603
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=str(PROJECT_ROOT),
                )
                if result.returncode == 0:
                    self._queue.mark_scaffolded(hyp.paper_ref)
                    logger.info(
                        "auto_scaffold.success",
                        alpha_id=alpha_id,
                        paper_ref=hyp.paper_ref,
                    )
                    results.append({
                        "alpha_id": alpha_id,
                        "paper_ref": hyp.paper_ref,
                        "status": "scaffolded",
                    })
                else:
                    logger.warning(
                        "auto_scaffold.failed",
                        alpha_id=alpha_id,
                        paper_ref=hyp.paper_ref,
                        stderr=result.stderr[:200],
                    )
                    results.append({
                        "alpha_id": alpha_id,
                        "paper_ref": hyp.paper_ref,
                        "status": "failed",
                        "error": result.stderr[:200],
                    })
            except subprocess.TimeoutExpired:
                logger.warning(
                    "auto_scaffold.timeout",
                    alpha_id=alpha_id,
                    paper_ref=hyp.paper_ref,
                )
                results.append({
                    "alpha_id": alpha_id,
                    "paper_ref": hyp.paper_ref,
                    "status": "timeout",
                })

        return results


def main() -> int:
    """CLI entry point for auto-scaffold pipeline."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Auto-scaffold alpha packages from hypothesis queue.",
    )
    parser.add_argument(
        "-n",
        type=int,
        default=5,
        help="Number of top hypotheses to scaffold",
    )
    parser.add_argument("--queue", help="Override hypothesis_queue.json path")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing alpha directories",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be scaffolded without doing it",
    )
    parser.add_argument("--out", help="Optional JSON output path")
    args = parser.parse_args()

    queue = HypothesisQueue(queue_path=args.queue)
    pipeline = AutoScaffoldPipeline(queue=queue)
    results = pipeline.scaffold_top(
        n=args.n,
        force=args.force,
        dry_run=args.dry_run,
    )

    payload = {"results": results, "count": len(results)}
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))  # noqa: T201
    return 0
