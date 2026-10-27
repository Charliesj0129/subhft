"""``python -m research status`` and ``python -m research evidence`` (ADR-005 section 6).

status
    One row per alpha: the effective status (ledger-derived, else the
    manifest's), its source, the declared status, run counts from both roots
    and the archived pre-ledger kills. A malformed ledger or archive is rc 2.
evidence init
    Create the evidence layout under the evidence root and ``git init`` it
    locally (no remote).
evidence audit-stale
    Mark the runs that a defect in ``config/research/defects.yaml`` affects,
    in ``ledger/stale.jsonl``. Marks flag evidence; they never change a status.
evidence import-kills
    Archive pre-ledger kills (``_kill_ledger.jsonl`` and ClickHouse
    ``audit.alpha_kill_ledger``, whose rows expire after 365 days) into
    ``legacy/kills.jsonl``. They never derive a status: they predate verdict
    rules v1, and the old Gate D wrote kills a correct run would not have.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from hft_platform.alpha.evidence import EvidenceRootError, EvidenceWriter, evidence_root, init_root
from hft_platform.alpha.status import derive_status, effective_status, ledger_events_by_alpha
from research.registry import legacy_evidence, stale_audit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m research")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="Effective alpha status from the verdict ledger")
    status.add_argument("--alpha", help="Show only this alpha")
    status.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    evidence = sub.add_parser("evidence", help="Evidence root maintenance")
    ev_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    ev_sub.add_parser("init", help="Create the evidence layout and git init it locally")
    imp = ev_sub.add_parser("import-kills", help="Archive pre-ledger kills into legacy/kills.jsonl")
    imp.add_argument(
        "--from",
        dest="source",
        choices=("all", "jsonl", "clickhouse"),
        default="all",
        help="Kill sources to read (default: all; a ClickHouse failure is rc 2)",
    )
    imp.add_argument("--dry-run", action="store_true", help="Count what would be archived; write nothing")
    stale = ev_sub.add_parser("audit-stale", help="Mark runs a registered defect affects (ledger/stale.jsonl)")
    stale.add_argument("--dry-run", action="store_true", help="Count what would be marked; write nothing")
    stale.add_argument("--json", action="store_true", help="Print the marks as JSON")
    return parser


def _discover_declared() -> dict[str, Any]:
    """Declared status per discovered alpha; discovery output goes to stderr.

    Discovery builds each alpha, and some log on construction; stdout carries
    only this command's own output (``--json`` must parse).
    """
    from research.registry.alpha_registry import AlphaRegistry

    with redirect_stdout(sys.stderr):
        loaded = AlphaRegistry().discover(legacy_evidence.research_root() / "alphas")
    return {aid: item.manifest.status for aid, item in loaded.items()}


def _status_event_ids(events: list[dict[str, Any]]) -> set[str]:
    """The events a ledger status rests on: each stage's latest non-INVALID event and every KILL."""
    latest: dict[str, str] = {}
    kills: set[str] = set()
    for event in events:
        verdict = event.get("verdict")
        if verdict == "INVALID":
            continue
        event_id = str(event.get("event_id", ""))
        latest[str(event.get("stage"))] = event_id
        if verdict == "KILL":
            kills.add(event_id)
    return (set(latest.values()) | kills) - {""}


def _status_rows(root: Path, only: str | None) -> list[dict[str, Any]]:
    ledger = ledger_events_by_alpha(root)
    declared = _discover_declared()
    runs = legacy_evidence.combined_run_stats(root, known=legacy_evidence.known_alpha_ids(extra=declared))
    stale_runs: dict[str, set[str]] = {}
    stale_events: set[str] = set()
    for mark in EvidenceWriter(root).read_stale_marks():
        stale_runs.setdefault(str(mark.get("alpha_id", "")), set()).add(str(mark["run_key"]))
        stale_events.update(str(e) for e in mark.get("event_ids") or ())
    kills: dict[str, int] = {}
    for kill in EvidenceWriter(root).read_legacy_kills():
        if not kill.get("quarantine"):
            kills[kill["alpha_id"]] = kills.get(kill["alpha_id"], 0) + 1
    rows: list[dict[str, Any]] = []
    for alpha_id in sorted(set(declared) | set(ledger)):
        if only is not None and alpha_id != only:
            continue
        events = ledger.get(alpha_id, [])
        if alpha_id in declared:
            eff = effective_status(declared[alpha_id], events)
            status, source, declared_value = eff.status.value, eff.source.value, declared[alpha_id].value
        else:
            derived = derive_status(events)
            status, source, declared_value = (derived.value if derived else ""), "ledger", ""
        stats = runs.get(alpha_id, {})
        basis = _status_event_ids(events) if source == "ledger" else set()
        rows.append(
            {
                "alpha_id": alpha_id,
                "status": status,
                "status_source": source,
                "declared_status": declared_value,
                "governed_run_count": int(stats.get("governed_run_count", 0)),
                "legacy_run_count": int(stats.get("legacy_run_count", 0)),
                "legacy_kill_count": kills.get(alpha_id, 0),
                "stale_run_count": len(stale_runs.get(alpha_id, ())),
                "status_on_stale_run": bool(basis & stale_events),
            }
        )
    return rows


def cmd_status(args: argparse.Namespace) -> int:
    try:
        root = evidence_root()
        rows = _status_rows(root, args.alpha)
    except (EvidenceRootError, ValueError, OSError) as exc:
        print(f"Cannot read the evidence root: {exc}", file=sys.stderr)
        return 2
    if args.alpha is not None and not rows:
        print(f"Unknown alpha: {args.alpha}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"evidence_root": str(root), "alphas": rows}, indent=2, sort_keys=True))
        return 0
    print(f"evidence root: {root}")
    for row in rows:
        print(
            f"{row['alpha_id']}\tstatus={row['status']}\tsource={row['status_source']}"
            f"\tdeclared={row['declared_status'] or '-'}\tgoverned_runs={row['governed_run_count']}"
            f"\tlegacy_runs={row['legacy_run_count']}\tlegacy_kills={row['legacy_kill_count']}"
            f"\tstale_runs={row['stale_run_count']}" + ("\tSTATUS_ON_STALE_RUN" if row["status_on_stale_run"] else "")
        )
    return 0


def cmd_evidence_init(_args: argparse.Namespace) -> int:
    try:
        root = evidence_root()
        created = init_root(root)
    except (EvidenceRootError, OSError) as exc:
        print(f"Cannot initialise the evidence root: {exc}", file=sys.stderr)
        return 2
    print(f"evidence root: {root}")
    print("created: " + (", ".join(created) if created else "nothing (already initialised)"))
    return 0


def cmd_import_kills(args: argparse.Namespace) -> int:
    from hft_platform.alpha import audit
    from hft_platform.alpha.kill_ledger import _jsonl_path

    kills: list[dict[str, Any]] = []
    try:
        root = evidence_root()
        known = legacy_evidence.known_alpha_ids(extra=_discover_declared())
        if args.source in ("all", "jsonl"):
            kills += legacy_evidence.read_jsonl_kills(_jsonl_path(), known)
        if args.source in ("all", "clickhouse"):
            kills += legacy_evidence.read_ch_kills(audit._get_client(), known)  # noqa: SLF001 - the canonical CH client
    except Exception as exc:  # noqa: BLE001 - any source failure stops the import with rc 2
        print(f"Cannot read the kill sources: {exc}", file=sys.stderr)
        return 2
    quarantined = sum(1 for k in kills if k["quarantine"])
    if args.dry_run:
        print(f"read {len(kills)} kills ({quarantined} quarantined as fixtures); dry run, nothing written")
        return 0
    try:
        added = EvidenceWriter(root).append_legacy_kills(kills)
    except (ValueError, OSError) as exc:
        print(f"Cannot archive the kills: {exc}", file=sys.stderr)
        return 2
    print(f"read {len(kills)} kills ({quarantined} quarantined as fixtures); archived {added} new under {root}")
    return 0


def cmd_audit_stale(args: argparse.Namespace) -> int:
    try:
        root = evidence_root()
        registry = stale_audit.load_registry()
        known = legacy_evidence.known_alpha_ids(extra=_discover_declared())
        runs = stale_audit.governed_runs(root) + stale_audit.legacy_runs(known=known)
        marks = stale_audit.audit(runs, registry, stale_audit.GitHistory())
    except (EvidenceRootError, ValueError, OSError, RuntimeError) as exc:
        print(f"Cannot audit stale runs: {exc}", file=sys.stderr)
        return 2
    per_defect: dict[str, set[str]] = {d.id: set() for d in registry.defects}
    for mark in marks:
        per_defect[mark["defect_id"]].add(mark["run_key"])
    added = 0
    if not args.dry_run:
        try:
            added = EvidenceWriter(root).append_stale_marks(marks)
        except (ValueError, OSError) as exc:
            print(f"Cannot write the stale marks: {exc}", file=sys.stderr)
            return 2
    if args.json:
        payload = {
            "evidence_root": str(root),
            "registry_sha256": registry.sha256,
            "audited_runs": len(runs),
            "stale_runs_per_defect": {k: len(v) for k, v in per_defect.items()},
            "marks": marks,
            "added": added,
            "dry_run": bool(args.dry_run),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"evidence root: {root}")
    print(f"audited {len(runs)} runs against {len(registry.defects)} defects (registry {registry.sha256[:12]})")
    for defect_id, keys in per_defect.items():
        print(f"{defect_id}\tstale_runs={len(keys)}")
    stale = len({m["run_key"] for m in marks})
    tail = "dry run, nothing written" if args.dry_run else f"{added} new marks under {root}"
    print(f"{stale} stale runs, {len(marks)} marks; {tail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "status":
        return cmd_status(args)
    if args.evidence_command == "init":
        return cmd_evidence_init(args)
    if args.evidence_command == "audit-stale":
        return cmd_audit_stale(args)
    return cmd_import_kills(args)
