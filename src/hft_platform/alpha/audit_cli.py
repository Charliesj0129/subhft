"""Sub-gate audit query / compare CLI (goal §9 read side).

Round 8 wrote the JSONL; Round 9 wired it into ``_invoke_sub_gates``.
This module is the consumer surface: human-readable answers to "why
was candidate X kept / killed?" and "what changed between two runs of
the same candidate?".

Public API (also reachable via ``python -m hft_platform.alpha.audit_cli``):

    show(run_id, strategy_type=None) -> str
    compare(run_id_a, run_id_b, strategy_type=None) -> str
    main(argv) -> int

Both ``show`` and ``compare`` return strings rather than printing so
they're testable without capturing stdout.  ``main`` prints the
returned string and exits 0 on success, 1 on missing-row / bad args.
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable

from hft_platform.alpha import sub_gate_audit


def _pick_row(
    run_id: str,
    strategy_type: str | None,
) -> dict | None:
    """Return the row for ``(run_id, strategy_type)``.

    When ``strategy_type`` is None, prefer maker over taker if both
    exist (matches the order the orchestrator typically evaluates).
    Returns None if no row matches.
    """
    rows = sub_gate_audit.read_runs(run_id=run_id)
    if not rows:
        return None
    if strategy_type is not None:
        for r in rows:
            if r.get("strategy_type") == strategy_type:
                return r
        return None
    for preferred in ("maker", "taker"):
        for r in rows:
            if r.get("strategy_type") == preferred:
                return r
    return rows[0]


def _format_gate_lines(sub_gates: list[dict]) -> Iterable[str]:
    for g in sub_gates:
        name = g.get("name", "?")
        passed = g.get("passed")
        marker = "PASS" if passed is True else "FAIL" if passed is False else "N/A "
        details = g.get("details") or ""
        yield f"  [{marker}] {name}: {details}"


def show(run_id: str, strategy_type: str | None = None) -> str:
    """Pretty-print one audit row (or return a not-found message)."""
    row = _pick_row(run_id, strategy_type)
    if row is None:
        suffix = f" (strategy_type={strategy_type})" if strategy_type else ""
        return f"no audit row for run_id={run_id!r}{suffix}"
    lines: list[str] = []
    lines.append(f"run_id          : {row.get('run_id', '')}")
    lines.append(f"strategy_name   : {row.get('strategy_name', '')}")
    lines.append(f"instrument      : {row.get('instrument', '')}")
    lines.append(f"strategy_type   : {row.get('strategy_type', '')}")
    lines.append(f"profile         : {row.get('profile_name', '') or '(loose)'}")
    blocking_passed = row.get("blocking_passed")
    lines.append(f"blocking_passed : {blocking_passed}")
    lines.append(f"triage_status   : {row.get('triage_status', '') or '(n/a)'}")
    reasons = row.get("triage_reasons") or []
    lines.append(f"triage_reasons  : {', '.join(reasons) if reasons else '(none)'}")
    # Goal §5 hard bar (Round 26): surface the per-round-trip edge at the
    # top of the row so audit show / compare highlight the > 10 pts/trade
    # floor without callers spelunking through sub_gates[*].metrics.
    edge = row.get("mean_net_edge_pts_per_trade")
    if edge is None:
        lines.append("mean_net_edge   : (n/a — edge_per_round_trip gate not run)")
    else:
        marker = "PASS" if edge > 10.0 else "FAIL"
        lines.append(f"mean_net_edge   : {edge:.3f} pts/trade  [vs goal §5 floor 10.0 -> {marker}]")
    lines.append("sub_gates:")
    lines.extend(_format_gate_lines(row.get("sub_gates", [])))
    return "\n".join(lines)


def _metric_diff(metrics_a: dict, metrics_b: dict) -> list[tuple[str, object, object]]:
    """Return [(key, a_value, b_value), ...] for keys whose values differ.

    Missing on either side is treated as a difference (recorded as None).
    Order: union of keys, sorted lexically for deterministic output.
    """
    keys = sorted(set(metrics_a) | set(metrics_b))
    out: list[tuple[str, object, object]] = []
    for k in keys:
        va = metrics_a.get(k)
        vb = metrics_b.get(k)
        if va != vb:
            out.append((k, va, vb))
    return out


def compare(
    run_id_a: str,
    run_id_b: str,
    strategy_type: str | None = None,
) -> str:
    """Diff two audit rows.

    Shows: triage_status A→B, blocking_passed A→B, then per-gate
    metric drift for every gate name present in either row.
    """
    a = _pick_row(run_id_a, strategy_type)
    b = _pick_row(run_id_b, strategy_type)
    if a is None or b is None:
        missing = [r for r, row in [(run_id_a, a), (run_id_b, b)] if row is None]
        return f"missing audit row(s): {missing}"

    lines: list[str] = []
    lines.append(f"A run_id : {run_id_a}  ({a.get('strategy_name', '')})")
    lines.append(f"B run_id : {run_id_b}  ({b.get('strategy_name', '')})")
    lines.append(f"profile       : {a.get('profile_name', '')!r} -> {b.get('profile_name', '')!r}")
    lines.append(f"blocking_pass : {a.get('blocking_passed')} -> {b.get('blocking_passed')}")
    lines.append(f"triage_status : {a.get('triage_status', '')!r} -> {b.get('triage_status', '')!r}")
    lines.append(f"triage_reasons: {a.get('triage_reasons') or []} -> {b.get('triage_reasons') or []}")
    # Round 26: edge drift surfacing.  Either side may have the metric.
    edge_a = a.get("mean_net_edge_pts_per_trade")
    edge_b = b.get("mean_net_edge_pts_per_trade")
    if edge_a is not None or edge_b is not None:
        marker = "~" if edge_a != edge_b else " "
        lines.append(f"  {marker} mean_net_edge: {edge_a!r} -> {edge_b!r}  (goal §5 floor: 10.0)")

    # Round 18: spec-provenance drift surfacing.  When either side
    # carries spec_provenance, show the diff so the operator can
    # attribute outcome differences to data_range / cost_model_id /
    # required_gates changes rather than treat them as noise.
    prov_a = a.get("spec_provenance") or {}
    prov_b = b.get("spec_provenance") or {}
    if prov_a or prov_b:
        lines.append("spec_provenance:")
        for key in ("data_range", "cost_model_id", "required_gates"):
            va = prov_a.get(key, "" if key != "required_gates" else [])
            vb = prov_b.get(key, "" if key != "required_gates" else [])
            if va != vb:
                lines.append(f"  ~ {key}: {va!r} -> {vb!r}")
            else:
                lines.append(f"    {key}: {va!r}")

    gates_a = {g["name"]: g for g in a.get("sub_gates", [])}
    gates_b = {g["name"]: g for g in b.get("sub_gates", [])}
    all_names = sorted(set(gates_a) | set(gates_b))
    lines.append("per-gate diff:")
    for name in all_names:
        ga = gates_a.get(name)
        gb = gates_b.get(name)
        if ga is None:
            lines.append(f"  + {name} (only in B): passed={gb.get('passed')}")
            continue
        if gb is None:
            lines.append(f"  - {name} (only in A): passed={ga.get('passed')}")
            continue
        if ga.get("passed") != gb.get("passed"):
            lines.append(f"  ~ {name}: passed {ga.get('passed')} -> {gb.get('passed')}")
        drift = _metric_diff(ga.get("metrics") or {}, gb.get("metrics") or {})
        if drift:
            if not (ga.get("passed") != gb.get("passed")):
                lines.append(f"  ~ {name}: metrics drift")
            for k, va, vb in drift:
                lines.append(f"      {k}: {va!r} -> {vb!r}")
    return "\n".join(lines)


def list_runs(
    strategy_type: str | None = None,
    *,
    profile: str | None = None,
    edge_min: float | None = None,
    only_passing: bool = False,
) -> str:
    """List all audit rows as a fixed-width table (goal §5 / §9 visibility).

    Columns: run_id | strategy_name | instrument | type | edge | block | triage.

    Filters (all AND-combined):
      * ``strategy_type`` — restrict to maker / taker rows
      * ``profile``       — exact match on ``profile_name`` (e.g. vm_ul6_strict)
      * ``edge_min``      — drop rows whose mean_net_edge is below this floor;
                            rows missing the metric are dropped only when an
                            explicit floor is supplied (caller wants edge-only)
      * ``only_passing``  — keep only rows with ``blocking_passed is True``
    """
    rows = sub_gate_audit.read_runs()
    if strategy_type is not None:
        rows = [r for r in rows if r.get("strategy_type") == strategy_type]
    if profile is not None:
        rows = [r for r in rows if r.get("profile_name") == profile]
    if only_passing:
        rows = [r for r in rows if r.get("blocking_passed") is True]
    if edge_min is not None:
        rows = [
            r
            for r in rows
            if isinstance(r.get("mean_net_edge_pts_per_trade"), (int, float))
            and float(r["mean_net_edge_pts_per_trade"]) >= edge_min
        ]
    if not rows:
        return "no audit rows match filter."

    headers = ("run_id", "strategy_name", "instrument", "type", "edge", "block", "triage")

    def _cells(row: dict) -> tuple[str, ...]:
        edge = row.get("mean_net_edge_pts_per_trade")
        edge_str = f"{float(edge):.2f}" if isinstance(edge, (int, float)) else "n/a"
        block = row.get("blocking_passed")
        block_str = {True: "PASS", False: "FAIL", None: "(loose)"}.get(block, str(block))
        return (
            str(row.get("run_id", ""))[:24],
            str(row.get("strategy_name", ""))[:24],
            str(row.get("instrument", ""))[:10],
            str(row.get("strategy_type", ""))[:6],
            edge_str,
            block_str,
            str(row.get("triage_status", "") or "")[:18],
        )

    body = [_cells(r) for r in rows]
    widths = [
        max(len(h), max(len(b[i]) for b in body)) for i, h in enumerate(headers)
    ]

    def _fmt(cells: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    out: list[str] = [_fmt(headers), _fmt(tuple("-" * w for w in widths))]
    out.extend(_fmt(c) for c in body)
    out.append(f"({len(body)} row{'s' if len(body) != 1 else ''})")
    return "\n".join(out)


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (numpy-compatible) without numpy.

    Returns 0.0 for empty input — callers gate on ``with_edge_count``
    before reading p50/p95 so the value is purely defensive.
    """
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def summary(
    strategy_type: str | None = None,
    *,
    profile: str | None = None,
) -> str:
    """Aggregate counts across the audit JSONL (goal §5 + §9 dashboard).

    Output groups:
      * total / by strategy_type / by blocking_passed
      * goal §5 hard bar: rows with edge, edge >= 10, edge p50/p95
      * triage_status distribution
    """
    rows = sub_gate_audit.read_runs()
    if strategy_type is not None:
        rows = [r for r in rows if r.get("strategy_type") == strategy_type]
    if profile is not None:
        rows = [r for r in rows if r.get("profile_name") == profile]
    if not rows:
        return "no audit rows match filter."

    total = len(rows)
    maker = sum(1 for r in rows if r.get("strategy_type") == "maker")
    taker = sum(1 for r in rows if r.get("strategy_type") == "taker")
    blk_pass = sum(1 for r in rows if r.get("blocking_passed") is True)
    blk_fail = sum(1 for r in rows if r.get("blocking_passed") is False)
    blk_loose = sum(1 for r in rows if r.get("blocking_passed") is None)

    edges: list[float] = [
        float(r["mean_net_edge_pts_per_trade"])
        for r in rows
        if isinstance(r.get("mean_net_edge_pts_per_trade"), (int, float))
    ]
    above_floor = sum(1 for e in edges if e > 10.0)

    triage_counts: dict[str, int] = {}
    for r in rows:
        key = str(r.get("triage_status") or "(none)")
        triage_counts[key] = triage_counts.get(key, 0) + 1

    lines: list[str] = []
    lines.append("audit summary")
    lines.append(f"  filter         : strategy_type={strategy_type!r} profile={profile!r}")
    lines.append(f"  total rows     : {total}")
    lines.append(f"  by type        : maker={maker} taker={taker}")
    lines.append(
        f"  blocking_passed: PASS={blk_pass} FAIL={blk_fail} (loose)={blk_loose}"
    )
    lines.append("goal §5 hard bar (mean_net_edge_pts_per_trade > 10):")
    lines.append(f"  rows with edge : {len(edges)} / {total}")
    lines.append(f"  rows > floor   : {above_floor}")
    if edges:
        lines.append(
            f"  edge p50/p95   : {_percentile(edges, 50):.3f} / {_percentile(edges, 95):.3f} pts/trade"
        )
        lines.append(
            f"  edge min/max   : {min(edges):.3f} / {max(edges):.3f} pts/trade"
        )
    lines.append("triage_status distribution:")
    for key in sorted(triage_counts):
        lines.append(f"  {key:24s}: {triage_counts[key]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hft-alpha-audit",
        description="Query the sub-gate audit JSONL (goal §9 replay).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    show_p = sub.add_parser("show", help="Print one audit row.")
    show_p.add_argument("run_id")
    show_p.add_argument("--strategy-type", choices=("maker", "taker"), default=None)

    cmp_p = sub.add_parser("compare", help="Diff two audit rows.")
    cmp_p.add_argument("run_id_a")
    cmp_p.add_argument("run_id_b")
    cmp_p.add_argument("--strategy-type", choices=("maker", "taker"), default=None)

    sum_p = sub.add_parser("summary", help="Aggregate counts across audit rows.")
    sum_p.add_argument("--strategy-type", choices=("maker", "taker"), default=None)
    sum_p.add_argument("--profile", default=None, help="Exact profile_name filter.")

    list_p = sub.add_parser("list", help="Tabulate all audit rows.")
    list_p.add_argument("--strategy-type", choices=("maker", "taker"), default=None)
    list_p.add_argument("--profile", default=None, help="Exact profile_name filter.")
    list_p.add_argument(
        "--edge-min",
        type=float,
        default=None,
        help="Drop rows whose mean_net_edge_pts_per_trade < this floor (and "
        "rows with no edge metric).",
    )
    list_p.add_argument(
        "--only-passing",
        action="store_true",
        help="Restrict to rows where blocking_passed is True.",
    )

    args = parser.parse_args(argv)
    if args.cmd == "show":
        out = show(args.run_id, strategy_type=args.strategy_type)
    elif args.cmd == "compare":
        out = compare(args.run_id_a, args.run_id_b, strategy_type=args.strategy_type)
    elif args.cmd == "list":
        out = list_runs(
            strategy_type=args.strategy_type,
            profile=args.profile,
            edge_min=args.edge_min,
            only_passing=args.only_passing,
        )
    elif args.cmd == "summary":
        out = summary(strategy_type=args.strategy_type, profile=args.profile)
    else:  # pragma: no cover — argparse already enforces this
        parser.print_usage()
        return 2

    print(out)
    if out.startswith("no audit row") or out.startswith("missing audit row"):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
