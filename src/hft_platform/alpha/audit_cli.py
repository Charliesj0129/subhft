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

    args = parser.parse_args(argv)
    if args.cmd == "show":
        out = show(args.run_id, strategy_type=args.strategy_type)
    elif args.cmd == "compare":
        out = compare(args.run_id_a, args.run_id_b, strategy_type=args.strategy_type)
    else:  # pragma: no cover — argparse already enforces this
        parser.print_usage()
        return 2

    print(out)
    if out.startswith("no audit row") or out.startswith("missing audit row"):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
