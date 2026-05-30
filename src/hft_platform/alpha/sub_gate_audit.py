"""Per-run sub-gate audit record (goal §4 / §9 traceability layer).

For every Gate-C invocation we want a durable, replayable record of
*why* a candidate was kept, killed, or routed to sample-triage.  The
blocking dict that ``hft_platform.alpha._gate_c._invoke_sub_gates``
returns is the canonical truth, but today it's discarded after the
caller renders a scorecard.  This module appends one JSONL row per
invocation so the decision is recoverable months later.

Design mirrors ``kill_ledger`` for consistency:
  * append-only JSONL at ``research/audit/sub_gate_runs.jsonl``
    (gitignored; overridable via ``HFT_SUB_GATE_AUDIT_PATH`` for tests),
  * deterministic dedupe key on ``(run_id, strategy_type)`` — repeated
    invocations of the same run with the same strategy type collapse
    to one row, so re-running a notebook doesn't pollute the log,
  * schema_version stamp on every row so the next migration is safe.

This module is writer-only — the orchestrator wiring lands in a
follow-up so this round stays within the "1 issue, ≤3 files" rule.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("alpha_sub_gate_audit")

SCHEMA_VERSION = "sub_gate_run.v2"

_DEFAULT_JSONL_PATH = Path("research/audit/sub_gate_runs.jsonl")


def _jsonl_path() -> Path:
    """Resolve the jsonl path; ``HFT_SUB_GATE_AUDIT_PATH`` overrides for tests."""
    override = os.getenv("HFT_SUB_GATE_AUDIT_PATH")
    return Path(override) if override else _DEFAULT_JSONL_PATH


@dataclass
class _DedupeCache:
    seen: set[tuple[str, str]] = field(default_factory=set)
    warmed_for: Path | None = None

    def warm(self, path: Path) -> None:
        if self.warmed_for == path and self.seen:
            return
        self.seen = set()
        self.warmed_for = path
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = (str(row.get("run_id", "")), str(row.get("strategy_type", "")))
                    if key[0] and key[1]:
                        self.seen.add(key)
        except OSError:
            logger.warning("sub_gate_audit warm failed", path=str(path), exc_info=True)

    def contains(self, run_id: str, strategy_type: str) -> bool:
        return (run_id, strategy_type) in self.seen

    def remember(self, run_id: str, strategy_type: str) -> None:
        self.seen.add((run_id, strategy_type))


_CACHE = _DedupeCache()


def _reset_cache_for_tests() -> None:
    """Test-only hook: clear the in-memory cache."""
    global _CACHE  # noqa: PLW0603
    _CACHE = _DedupeCache()


def _normalize_sub_gates(entries: list[dict] | None) -> list[dict]:
    """Project advisory entries onto the audit schema's stable shape."""
    if not entries:
        return []
    out: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        out.append(
            {
                "name": str(e.get("name", "")),
                "passed": e.get("passed"),
                "metrics": dict(e.get("metrics") or {}),
                "details": str(e.get("details", "")),
                "error": bool(e.get("error", False)),
            }
        )
    return out


def _extract_mean_net_edge(advisory: list[dict] | None) -> float | None:
    """Lift the goal §5 hard-bar metric from the edge_per_round_trip entry.

    Round 22-25 wired ``mean_net_edge_pts_per_trade`` into the
    edge_per_round_trip sub-gate's metrics dict.  That value is buried
    inside ``sub_gates[*].metrics`` — queryable but not at a glance.
    This helper lifts it to a top-level row field so ``audit show`` /
    ``audit compare`` can highlight the candidate's edge against the
    goal §5 ``> 10 pts/trade`` floor without callers parsing the gate
    list.  Returns ``None`` when the gate didn't run or wasn't applicable.
    """
    if not advisory:
        return None
    for entry in advisory:
        if not isinstance(entry, dict):
            continue
        if entry.get("name") != "edge_per_round_trip":
            continue
        metrics = entry.get("metrics") or {}
        if not isinstance(metrics, dict):
            return None
        value = metrics.get("mean_net_edge_pts_per_trade")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _extract_force_flat_share(advisory: list[dict] | None) -> float | None:
    """Lift force_flat_trip_share_pct from the ``force_flat_residual`` gate
    (Round 43) to a top-level row field (Round 45).

    Mirrors :func:`_extract_mean_net_edge`: ``audit show`` / ``summary``
    can flag candidates whose mean_net_edge is propped up by force-flat
    trips without parsing the sub_gates list.  Returns ``None`` when
    the gate didn't run or wasn't applicable.
    """
    if not advisory:
        return None
    for entry in advisory:
        if not isinstance(entry, dict):
            continue
        if entry.get("name") != "force_flat_residual":
            continue
        metrics = entry.get("metrics") or {}
        if not isinstance(metrics, dict):
            return None
        value = metrics.get("force_flat_trip_share_pct")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _extract_single_day_dominance(advisory: list[dict] | None) -> float | None:
    """Lift top_day_contribution_pct from the ``single_day_dominance`` gate
    to a top-level row field (Round 48).

    Mirrors :func:`_extract_mean_net_edge` / :func:`_extract_force_flat_share`.
    驗證標準 §5 requires checking whether OOS PnL is dominated by a few
    dates; the single-day-dominance pathology recurs across the team's
    KILLed rounds (R65 / cd600 / T1-A).  Surfacing the share next to the
    edge lets ``audit show`` / ``summary`` flag a candidate whose edge is
    carried by one trading day without parsing the sub_gates list.
    Returns ``None`` when the gate didn't run or wasn't applicable.
    """
    if not advisory:
        return None
    for entry in advisory:
        if not isinstance(entry, dict):
            continue
        if entry.get("name") != "single_day_dominance":
            continue
        metrics = entry.get("metrics") or {}
        if not isinstance(metrics, dict):
            return None
        value = metrics.get("top_day_contribution_pct")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _normalize_spec_provenance(prov: dict | None) -> dict[str, Any]:
    """Project a candidate spec provenance dict onto the row's stable shape.

    Round 17 (goal §4): every row should answer "what data range,
    cost-model id, and required-gate set did this Gate-C run think it
    was operating under?".  Today the orchestrator carries those in
    the candidate ``spec.yaml`` (Round 11); piping them into the
    audit row makes ``audit compare`` able to explain run-to-run
    differences as spec drift rather than result noise.

    Missing keys collapse to ``""`` / ``[]`` so a row never holds None
    for these fields — schema is opt-in by ``prov is not None`` but
    once opted-in the shape is fixed.
    """
    if not isinstance(prov, dict):
        return {}
    data_range = prov.get("data_range") or ""
    cost_model_id = prov.get("cost_model_id") or ""
    required_gates = prov.get("required_gates") or []
    if not isinstance(required_gates, list):
        required_gates = []
    return {
        "data_range": str(data_range),
        "cost_model_id": str(cost_model_id),
        "required_gates": [str(g) for g in required_gates],
    }


def build_record(
    *,
    run_id: str,
    strategy_name: str,
    instrument: str,
    strategy_type: str,
    profile_name: str,
    advisory: list[dict] | None,
    blocking: dict | None,
    recorded_at_ns: int | None = None,
    spec_provenance: dict | None = None,
) -> dict[str, Any]:
    """Construct the JSONL row from a Gate-C invocation result."""
    if not run_id:
        raise ValueError("run_id must be non-empty")
    if strategy_type not in {"maker", "taker"}:
        raise ValueError(f"strategy_type must be 'maker' or 'taker', got {strategy_type!r}")
    ts = recorded_at_ns if recorded_at_ns is not None else timebase.now_ns()
    blocking_passed: bool | None = None
    triage_status = ""
    triage_reasons: list[str] = []
    if isinstance(blocking, dict):
        blocking_passed = bool(blocking.get("passed", False))
        triage_status = str(blocking.get("triage_status", ""))
        triage_reasons = [str(r) for r in (blocking.get("triage_reasons") or [])]
    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at_ns": int(ts),
        "run_id": str(run_id),
        "strategy_name": str(strategy_name),
        "instrument": str(instrument),
        "strategy_type": strategy_type,
        "profile_name": str(profile_name or ""),
        "blocking_passed": blocking_passed,
        "triage_status": triage_status,
        "triage_reasons": triage_reasons,
        "sub_gates": _normalize_sub_gates(advisory),
    }
    edge = _extract_mean_net_edge(advisory)
    if edge is not None:
        row["mean_net_edge_pts_per_trade"] = edge
    ff_share = _extract_force_flat_share(advisory)
    if ff_share is not None:
        row["force_flat_trip_share_pct"] = ff_share
    day_dom = _extract_single_day_dominance(advisory)
    if day_dom is not None:
        row["single_day_dominance_pct"] = day_dom
    prov = _normalize_spec_provenance(spec_provenance)
    if prov:
        row["spec_provenance"] = prov
    return row


def record_sub_gate_run(
    *,
    run_id: str,
    strategy_name: str,
    instrument: str,
    strategy_type: str,
    profile_name: str,
    advisory: list[dict] | None,
    blocking: dict | None,
    recorded_at_ns: int | None = None,
    spec_provenance: dict | None = None,
) -> bool:
    """Append one row; return True iff a new row was written.

    Dedupes on ``(run_id, strategy_type)`` — same inputs twice produce
    exactly one line.  Disk failures log a warning and return False so
    the caller's pipeline doesn't crash on transient I/O issues.
    """
    path = _jsonl_path()
    _CACHE.warm(path)
    if _CACHE.contains(run_id, strategy_type):
        return False
    row = build_record(
        run_id=run_id,
        strategy_name=strategy_name,
        instrument=instrument,
        strategy_type=strategy_type,
        profile_name=profile_name,
        advisory=advisory,
        blocking=blocking,
        recorded_at_ns=recorded_at_ns,
        spec_provenance=spec_provenance,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        logger.warning("sub_gate_audit append failed", path=str(path), exc_info=True)
        return False
    _CACHE.remember(run_id, strategy_type)
    return True


def read_runs(run_id: str | None = None) -> list[dict[str, Any]]:
    """Read recorded rows (all or filtered by ``run_id``); natural append order."""
    path = _jsonl_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if run_id is not None and row.get("run_id") != run_id:
                continue
            out.append(row)
    return out
