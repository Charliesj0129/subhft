"""CLI for the T1-A zero-event diagnostic."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from research.tools.t1_a_zero_event_diagnostic.aggregate import aggregate
from research.tools.t1_a_zero_event_diagnostic.classify import classify_dataframe
from research.tools.t1_a_zero_event_diagnostic.load import (
    csv_sha256,
    find_summary_sibling,
    freshness_check,
    load_and_dedupe_coverage,
    read_summary_event_count,
    read_viability_event_count,
)
from research.tools.t1_a_zero_event_diagnostic.verdict import THRESHOLDS, decide_verdict

SPEC_PATH = Path("docs/superpowers/specs/2026-05-19-t1a-zero-event-diagnostic-design.md")


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def _repo_root() -> Path:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return Path(out)
    except Exception:
        return Path.cwd()


def _spec_sha() -> str | None:
    path = _repo_root() / SPEC_PATH
    return csv_sha256(path) if path.exists() else None


def _jsonable_aggregate(agg) -> dict[str, Any]:
    data = asdict(agg)
    data["contract_month_grid"] = {
        f"{contract}|{year_month}|{cause}": count
        for (contract, year_month, cause), count in data["contract_month_grid"].items()
    }
    return data


def _render_markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    freshness = payload["run_config"]["freshness_check"]
    if freshness["match"] is False:
        lines.extend(
            [
                "> WARNING: coverage input row count does not match viability summary.",
                "> Regenerate coverage before treating this as final.",
                "",
            ]
        )
    lines.extend(
        [
            "# T1-A Zero-Event Diagnostic",
            "",
            f"- Spec: `{payload['run_config']['spec_path']}`",
            f"- Spec sha256: `{payload['run_config']['spec_sha256']}`",
            f"- Commit: `{payload['run_config']['git_sha']}`",
            f"- Viability event CSV: `{payload['run_config']['viability_events_csv']}`",
            f"- Viability event count: {payload['run_config']['viability_event_count']}",
            f"- Viability summary events: {payload['run_config']['viability_summary_event_count']}",
            f"- Coverage `would_emit` count: {payload['aggregate']['would_emit_count_from_coverage']}",
            "- Freshness: "
            f"summary={freshness['audited_trading_days_summary']} "
            f"input={freshness['audited_trading_days_in_input']} "
            f"match={freshness['match']}",
            "",
            "## Inputs",
            "",
        ]
    )
    for path, sha in payload["run_config"]["coverage_csv_sha256_by_path"].items():
        lines.append(f"- `{path}` (sha256 `{sha[:12]}...`)")

    lines.extend(
        [
            "",
            f"## Verdict: **{payload['verdict']}** "
            f"(primary reason: **{payload['primary_reason'] or '-'}**)",
            "",
        ]
    )
    if payload["reasons"]:
        lines.extend(f"- {reason}" for reason in payload["reasons"])
    else:
        lines.append("- No pre-registered rule fired.")

    lines.extend(
        [
            "",
            "## Cause Histogram",
            "",
            "| cause | count | pct |",
            "| --- | ---: | ---: |",
        ]
    )
    n_total = payload["aggregate"]["n_total"]
    denom = n_total or 1
    for cause, count in payload["aggregate"]["cause_counts"].items():
        lines.append(f"| {cause} | {count} | {count / denom:.1%} |")

    lines.extend(
        [
            "",
            "## Conditional Probabilities",
            "",
            "| metric | value |",
            "| --- | ---: |",
        ]
    )
    for metric, value in payload["aggregate"]["conditional_probs"].items():
        formatted = "-" if value is None else f"{value:.2%}"
        lines.append(f"| {metric} | {formatted} |")

    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="t1a-zero-event-diagnostic")
    parser.add_argument("--coverage-csv", required=True, action="append", type=Path)
    parser.add_argument("--viability-events-csv", required=True, type=Path)
    parser.add_argument("--out-markdown", required=True, type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        deduped, sha_map = load_and_dedupe_coverage(args.coverage_csv)
        viability_count = read_viability_event_count(args.viability_events_csv)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    summary_path = find_summary_sibling(args.viability_events_csv)
    summary_event_count = (
        read_summary_event_count(summary_path) if summary_path is not None else None
    )
    viability_count_for_verdict = (
        summary_event_count if summary_event_count is not None else viability_count
    )
    classified = classify_dataframe(deduped)
    agg = aggregate(classified)
    verdict = decide_verdict(agg, viability_event_count=viability_count_for_verdict)
    freshness = freshness_check(deduped, args.viability_events_csv)

    payload = {
        "verdict": verdict.verdict,
        "primary_reason": verdict.primary_reason,
        "reasons": verdict.reasons,
        "aggregate": _jsonable_aggregate(agg),
        "run_config": {
            "coverage_csv_sha256_by_path": sha_map,
            "freshness_check": freshness,
            "git_sha": _git_sha(),
            "spec_path": str(SPEC_PATH),
            "spec_sha256": _spec_sha(),
            "thresholds": THRESHOLDS,
            "viability_events_csv": str(args.viability_events_csv),
            "viability_events_csv_sha256": csv_sha256(args.viability_events_csv),
            "viability_event_count": viability_count,
            "viability_summary_event_count": summary_event_count,
        },
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    args.out_markdown.write_text(_render_markdown(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
