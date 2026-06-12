"""CLI for the alpha candidate loop v1 (offline research tool; print is OK here).

Subcommands (spec §5/§11):

* ``generate`` — wrap a raw model/template JSONL drop with the §11 provenance
  header into ``candidates/<gen_run>/family=<f>.jsonl``.
* ``run``      — run a batch end-to-end (``--resume`` is the same idempotent
  operation; dedupe writers + cached panels make re-runs zero-new-rows).
* ``summarize``— rebuild the §15 failure summary from ClickHouse for a run.
* ``promote``  — print the WATCHLIST/PROMOTED shortlist from a run's
  failure_summary.json.
* ``replay-fallback`` — flush ``runs/<run_id>/_results_fallback.jsonl`` into CH.

Usage::

    uv run python -m research.candidate_loop generate \\
        --run-id smoke_001 --family microprice --count 20 \\
        --prompt research/candidate_loop/prompts/v1/microprice.md \\
        --from-jsonl /tmp/drop.jsonl
    uv run python -m research.candidate_loop run --batch smoke_001
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.candidate_loop.generate import (
    DEFAULT_CANDIDATES_ROOT,
    generate_drop,
)
from research.candidate_loop.runner import DEFAULT_RUNS_ROOT, RunConfig, run_batch

ARGMAX_STATUS_SQL = (
    "SELECT alpha_id, any(family), argMax(status, inserted_at), "
    "argMax(death_reason, inserted_at) "
    "FROM research.alpha_candidates WHERE run_id = %(run_id)s GROUP BY alpha_id"
)


def _ch_client(required: bool) -> Any | None:
    try:
        from hft_platform.infra.ch_client import get_ch_client

        return get_ch_client()
    except Exception as exc:  # noqa: BLE001 - CLI degrades to jsonl fallback
        if required:
            print(f"ERROR: ClickHouse client unavailable: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        print(f"WARNING: ClickHouse unavailable ({exc}); using jsonl fallback", file=sys.stderr)
        return None


def _cmd_generate(args: argparse.Namespace) -> int:
    out = generate_drop(
        gen_run_id=args.run_id,
        family=args.family,
        prompt_path=Path(args.prompt),
        from_jsonl=Path(args.from_jsonl),
        expected_count=args.count,
        generation_model=args.generation_model,
        generated_at=datetime.now(timezone.utc).isoformat(),
        candidates_root=Path(args.candidates_root),
    )
    print(f"wrote {out}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    client = None if args.no_ch else _ch_client(required=False)
    rc = RunConfig.for_run_id(
        args.batch,
        candidates_root=Path(args.candidates_root),
        runs_root=Path(args.runs_root),
    )
    summary = run_batch(rc, client)
    print(json.dumps(summary["totals"], indent=2, sort_keys=True))
    print(f"failure_summary: {rc.run_dir / 'failure_summary.json'}")
    return 0


def _cmd_summarize(args: argparse.Namespace) -> int:
    from research.candidate_loop.evaluator import load_evaluator_config
    from research.candidate_loop.failure_summary import (
        build_failure_summary,
        fetch_result_rows,
        write_failure_summary,
    )
    from research.candidate_loop.scoring import load_scoring_config
    from research.candidate_loop.splits import load_split_definition

    client = _ch_client(required=True)
    assert client is not None  # required=True exits on failure
    rc = RunConfig.for_run_id(args.batch, runs_root=Path(args.runs_root))
    eval_cfg = load_evaluator_config(rc.evaluator_config_path)
    scoring_cfg = load_scoring_config(rc.scoring_config_path)
    split_def = load_split_definition(rc.split_definition_path)

    rows = client.query(ARGMAX_STATUS_SQL, parameters={"run_id": args.batch}).result_rows
    candidate_rows = [
        {"alpha_id": str(r[0]), "family": str(r[1]), "status": str(r[2]), "death_reason": str(r[3])}
        for r in rows
    ]
    result_rows = fetch_result_rows(client, args.batch)
    summary = build_failure_summary(
        run_id=args.batch,
        versions={
            "data_version": split_def.data_version,
            "primitive_version": eval_cfg.primitive_version,
            "evaluator_version": eval_cfg.evaluator_version,
            "scoring_version": scoring_cfg.scoring_version,
            "cost_assumption_version": eval_cfg.cost_assumption_version,
            "latency_config_version": eval_cfg.latency_config_version,
        },
        candidate_rows=candidate_rows,
        result_rows=result_rows,
        scoring_cfg=scoring_cfg,
    )
    path = write_failure_summary(summary, rc.run_dir)
    print(json.dumps(summary["totals"], indent=2, sort_keys=True))
    print(f"wrote {path}")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    path = Path(args.runs_root) / args.batch / "failure_summary.json"
    if not path.exists():
        print(f"ERROR: {path} not found (run the batch first)", file=sys.stderr)
        return 2
    summary = json.loads(path.read_text())
    for status in ("promoted", "watchlist"):
        rows = summary.get(status, [])
        print(f"{status.upper()} ({len(rows)}):")
        for row in rows:
            print(f"  {row['alpha_id']}  {row['family']:<24} final_score={row['final_score']:.4f}")
    return 0


def _cmd_replay_fallback(args: argparse.Namespace) -> int:
    from research.candidate_loop.ch_writer import replay_fallback

    client = _ch_client(required=True)
    path = Path(args.runs_root) / args.batch / "_results_fallback.jsonl"
    counts = replay_fallback(client, path)
    print(json.dumps(counts, sort_keys=True))
    return 0 if counts["failed"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research.candidate_loop")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="wrap a raw JSONL drop with the §11 provenance header")
    gen.add_argument("--run-id", required=True, help="generation run id (candidates/<run_id>/)")
    gen.add_argument("--family", required=True)
    gen.add_argument("--count", type=int, default=None, help="expected candidate count")
    gen.add_argument("--prompt", required=True, help="prompts/v1/<family>.md")
    gen.add_argument("--from-jsonl", required=True, help="raw model/template drop")
    gen.add_argument("--generation-model", default="external")
    gen.add_argument("--candidates-root", default=str(DEFAULT_CANDIDATES_ROOT))

    run = sub.add_parser("run", help="run a batch end-to-end (idempotent)")
    run.add_argument("--batch", required=True, help="run id == candidates/<run_id>/ dir")
    run.add_argument("--resume", action="store_true", help="alias of run (dedupe makes re-runs safe)")
    run.add_argument("--no-ch", action="store_true", help="skip ClickHouse; jsonl fallback only")
    run.add_argument("--candidates-root", default=str(DEFAULT_CANDIDATES_ROOT))
    run.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))

    summarize = sub.add_parser("summarize", help="rebuild failure_summary.json from ClickHouse")
    summarize.add_argument("--batch", required=True)
    summarize.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))

    promote = sub.add_parser("promote", help="print WATCHLIST/PROMOTED from failure_summary.json")
    promote.add_argument("--batch", required=True)
    promote.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))

    replay = sub.add_parser("replay-fallback", help="flush _results_fallback.jsonl into ClickHouse")
    replay.add_argument("--batch", required=True)
    replay.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "generate": _cmd_generate,
        "run": _cmd_run,
        "summarize": _cmd_summarize,
        "promote": _cmd_promote,
        "replay-fallback": _cmd_replay_fallback,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
