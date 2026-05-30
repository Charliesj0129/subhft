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
import shutil
import sys
from pathlib import Path
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


# Round 51: strict-profile thresholds for the composite promotion verdict.
# These mirror the per-axis caps already surfaced in show()/summary() and
# vm_ul6_strict.yaml; the verdict is purely derived (no relaxation of any
# bar — 限制 §3).
_EDGE_FLOOR_PTS = 10.0
_FORCE_FLAT_CAP_PCT = 30.0
_DAY_DOMINANCE_CAP_PCT = 25.0
_DRAWDOWN_RATIO_CAP = 2.0
_TOP_MONTH_CAP_PCT = 50.0
_WORST_LOSS_SHARE_CAP_PCT = 50.0
_REPLAY_MATCH_FLOOR_PCT = 95.0


def promotion_readiness(row: dict) -> tuple[bool, list[str]]:
    """Derive a single promotion verdict from the already-lifted row fields.

    Answers 驗證標準 §9 ("know the kept/killed rationale") in one place: a
    candidate is promotion-ready only when every credibility axis clears
    its bar *and* blocking gates passed.  Pure function over fields the
    earlier rounds lifted (edge / force-flat / dominance / sample-adequacy /
    drawdown-ratio / top-month-share / worst-loss-share / replay-parity /
    cost-model) plus ``blocking_passed`` — it re-derives nothing and relaxes
    nothing
    (限制 §3).  A missing metric is treated as a blocker (the gate must
    have run to clear the axis), tagged ``<axis>:missing``.

    Returns ``(ready, blockers)`` where ``blockers`` is empty iff ready.
    """
    blockers: list[str] = []

    edge = row.get("mean_net_edge_pts_per_trade")
    if not isinstance(edge, (int, float)):
        blockers.append("edge:missing")
    elif not float(edge) > _EDGE_FLOOR_PTS:
        blockers.append("edge")

    ff = row.get("force_flat_trip_share_pct")
    if isinstance(ff, (int, float)) and float(ff) > _FORCE_FLAT_CAP_PCT:
        blockers.append("force_flat")
    # force-flat missing is not a blocker on its own: the gate may be
    # inapplicable (no residual); the residual gate passes advisory in
    # that case and the edge already accounts for realized PnL.

    dom = row.get("single_day_dominance_pct")
    if not isinstance(dom, (int, float)):
        blockers.append("dominance:missing")
    elif float(dom) > _DAY_DOMINANCE_CAP_PCT:
        blockers.append("dominance")

    sample = row.get("sample_adequacy_label")
    if not isinstance(sample, str):
        blockers.append("sample:missing")
    elif sample != "adequate":
        blockers.append("sample")

    # Round 56: 驗證標準 §6 — max_drawdown must stay within 2× avg-monthly net
    # PnL. Like force-flat, a missing ratio is not a blocker on its own: the
    # monthly_distribution gate needs >=2 months and is inapplicable to a
    # short-window candidate. But a *present* breach (incl. inf when there is
    # no positive monthly baseline) blocks promotion.
    dd_ratio = row.get("drawdown_to_avg_monthly_ratio")
    if isinstance(dd_ratio, (int, float)) and float(dd_ratio) > _DRAWDOWN_RATIO_CAP:
        blockers.append("drawdown")

    # Round 57: 驗證標準 §6 單月收益支配性 — a candidate whose net PnL leans on
    # one calendar month is not a durable monthly stream. Same gate as the
    # drawdown axis, so a missing share is not a blocker; a present breach is.
    top_month = row.get("top_month_contribution_pct")
    if isinstance(top_month, (int, float)) and float(top_month) > _TOP_MONTH_CAP_PCT:
        blockers.append("top_month")

    # Round 61: 驗證標準 §5 (虧損分布/是否被少數交易支配) — one trade carrying
    # >50% of total loss makes the edge hostage to a single fill. Same
    # missing-not-a-blocker semantics as the dominance axes.
    worst_loss = row.get("worst_loss_share_pct")
    if isinstance(worst_loss, (int, float)) and float(worst_loss) > _WORST_LOSS_SHARE_CAP_PCT:
        blockers.append("worst_loss")

    # Round 63: 驗證標準 §7 (回測↔replay 訊號一致性, 限制 §3 不可放寬) — a present
    # replay-parity result below the 95% match floor blocks. A missing result
    # is not a blocker here (mirrors the other lifted axes); demonstrating
    # parity for a row that has no replay log is left to a later round.
    replay_match = row.get("replay_match_pct")
    if isinstance(replay_match, (int, float)) and float(replay_match) < _REPLAY_MATCH_FLOOR_PCT:
        blockers.append("replay_parity")

    # Round 68: 驗證標準 §2 / 限制 §3 (成本扣除 不可放寬) — a *declared but
    # incomplete* cost model blocks: if a cost_model_id is present yet omits a
    # §2 knob (fee/tax/slip/latency), the edge can't be trusted as net. A row
    # with no cost_model_id at all is not blocked here (that traceability gap
    # is reported by spec_provenance_complete instead).
    prov = row.get("spec_provenance")
    cmid = prov.get("cost_model_id") if isinstance(prov, dict) else None
    if isinstance(cmid, str) and cmid:
        cost_ok, _cost_missing = cost_model_complete(row)
        if not cost_ok:
            blockers.append("cost_model")

    if row.get("blocking_passed") is not True:
        blockers.append("blocking")

    return (not blockers, blockers)


# Round 64: map each promotion blocker onto the 迭代規則 §5 triage vocabulary
# (failed / needs_more_sample / blocked_by_parity / blocked_by_risk /
# blocked_by_audit). A row may carry several blockers; the dominant one is
# chosen by category precedence below.
_BLOCKER_TRIAGE_CATEGORY: dict[str, str] = {
    "replay_parity": "blocked_by_parity",
    "drawdown": "blocked_by_risk",
    "worst_loss": "blocked_by_risk",
    "blocking": "blocked_by_risk",
    "sample": "needs_more_sample",
    "cost_model": "blocked_by_audit",
    "edge": "failed",
    "dominance": "failed",
    "top_month": "failed",
    "force_flat": "failed",
}
# Highest precedence first: an incomplete audit record can't be trusted at
# all, then a parity break, then risk-limit breaches, then sample shortfall,
# then a plain credibility failure.
_TRIAGE_PRECEDENCE: tuple[str, ...] = (
    "blocked_by_audit",
    "blocked_by_parity",
    "blocked_by_risk",
    "needs_more_sample",
    "failed",
)


def triage_reason(row: dict) -> str:
    """Map a row's promotion verdict onto the user's triage vocabulary.

    驗證標準 §9 + 迭代規則 §5: every kept/killed decision must carry a reason
    in the fixed vocabulary.  ``promotion_readiness`` already lists the
    failing axes; this collapses them to the single dominant triage label so
    ``audit show`` and any decision-replay reads the *category* of failure,
    not just which metric tripped.  Pure derivation over the verdict — no
    relaxation, no re-computation.

    Returns ``"promotable"`` when the row clears every axis, else the
    highest-precedence triage category among its blockers.  Any
    ``<axis>:missing`` blocker maps to ``blocked_by_audit`` (the record can't
    be evaluated until the gate runs).
    """
    ready, blockers = promotion_readiness(row)
    if ready:
        return "promotable"
    categories = {
        "blocked_by_audit" if b.endswith(":missing") else _BLOCKER_TRIAGE_CATEGORY.get(b, "failed")
        for b in blockers
    }
    for category in _TRIAGE_PRECEDENCE:
        if category in categories:
            return category
    return "failed"


# Round 58: the spec-provenance keys a traceable Gate-C run must carry
# (goal §4: 資料區間 / 成本假設 / required-gate set). These mirror
# strategy_spec.load_spec_provenance's output shape.
_REQUIRED_PROVENANCE_KEYS: tuple[str, ...] = (
    "data_range",
    "cost_model_id",
    "required_gates",
)


def spec_provenance_complete(row: dict) -> tuple[bool, list[str]]:
    """Report whether a row carries a complete spec provenance (goal §4).

    A traceable experiment record must answer "what data range, cost-model,
    and required-gate set drove this run?".  ``spec_provenance`` is opt-in at
    record time, but once a row claims promotability a reviewer needs to know
    the audit trail is intact — a row whose provenance is absent or has empty
    required fields is *traceability-incomplete*, distinct from the
    credibility verdict (``promotion_readiness``).  This re-derives and
    relaxes nothing; it only inspects already-stored fields.

    Returns ``(complete, missing)`` where ``missing`` lists the absent/empty
    provenance keys (and is empty iff complete).  A row with no
    ``spec_provenance`` at all reports every required key as missing.
    """
    prov = row.get("spec_provenance")
    if not isinstance(prov, dict):
        return (False, list(_REQUIRED_PROVENANCE_KEYS))
    missing: list[str] = []
    for key in _REQUIRED_PROVENANCE_KEYS:
        value = prov.get(key)
        if key == "required_gates":
            if not isinstance(value, list) or not value:
                missing.append(key)
        elif not (isinstance(value, str) and value):
            missing.append(key)
    return (not missing, missing)


# Round 70: the 完成狀態 §3 fixed-spec top-level fields. The audit record can
# attest to only a subset from what it stores; the rest live solely in
# spec.yaml. Listing both halves makes the traceability boundary explicit.
_SPEC_FIELDS_3: tuple[str, ...] = (
    "strategy_name",
    "market",
    "instrument",
    "hypothesis",
    "timeframe",
    "holding_period",
    "entry_rule",
    "exit_rule",
    "position_sizing",
    "risk_control",
    "cost_model",
    "validation_plan",
)


def spec_field_audit(row: dict) -> tuple[list[str], list[str]]:
    """Report which 完成狀態 §3 spec fields the audit record can attest to.

    The fixed spec has 12 top-level fields, but a sub-gate audit row only
    stores a handful directly (``strategy_name``, ``instrument``) plus
    evidence for ``cost_model`` / ``validation_plan`` via ``spec_provenance``.
    The behavioural fields (hypothesis / entry_rule / risk_control / …) live
    only in ``spec.yaml`` and are *not* recoverable from the audit log alone.

    This makes that boundary explicit (迭代規則 §2 可追溯性) without re-loading
    the spec file or fabricating evidence: it returns ``(traceable,
    untraceable)`` over ``_SPEC_FIELDS_3`` so a reviewer sees the audit record
    attests to N of 12 fields and which N are missing from the record.
    """
    prov = row.get("spec_provenance")
    prov = prov if isinstance(prov, dict) else {}
    cost_ok, _ = cost_model_complete(row)
    has_validation = bool(prov.get("data_range")) or bool(prov.get("required_gates"))
    attestable: dict[str, bool] = {
        "strategy_name": bool(row.get("strategy_name")),
        "instrument": bool(row.get("instrument")),
        "cost_model": cost_ok,
        "validation_plan": has_validation,
        # Round 71: carried additively in spec_provenance when the candidate
        # spec supplied them; absent for rows that predate the extension.
        "timeframe": bool(prov.get("timeframe")),
        "holding_period": bool(prov.get("holding_period")),
    }
    traceable = [f for f in _SPEC_FIELDS_3 if attestable.get(f, False)]
    untraceable = [f for f in _SPEC_FIELDS_3 if not attestable.get(f, False)]
    return (traceable, untraceable)


def _token_is_number(token: str, suffix: str) -> bool:
    """True when ``token`` is ``<number><suffix>`` with a parseable number."""
    body = token[: -len(suffix)] if suffix and token.endswith(suffix) else token
    try:
        float(body)
    except ValueError:
        return False
    return True


def cost_model_complete(row: dict) -> tuple[bool, list[str]]:
    """Report which §2 cost knobs the row's declared cost_model omits.

    驗證標準 §2: net edge must subtract 手續費 / 稅 / 滑點 / latency adverse
    selection (plus bid-ask spread, force-flat, residual MtM — those are
    surfaced separately).  ``extract_provenance`` packs the first four into
    ``cost_model_id = "<latency>+<fee>bp/<tax>bp/<slip>pts"``; a knob left
    unset serialises as ``None``/empty, so a reviewer can't tell a deducted
    cost from a silently-omitted one.  This parses the already-stored id and
    flags any knob that is absent or non-numeric — it reads, never relaxes,
    the cost model (限制 §2).

    Returns ``(complete, missing)`` over ``{latency_profile, fee_bps,
    tax_bps, slippage_pts}``.  A row with no cost_model_id reports all four.
    """
    knobs = ["latency_profile", "fee_bps", "tax_bps", "slippage_pts"]
    prov = row.get("spec_provenance")
    cmid = prov.get("cost_model_id") if isinstance(prov, dict) else None
    if not isinstance(cmid, str) or "+" not in cmid:
        return (False, list(knobs))
    latency, _, rest = cmid.partition("+")
    missing: list[str] = []
    if not latency or latency in ("unspecified", "None"):
        missing.append("latency_profile")
    parts = rest.split("/")
    checks = (("fee_bps", "bp"), ("tax_bps", "bp"), ("slippage_pts", "pts"))
    for idx, (name, suffix) in enumerate(checks):
        if idx >= len(parts) or not _token_is_number(parts[idx], suffix):
            missing.append(name)
    return (not missing, missing)


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
    # Round 45: surface force-flat residual share next to the edge so a
    # reviewer can immediately see whether the edge is propped up by
    # end-of-window inventory marks (Round 41-44 chain).
    ff_share = row.get("force_flat_trip_share_pct")
    if ff_share is None:
        lines.append("force_flat_share: (n/a — force_flat_residual gate not run)")
    else:
        ff_marker = "PASS" if ff_share <= 30.0 else "FAIL"
        lines.append(
            f"force_flat_share: {ff_share:.1f}% of trips  [vs strict cap 30.0% -> {ff_marker}]"
        )
    # Round 48: surface single-day dominance (驗證標準 §5) next to the edge
    # so a reviewer sees whether the edge is carried by one trading day —
    # the pathology that KILLed R65 / cd600 / T1-A.
    day_dom = row.get("single_day_dominance_pct")
    if day_dom is None:
        lines.append("single_day_dom : (n/a — single_day_dominance gate not run)")
    else:
        dom_marker = "PASS" if day_dom <= 25.0 else "FAIL"
        lines.append(
            f"single_day_dom : {day_dom:.1f}% of |total|  [vs strict cap 25.0% -> {dom_marker}]"
        )
    # Round 50: sample-adequacy label (驗證標準 §4) — a blocking-passed
    # candidate that is not 'adequate' must still be triaged as
    # promising / needs_more_sample / inconclusive, never complete.
    sample_label = row.get("sample_adequacy_label")
    if sample_label is None:
        lines.append("sample_adequacy: (n/a — min_sample_size gate not run)")
    else:
        sample_marker = "READY" if sample_label == "adequate" else "NOT-READY"
        lines.append(
            f"sample_adequacy: {sample_label}  [§4 -> {sample_marker}]"
        )
    # Round 53: drawdown-to-avg-monthly ratio (驗證標準 §6) — max_drawdown must
    # stay within 2× average monthly net PnL, else a candidate's edge is not
    # survivable month-to-month. inf (avg_monthly <= 0) always reads FAIL.
    dd_ratio = row.get("drawdown_to_avg_monthly_ratio")
    if dd_ratio is None:
        lines.append("drawdown_ratio : (n/a — monthly_distribution gate not run)")
    else:
        dd_marker = "PASS" if dd_ratio <= 2.0 else "FAIL"
        dd_text = "inf" if dd_ratio == float("inf") else f"{dd_ratio:.2f}"
        lines.append(
            f"drawdown_ratio : {dd_text}× avg-monthly  [vs strict cap 2.0× -> {dd_marker}]"
        )
    # Round 54: single-month dominance (驗證標準 §6 "單月收益支配性") — the
    # monthly analogue of single_day_dom; a candidate whose net PnL leans on
    # one calendar month is not a durable monthly-income stream.
    top_month = row.get("top_month_contribution_pct")
    if top_month is None:
        lines.append("top_month_share: (n/a — monthly_distribution gate not run)")
    else:
        tm_marker = "PASS" if top_month <= 50.0 else "FAIL"
        lines.append(
            f"top_month_share: {top_month:.1f}% of net  [vs strict cap 50.0% -> {tm_marker}]"
        )
    # Round 60: worst-trade loss share (驗證標準 §5 損益/虧損分布) — what
    # fraction of total loss the single worst trade carries; high share means
    # the edge is hostage to one trade landing differently.
    worst_loss = row.get("worst_loss_share_pct")
    if worst_loss is None:
        lines.append("worst_loss_share: (n/a — trade_concentration gate not run)")
    else:
        wl_marker = "PASS" if worst_loss <= 50.0 else "FAIL"
        lines.append(
            f"worst_loss_share: {worst_loss:.1f}% of |loss|  [vs strict cap 50.0% -> {wl_marker}]"
        )
    # Round 62: replay parity (驗證標準 §7/§8) — backtest↔replay intent match,
    # with the dominant divergence category named when below the floor.
    replay_match = row.get("replay_match_pct")
    if replay_match is None:
        lines.append("replay_parity  : (n/a — replay_parity gate not run)")
    else:
        rp_marker = "PASS" if replay_match >= 95.0 else "FAIL"
        category = row.get("replay_divergence_category")
        suffix = (
            f"; dominant={category}"
            if isinstance(category, str) and category and replay_match < 95.0
            else ""
        )
        lines.append(
            f"replay_parity  : {replay_match:.1f}% match  [vs strict floor 95.0% -> {rp_marker}{suffix}]"
        )
    # Round 66: residual mark-to-market (驗證標準 §2/§3) — how much of net edge
    # is carried by the residual mark vs realized fills, so a reviewer can see
    # whether un-FIFO'd inventory is propping the edge up.
    residual_mtm = row.get("residual_mtm_pts")
    if residual_mtm is None:
        lines.append("residual_mtm   : (n/a — inventory_mtm gate not run)")
    else:
        inv_net = row.get("inventory_net_pts")
        if isinstance(inv_net, (int, float)) and inv_net != 0:
            share = residual_mtm / inv_net * 100.0
            lines.append(
                f"residual_mtm   : {residual_mtm:.1f} pts  ({share:.0f}% of net {inv_net:.1f})"
            )
        else:
            lines.append(f"residual_mtm   : {residual_mtm:.1f} pts  (net n/a)")
    # Round 51: composite promotion verdict over every lifted axis, so a
    # reviewer gets the kept/killed answer (驗證標準 §9) in one line.
    ready, blockers = promotion_readiness(row)
    if ready:
        lines.append("promotion_ready: READY  [all credibility axes + blocking clear]")
    else:
        lines.append(f"promotion_ready: NOT-READY  [blockers: {', '.join(blockers)}]")
    # Round 64: dominant triage reason in the user's fixed vocabulary
    # (迭代規則 §5) so the kept/killed rationale (驗證標準 §9) is one word.
    lines.append(f"triage_reason  : {triage_reason(row)}")
    # Round 58: traceability completeness (goal §4) — separate from the
    # credibility verdict; flags a row whose audit trail can't answer
    # data-range / cost-model / required-gate provenance.
    spec_ok, spec_missing = spec_provenance_complete(row)
    if spec_ok:
        lines.append("spec_provenance: complete  [data_range, cost_model_id, required_gates]")
    else:
        lines.append(
            f"spec_provenance: INCOMPLETE  [missing: {', '.join(spec_missing)}]"
        )
    # Round 67: §2 cost-knob completeness parsed from the declared cost_model_id
    # so a silently-omitted fee/tax/slippage/latency can't pass unnoticed.
    cost_ok, cost_missing = cost_model_complete(row)
    if cost_ok:
        lines.append("cost_model     : complete  [latency, fee, tax, slippage declared]")
    else:
        lines.append(
            f"cost_model     : INCOMPLETE  [§2 missing: {', '.join(cost_missing)}]"
        )
    # Round 70: 完成狀態 §3 fixed-spec coverage the audit record can attest to;
    # behavioural fields live only in spec.yaml, not the audit log.
    traceable, untraceable = spec_field_audit(row)
    spec_line = f"spec_fields    : {len(traceable)}/{len(_SPEC_FIELDS_3)} traceable in record"
    if untraceable:
        spec_line += f"  [not in audit log: {', '.join(untraceable)}]"
    lines.append(spec_line)
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


_DEFAULT_TEMPLATE = Path("research/alphas/_templates/spec.yaml")
_DEFAULT_ALPHAS_ROOT = Path("research/alphas")
_TEMPLATE_NAME = "exemplar_txfd6_demo"

# Round 37: map shape tag -> (template path, placeholder strategy_name).
# `audit init --shape <tag>` picks the right exemplar without the
# author having to know the file path.  Keep `single` first so it
# remains the default and the back-compat behavior is preserved.
_SHAPE_TEMPLATES: dict[str, tuple[Path, str]] = {
    "single": (_DEFAULT_TEMPLATE, _TEMPLATE_NAME),
    "straddle": (
        Path("research/alphas/_templates/spec.straddle.yaml"),
        "txo_straddle_atm_demo",
    ),
    "futures_pair": (
        Path("research/alphas/_templates/spec.futures_pair.yaml"),
        "txf_tmf_hedged_pair_demo",
    ),
}


def init_candidate(
    alpha_id: str,
    *,
    template: str | Path | None = None,
    shape: str | None = None,
    root: str | Path = _DEFAULT_ALPHAS_ROOT,
    strategy_name: str | None = None,
    force: bool = False,
) -> str:
    """Scaffold a new candidate directory from the spec template.

    Goal §3 + §9: "固定模板新增策略" should be a single command, not a
    manual file-copy ritual.  This:
      1. Validates ``alpha_id`` is a safe directory name.
      2. Refuses to overwrite an existing directory unless ``force``.
      3. Copies the template spec to ``<root>/<alpha_id>/spec.yaml``.
      4. Substitutes ``strategy_name`` (default: ``alpha_id``).
      5. Runs ``spec_check.check_one`` and reports the verdict.

    Returns a multi-line status string suitable for stdout.
    """
    from hft_platform.alpha import spec_check

    if not alpha_id or "/" in alpha_id or alpha_id.startswith("."):
        return f"refused: alpha_id {alpha_id!r} must be a plain directory name"
    # Round 37: shape -> template resolution.  Explicit --template wins
    # so old callers keep working.  `shape` must be one of the known
    # exemplar tags or the call is refused (no silent fall-back to
    # single — that would mask typos like --shape stradle).
    if template is not None and shape is not None:
        return "refused: pass either --template OR --shape, not both"
    placeholder_name = _TEMPLATE_NAME
    if template is not None:
        template_path = Path(template)
    elif shape is not None:
        if shape not in _SHAPE_TEMPLATES:
            return (
                f"refused: unknown shape {shape!r}; "
                f"choose from {sorted(_SHAPE_TEMPLATES)}"
            )
        template_path, placeholder_name = _SHAPE_TEMPLATES[shape]
    else:
        template_path = _DEFAULT_TEMPLATE
    if not template_path.is_file():
        return f"refused: template not found at {template_path}"
    target_dir = Path(root) / alpha_id
    target_spec = target_dir / "spec.yaml"
    if target_spec.exists() and not force:
        return (
            f"refused: {target_spec} already exists; pass --force to overwrite, "
            "or pick a different alpha_id"
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_path, target_spec)
    raw = target_spec.read_text(encoding="utf-8")
    new_name = strategy_name or alpha_id
    # Single literal substitution: the exemplar's strategy_name slot.
    if placeholder_name in raw:
        raw = raw.replace(placeholder_name, new_name)
        target_spec.write_text(raw, encoding="utf-8")

    passed, errors = spec_check.check_one(target_spec)
    lines: list[str] = []
    lines.append(f"created : {target_spec}")
    lines.append(f"strategy_name set to : {new_name}")
    if passed:
        lines.append("spec_check: PASS")
    else:
        lines.append(f"spec_check: FAIL ({len(errors)} error{'s' if len(errors) != 1 else ''})")
        lines.extend(f"  - {e}" for e in errors)
        lines.append(
            "(this is expected — fill in hypothesis / entry_rule / exit_rule / "
            "validation_plan before promotion)"
        )
    return "\n".join(lines)


_PLACEHOLDER_MARKERS: tuple[str, ...] = ("TODO", "FILLME", "PLACEHOLDER", "exemplar_txfd6_demo")


def _field_state(value: object) -> str:
    """Classify a top-level spec field value.

    Returns one of: ``"missing"``, ``"placeholder"``, ``"set"``.

    ``missing``     — key absent or value is None / empty string / empty list/dict.
    ``placeholder`` — value contains a known TODO / placeholder marker
                      (recursively for strings inside lists / dicts).
    ``set``         — anything else (treated as actually filled-in).
    """
    if value is None:
        return "missing"
    if isinstance(value, str):
        if not value.strip():
            return "missing"
        upper = value.upper()
        if any(m.upper() in upper for m in _PLACEHOLDER_MARKERS):
            return "placeholder"
        return "set"
    if isinstance(value, (list, tuple)):
        if not value:
            return "missing"
        # If any element is a placeholder string, surface that.
        states = [_field_state(v) for v in value]
        if "placeholder" in states:
            return "placeholder"
        return "set"
    if isinstance(value, dict):
        if not value:
            return "missing"
        states = [_field_state(v) for v in value.values()]
        if "placeholder" in states:
            return "placeholder"
        return "set"
    return "set"


def verify_spec(
    alpha_id: str | None = None,
    *,
    root: str | Path = _DEFAULT_ALPHAS_ROOT,
    all_specs: bool = False,
) -> str:
    """Show per-field fill-state for one or every candidate spec.

    Goal §3 + §9: backfill stubs need a way to track which required
    fields are still placeholders.  ``audit verify-spec <id>`` prints
    a per-field breakdown; ``--all`` produces a summary table
    (alpha_id | total | set | placeholder | missing | spec_check).
    """
    from hft_platform.alpha import spec_check
    from hft_platform.alpha.strategy_spec import (
        REQUIRED_TOP_LEVEL_FIELDS,
        load_spec,
    )

    root_path = Path(root)
    if not root_path.is_dir():
        return f"refused: alphas root not found at {root_path}"

    if all_specs:
        rows: list[tuple[str, int, int, int, int, str]] = []
        for d in sorted(root_path.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            spec_path = d / "spec.yaml"
            if not spec_path.is_file():
                rows.append((d.name, len(REQUIRED_TOP_LEVEL_FIELDS), 0, 0, len(REQUIRED_TOP_LEVEL_FIELDS), "NO_SPEC"))
                continue
            try:
                spec = load_spec(spec_path)
            except Exception:  # noqa: BLE001
                rows.append((d.name, len(REQUIRED_TOP_LEVEL_FIELDS), 0, 0, 0, "PARSE_ERR"))
                continue
            counts = {"set": 0, "placeholder": 0, "missing": 0}
            for fld in REQUIRED_TOP_LEVEL_FIELDS:
                counts[_field_state(spec.get(fld))] += 1
            passed, _errors = spec_check.check_one(spec_path)
            rows.append(
                (
                    d.name,
                    len(REQUIRED_TOP_LEVEL_FIELDS),
                    counts["set"],
                    counts["placeholder"],
                    counts["missing"],
                    "PASS" if passed else "FAIL",
                )
            )
        if not rows:
            return f"no candidate directories under {root_path}"
        headers = ("alpha_id", "total", "set", "placeholder", "missing", "spec_check")
        body = [tuple(str(c) for c in r) for r in rows]
        widths = [max(len(h), max(len(b[i]) for b in body)) for i, h in enumerate(headers)]

        def _fmt(cells: tuple[str, ...]) -> str:
            return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

        out: list[str] = [_fmt(headers), _fmt(tuple("-" * w for w in widths))]
        out.extend(_fmt(c) for c in body)
        out.append(f"({len(body)} candidates scanned)")
        return "\n".join(out)

    # Single-spec mode.
    if not alpha_id:
        return "refused: pass an alpha_id, or use --all"
    spec_path = root_path / alpha_id / "spec.yaml"
    if not spec_path.is_file():
        return f"no spec.yaml at {spec_path}"
    try:
        spec = load_spec(spec_path)
    except Exception as exc:  # noqa: BLE001
        return f"parse error: {exc!r}"

    from hft_platform.alpha.strategy_spec import classify_strategy_shape

    lines = [
        f"alpha_id : {alpha_id}",
        f"path     : {spec_path}",
        f"shape    : {classify_strategy_shape(spec)}",
        "fields:",
    ]
    counts = {"set": 0, "placeholder": 0, "missing": 0}
    for fld in REQUIRED_TOP_LEVEL_FIELDS:
        state = _field_state(spec.get(fld))
        counts[state] += 1
        marker = {"set": "OK ", "placeholder": "TODO", "missing": "MISS"}[state]
        lines.append(f"  [{marker}] {fld}")
    lines.append(
        f"summary  : set={counts['set']} placeholder={counts['placeholder']} "
        f"missing={counts['missing']} / total={len(REQUIRED_TOP_LEVEL_FIELDS)}"
    )
    passed, errors = spec_check.check_one(spec_path)
    lines.append(f"spec_check: {'PASS' if passed else 'FAIL'}")
    if errors:
        lines.extend(f"  - {e}" for e in errors[:10])
    return "\n".join(lines)


def backfill_specs(
    *,
    template: str | Path = _DEFAULT_TEMPLATE,
    root: str | Path = _DEFAULT_ALPHAS_ROOT,
    apply: bool = False,
) -> str:
    """Backfill missing ``spec.yaml`` for existing candidate directories.

    Goal §3 + §9: every candidate needs a spec.  ``audit init`` covers
    new candidates; this surface covers the legacy directories that
    pre-date the spec template.  Default is dry-run — lists which dirs
    would receive a stub.  Pass ``apply=True`` to actually create them.

    Skipped names:
      * starts with ``_`` (``_templates``, ``__pycache__``, ``_archive``)
      * not a directory
      * already has ``spec.yaml``

    Each stub copies the template and substitutes ``strategy_name`` to
    the directory name.  spec_check is run; FAIL is recorded but does
    not abort the backfill (legacy candidates are expected to need
    hypothesis / entry_rule / etc. filled in by hand).
    """
    from hft_platform.alpha import spec_check

    root_path = Path(root)
    template_path = Path(template)
    if not template_path.is_file():
        return f"refused: template not found at {template_path}"
    if not root_path.is_dir():
        return f"refused: alphas root not found at {root_path}"

    candidates: list[Path] = sorted(
        p for p in root_path.iterdir() if p.is_dir() and not p.name.startswith("_")
    )
    missing: list[Path] = [c for c in candidates if not (c / "spec.yaml").is_file()]
    if not missing:
        return f"no missing specs under {root_path} ({len(candidates)} candidate dirs scanned)"

    lines: list[str] = []
    lines.append(f"scanned {len(candidates)} candidate dirs under {root_path}")
    lines.append(f"missing spec.yaml: {len(missing)}")
    lines.append("mode    : " + ("APPLY (will write files)" if apply else "DRY-RUN (no changes)"))

    failed_specs = 0
    for c in missing:
        target = c / "spec.yaml"
        if not apply:
            lines.append(f"  [dry-run] would scaffold {target}")
            continue
        shutil.copyfile(template_path, target)
        raw = target.read_text(encoding="utf-8")
        if _TEMPLATE_NAME in raw:
            raw = raw.replace(_TEMPLATE_NAME, c.name)
            target.write_text(raw, encoding="utf-8")
        passed, _errors = spec_check.check_one(target)
        marker = "OK" if passed else "spec_check FAIL"
        if not passed:
            failed_specs += 1
        lines.append(f"  [apply ] wrote     {target}  ({marker})")

    if apply:
        lines.append(
            f"summary: {len(missing) - failed_specs} pass / {failed_specs} spec_check FAIL"
            " (legacy stubs expected to need manual fill-in)"
        )
    return "\n".join(lines)


def gates(
    strategy_type: str | None = None,
    *,
    profile: str | None = None,
    top: int | None = None,
) -> str:
    """Per-sub-gate failure-frequency view (research bottleneck dashboard).

    For each sub-gate name present in any matching row, count:
      * ``evaluated`` — rows where the gate ran (sub_gates entry present)
      * ``failed``    — entry with passed == False
      * ``errored``   — entry with passed == None or error == True
      * ``fail_rate`` — failed / evaluated (rendered as %)

    Sorted by failed descending so the top of the table shows which
    gates are killing the most candidates — answers goal §9's "what's
    the kept/killed rationale" at the *gate population* axis instead
    of per-row.  ``--top N`` truncates the list to the worst N.

    Filters (AND-combined): strategy_type, profile.
    """
    rows = sub_gate_audit.read_runs()
    if strategy_type is not None:
        rows = [r for r in rows if r.get("strategy_type") == strategy_type]
    if profile is not None:
        rows = [r for r in rows if r.get("profile_name") == profile]
    if not rows:
        return "no audit rows match filter."

    counters: dict[str, dict[str, int]] = {}
    for row in rows:
        for entry in row.get("sub_gates") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "") or "")
            if not name:
                continue
            bucket = counters.setdefault(name, {"evaluated": 0, "failed": 0, "errored": 0})
            bucket["evaluated"] += 1
            passed = entry.get("passed")
            is_error = bool(entry.get("error"))
            if passed is None or is_error:
                bucket["errored"] += 1
            elif passed is False:
                bucket["failed"] += 1

    if not counters:
        return "no sub-gate entries recorded across matched rows."

    items = sorted(
        counters.items(),
        key=lambda kv: (kv[1]["failed"], kv[1]["errored"], kv[1]["evaluated"]),
        reverse=True,
    )
    if top is not None and top > 0:
        items = items[:top]

    headers = ("sub_gate", "evaluated", "failed", "errored", "fail_rate")
    body: list[tuple[str, ...]] = []
    for name, c in items:
        ev = c["evaluated"]
        rate = (100.0 * c["failed"] / ev) if ev else 0.0
        body.append(
            (
                name[:32],
                str(ev),
                str(c["failed"]),
                str(c["errored"]),
                f"{rate:.1f}%",
            )
        )

    widths = [max(len(h), max(len(b[i]) for b in body)) for i, h in enumerate(headers)]

    def _fmt(cells: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    out: list[str] = [_fmt(headers), _fmt(tuple("-" * w for w in widths))]
    out.extend(_fmt(c) for c in body)
    out.append(f"({len(body)} sub-gate{'s' if len(body) != 1 else ''} across {len(rows)} rows)")
    return "\n".join(out)


def _replay_parity_entry(row: dict) -> dict | None:
    """Locate the replay_parity sub-gate entry in an audit row."""
    for entry in row.get("sub_gates") or []:
        if isinstance(entry, dict) and entry.get("name") == "replay_parity":
            return entry
    return None


def divergence(
    strategy_type: str | None = None,
    *,
    profile: str | None = None,
    category: str | None = None,
    only_failed: bool = False,
) -> str:
    """List replay-parity divergence categorization per audit row.

    Goal §7 / §8 require parity checks and a canonical divergence
    taxonomy.  ``replay_parity`` already records match_pct,
    first_divergence_idx, and a category histogram in each row's
    sub_gates[].metrics.  This view tabulates them so an operator can
    answer "which runs diverge, and what bucket are they in?" at a
    glance — without parsing JSONL.

    Filters (all AND-combined):
      * ``strategy_type``  — maker / taker
      * ``profile``        — exact profile_name match
      * ``category``       — keep rows whose dominant_divergence_category
                             equals this value (e.g. ``data_mismatch``)
      * ``only_failed``    — keep rows where the gate did not pass

    Output columns: run_id | strategy | match_pct | first_idx |
    dominant_category | top_categories.
    """
    rows = sub_gate_audit.read_runs()
    if strategy_type is not None:
        rows = [r for r in rows if r.get("strategy_type") == strategy_type]
    if profile is not None:
        rows = [r for r in rows if r.get("profile_name") == profile]

    body: list[tuple[str, ...]] = []
    for row in rows:
        entry = _replay_parity_entry(row)
        if entry is None:
            continue
        metrics = entry.get("metrics") or {}
        passed = entry.get("passed")
        if only_failed and passed is not False:
            continue
        dominant = str(metrics.get("dominant_divergence_category", "") or "")
        if category is not None and dominant != category:
            continue
        match_pct = metrics.get("match_pct")
        match_str = f"{float(match_pct):.2f}" if isinstance(match_pct, (int, float)) else "n/a"
        first_idx = metrics.get("first_divergence_idx")
        idx_str = (
            f"{int(float(first_idx))}"
            if isinstance(first_idx, (int, float)) and float(first_idx) >= 0
            else "(none)"
        )
        cats_dict = metrics.get("divergence_categories") or {}
        if isinstance(cats_dict, dict) and cats_dict:
            top = sorted(cats_dict.items(), key=lambda kv: kv[1], reverse=True)[:3]
            top_str = ",".join(f"{k}={v}" for k, v in top)
        else:
            top_str = ""
        body.append(
            (
                str(row.get("run_id", ""))[:24],
                str(row.get("strategy_name", ""))[:24],
                match_str,
                idx_str,
                dominant or "(none)",
                top_str,
            )
        )

    if not body:
        return "no audit rows match filter."

    headers = (
        "run_id",
        "strategy",
        "match_pct",
        "first_idx",
        "dominant_category",
        "top_categories",
    )
    widths = [
        max(len(h), max(len(b[i]) for b in body)) for i, h in enumerate(headers)
    ]

    def _fmt(cells: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    out: list[str] = [_fmt(headers), _fmt(tuple("-" * w for w in widths))]
    out.extend(_fmt(c) for c in body)
    out.append(f"({len(body)} row{'s' if len(body) != 1 else ''})")
    return "\n".join(out)


_EXPORT_COLUMNS: tuple[str, ...] = (
    "run_id",
    "strategy_name",
    "instrument",
    "strategy_type",
    "profile_name",
    "blocking_passed",
    "triage_status",
    "mean_net_edge_pts_per_trade",
    "force_flat_trip_share_pct",
    "single_day_dominance_pct",
    "median_monthly_net_pnl_pts",
    "worst_monthly_pnl_pts",
    "sample_adequacy_label",
    "promotion_ready",
    "promotion_blockers",
    "triage_reason",
    "spec_provenance_complete",
    "spec_fields_traceable",
    "data_range",
    "cost_model_id",
)


def _export_row(row: dict) -> dict[str, str]:
    """Project a JSONL row onto the flat export schema (``_EXPORT_COLUMNS``).

    ``data_range`` and ``cost_model_id`` are lifted from ``spec_provenance``
    when present so external records (Markdown notebook, CSV in a Linear
    ticket) carry the spec context that drove the audit verdict.  The
    ``promotion_ready`` / ``promotion_blockers`` columns carry the Round 51
    composite verdict so the kept/killed answer (驗證標準 §9) travels with
    the row.
    """
    prov = row.get("spec_provenance") or {}
    edge = row.get("mean_net_edge_pts_per_trade")
    edge_str = f"{float(edge):.6f}" if isinstance(edge, (int, float)) else ""
    ff = row.get("force_flat_trip_share_pct")
    ff_str = f"{float(ff):.4f}" if isinstance(ff, (int, float)) else ""
    dom = row.get("single_day_dominance_pct")
    dom_str = f"{float(dom):.4f}" if isinstance(dom, (int, float)) else ""
    med_month = row.get("median_monthly_net_pnl_pts")
    med_month_str = f"{float(med_month):.4f}" if isinstance(med_month, (int, float)) else ""
    worst_month = row.get("worst_monthly_pnl_pts")
    worst_month_str = (
        f"{float(worst_month):.4f}" if isinstance(worst_month, (int, float)) else ""
    )
    sample_label = row.get("sample_adequacy_label")
    sample_str = sample_label if isinstance(sample_label, str) else ""
    ready, blockers = promotion_readiness(row)
    spec_ok, _ = spec_provenance_complete(row)
    traceable_fields, _untraceable = spec_field_audit(row)
    blk = row.get("blocking_passed")
    blk_str = "" if blk is None else ("true" if blk else "false")
    return {
        "run_id": str(row.get("run_id", "")),
        "strategy_name": str(row.get("strategy_name", "")),
        "instrument": str(row.get("instrument", "")),
        "strategy_type": str(row.get("strategy_type", "")),
        "profile_name": str(row.get("profile_name", "")),
        "blocking_passed": blk_str,
        "triage_status": str(row.get("triage_status", "") or ""),
        "mean_net_edge_pts_per_trade": edge_str,
        "force_flat_trip_share_pct": ff_str,
        "single_day_dominance_pct": dom_str,
        "median_monthly_net_pnl_pts": med_month_str,
        "worst_monthly_pnl_pts": worst_month_str,
        "sample_adequacy_label": sample_str,
        "promotion_ready": "true" if ready else "false",
        "promotion_blockers": ";".join(blockers),
        "triage_reason": triage_reason(row),
        "spec_provenance_complete": "true" if spec_ok else "false",
        "spec_fields_traceable": f"{len(traceable_fields)}/{len(_SPEC_FIELDS_3)}",
        "data_range": str(prov.get("data_range", "") if isinstance(prov, dict) else ""),
        "cost_model_id": str(prov.get("cost_model_id", "") if isinstance(prov, dict) else ""),
    }


def export(
    fmt: str = "csv",
    *,
    strategy_type: str | None = None,
    profile: str | None = None,
    edge_min: float | None = None,
    only_passing: bool = False,
    max_force_flat_share: float | None = None,
    max_day_dominance: float | None = None,
    only_ready: bool = False,
) -> str:
    """Emit audit rows as CSV or Markdown for external experiment records.

    Goal §4 / §9: every experiment should leave a traceable record.
    show / list / summary cover the human-readable side; this surface
    exports the same row set into formats that paste into a Linear
    ticket, Notion notebook, or feed an Excel pivot directly — no
    JSONL parsing required on the receiving end.

    The ``force_flat_trip_share_pct`` column travels with the edge so the
    edge-credibility signal (驗證標準 §2/§3: residual MtM must not inflate
    edge) survives into exported review artifacts.  ``max_force_flat_share``
    drops rows whose share strictly exceeds the bound — combine with
    ``edge_min`` to export only candidates whose edge is both high *and*
    not propped up by force-flat marks.  Rows missing the metric are kept
    (the gate simply didn't run); use ``force-flat`` to inspect offenders.

    ``single_day_dominance_pct`` (驗證標準 §5) travels the same way, with a
    parallel ``max_day_dominance`` filter, so the full edge-credibility +
    distribution-dominance signal set survives into exported artifacts.

    The Round 51 composite verdict travels as ``promotion_ready`` /
    ``promotion_blockers`` columns; ``only_ready`` restricts the export to
    the deployment-ready cohort (every credibility axis + blocking clear),
    so a reviewer can export exactly the kept set (驗證標準 §9).

    Filters mirror ``list_runs`` plus ``max_force_flat_share``,
    ``max_day_dominance`` and ``only_ready``.
    """
    if fmt not in ("csv", "md"):
        raise ValueError(f"unsupported export fmt: {fmt!r} (want 'csv' or 'md')")

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
    if max_force_flat_share is not None:
        # Keep rows missing the metric (gate didn't run); drop only those
        # whose recorded share strictly exceeds the bound.
        rows = [
            r
            for r in rows
            if not isinstance(r.get("force_flat_trip_share_pct"), (int, float))
            or float(r["force_flat_trip_share_pct"]) <= max_force_flat_share
        ]
    if max_day_dominance is not None:
        # Same keep-missing-metric semantics as max_force_flat_share.
        rows = [
            r
            for r in rows
            if not isinstance(r.get("single_day_dominance_pct"), (int, float))
            or float(r["single_day_dominance_pct"]) <= max_day_dominance
        ]
    if only_ready:
        rows = [r for r in rows if promotion_readiness(r)[0]]

    projected = [_export_row(r) for r in rows]

    if fmt == "csv":
        import csv
        import io

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(_EXPORT_COLUMNS))
        writer.writeheader()
        for p in projected:
            writer.writerow(p)
        return buf.getvalue().rstrip("\n")

    # md: GFM table.
    lines: list[str] = []
    lines.append("| " + " | ".join(_EXPORT_COLUMNS) + " |")
    lines.append("|" + "|".join(["---"] * len(_EXPORT_COLUMNS)) + "|")
    for p in projected:
        cells = [p[c].replace("|", r"\|") for c in _EXPORT_COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


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
    # Round 45: force-flat residual aggregation so reviewers can spot
    # whether the edge cohort is dominated by inventory-mark artifacts.
    ff_shares: list[float] = [
        float(r["force_flat_trip_share_pct"])
        for r in rows
        if isinstance(r.get("force_flat_trip_share_pct"), (int, float))
    ]
    above_ff_cap = sum(1 for s in ff_shares if s > 30.0)
    lines.append("force_flat_residual (strict cap 30.0% of trips):")
    lines.append(f"  rows with metric: {len(ff_shares)} / {total}")
    lines.append(f"  rows over cap   : {above_ff_cap}")
    if ff_shares:
        lines.append(
            f"  share p50/p95   : {_percentile(ff_shares, 50):.1f}% / {_percentile(ff_shares, 95):.1f}%"
        )
        lines.append(
            f"  share min/max   : {min(ff_shares):.1f}% / {max(ff_shares):.1f}%"
        )
    # Round 49: single-day-dominance aggregation (驗證標準 §5) so reviewers
    # can spot whether the cohort's edge is carried by one trading day.
    dom_shares: list[float] = [
        float(r["single_day_dominance_pct"])
        for r in rows
        if isinstance(r.get("single_day_dominance_pct"), (int, float))
    ]
    above_dom_cap = sum(1 for s in dom_shares if s > 25.0)
    lines.append("single_day_dominance (strict cap 25.0% of |total|):")
    lines.append(f"  rows with metric: {len(dom_shares)} / {total}")
    lines.append(f"  rows over cap   : {above_dom_cap}")
    if dom_shares:
        lines.append(
            f"  share p50/p95   : {_percentile(dom_shares, 50):.1f}% / {_percentile(dom_shares, 95):.1f}%"
        )
        lines.append(
            f"  share min/max   : {min(dom_shares):.1f}% / {max(dom_shares):.1f}%"
        )
    # Round 54: single-month-dominance aggregation (驗證標準 §6 "單月收益支配性")
    # — the monthly analogue, so reviewers spot a cohort whose edge leans on
    # one calendar month rather than a durable monthly stream.
    month_shares: list[float] = [
        float(r["top_month_contribution_pct"])
        for r in rows
        if isinstance(r.get("top_month_contribution_pct"), (int, float))
    ]
    above_month_cap = sum(1 for s in month_shares if s > 50.0)
    lines.append("top_month_dominance (strict cap 50.0% of net):")
    lines.append(f"  rows with metric: {len(month_shares)} / {total}")
    lines.append(f"  rows over cap   : {above_month_cap}")
    if month_shares:
        lines.append(
            f"  share p50/p95   : {_percentile(month_shares, 50):.1f}% / {_percentile(month_shares, 95):.1f}%"
        )
        lines.append(
            f"  share min/max   : {min(month_shares):.1f}% / {max(month_shares):.1f}%"
        )
    # Round 50: sample-adequacy distribution (驗證標準 §4) — how many
    # candidates are actually deployment-ready vs must stay flagged
    # promising / needs_more_sample / inconclusive.
    sample_counts: dict[str, int] = {}
    for r in rows:
        lbl = r.get("sample_adequacy_label")
        if isinstance(lbl, str):
            sample_counts[lbl] = sample_counts.get(lbl, 0) + 1
    rows_with_label = sum(sample_counts.values())
    lines.append("sample_adequacy (驗證標準 §4, only 'adequate' is deployment-ready):")
    lines.append(f"  rows with label : {rows_with_label} / {total}")
    for lbl in ("adequate", "promising", "needs_more_sample", "inconclusive"):
        if lbl in sample_counts:
            lines.append(f"  {lbl:16s}: {sample_counts[lbl]}")
    # Round 51: composite promotion verdict count (驗證標準 §9) — how many
    # candidates clear *every* credibility axis plus blocking at once.
    ready_count = sum(1 for r in rows if promotion_readiness(r)[0])
    lines.append("promotion_readiness (all axes + blocking clear):")
    lines.append(f"  READY rows     : {ready_count} / {total}")
    # Round 59: cohort-level traceability gap (goal §4) — distinct from the
    # credibility distribution; how many rows carry a complete audit trail
    # (data_range / cost_model_id / required_gates) vs are missing provenance.
    complete_count = 0
    missing_key_counts: dict[str, int] = {}
    for r in rows:
        ok, missing = spec_provenance_complete(r)
        if ok:
            complete_count += 1
        for key in missing:
            missing_key_counts[key] = missing_key_counts.get(key, 0) + 1
    lines.append("spec_provenance (goal §4 traceability):")
    lines.append(f"  complete rows  : {complete_count} / {total}")
    if missing_key_counts:
        top_key, top_n = max(missing_key_counts.items(), key=lambda kv: kv[1])
        lines.append(f"  top missing key: {top_key} ({top_n})")
    # Round 73: cohort-level §3 fixed-spec field traceability — how many of
    # the 12 完成狀態 §3 fields the audit record itself can attest to, summed
    # across the cohort.  Distinct from spec_provenance completeness (which is
    # the §4 audit-trail triple): this is the §3 field-coverage distribution,
    # built from spec_field_audit per row so a reviewer sees whether the
    # cohort's records stand on their own or lean on spec.yaml.
    coverage_counts: dict[int, int] = {}
    for r in rows:
        traceable, _untraceable = spec_field_audit(r)
        n = len(traceable)
        coverage_counts[n] = coverage_counts.get(n, 0) + 1
    lines.append(
        f"spec_fields (完成狀態 §3, attestable from record / {len(_SPEC_FIELDS_3)}):"
    )
    for n in sorted(coverage_counts, reverse=True):
        lines.append(f"  {n:2d}/{len(_SPEC_FIELDS_3)} traceable: {coverage_counts[n]}")
    # Round 65: triage-reason distribution (驗證標準 §9 比較策略) — how the
    # cohort splits across the kept/killed vocabulary, derived from the
    # composite verdict rather than the raw blocking triage_status.
    triage_reason_counts: dict[str, int] = {}
    for r in rows:
        reason = triage_reason(r)
        triage_reason_counts[reason] = triage_reason_counts.get(reason, 0) + 1
    lines.append("triage_reason distribution (迭代規則 §5 vocabulary):")
    for reason in (
        "promotable",
        "failed",
        "needs_more_sample",
        "blocked_by_parity",
        "blocked_by_risk",
        "blocked_by_audit",
    ):
        if reason in triage_reason_counts:
            lines.append(f"  {reason:18s}: {triage_reason_counts[reason]}")
    lines.append("triage_status distribution:")
    for key in sorted(triage_counts):
        lines.append(f"  {key:24s}: {triage_counts[key]}")
    return "\n".join(lines)


def force_flat_offenders(
    strategy_type: str | None = None,
    *,
    profile: str | None = None,
    min_share: float = 30.0,
) -> str:
    """List the specific runs whose edge is propped up by force-flat marks.

    ``summary`` aggregates the ``force_flat_trip_share_pct`` distribution;
    this view names the offending ``run_id``s so a reviewer can jump
    straight to the cohort whose ``mean_net_edge_pts_per_trade`` may be an
    inventory-mark artifact rather than tradeable edge (驗證標準 §2/§3:
    residual MtM must not inflate edge).  Only rows whose share strictly
    exceeds ``min_share`` (default = strict 30.0% cap) are shown, sorted by
    share descending (worst inflation first).

    Filters (AND-combined): strategy_type, profile.
    """
    rows = sub_gate_audit.read_runs()
    if strategy_type is not None:
        rows = [r for r in rows if r.get("strategy_type") == strategy_type]
    if profile is not None:
        rows = [r for r in rows if r.get("profile_name") == profile]
    if not rows:
        return "no audit rows match filter."

    offenders: list[tuple[float, dict]] = []
    for row in rows:
        share = row.get("force_flat_trip_share_pct")
        if not isinstance(share, (int, float)):
            continue
        if float(share) > min_share:
            offenders.append((float(share), row))

    if not offenders:
        return (
            f"no rows over force_flat cap ({min_share:.1f}% of trips) "
            f"across {len(rows)} matched rows."
        )

    offenders.sort(key=lambda kv: kv[0], reverse=True)

    headers = ("run_id", "strategy_name", "instrument", "type", "mean_net_edge", "ff_share")
    body: list[tuple[str, ...]] = []
    for share, row in offenders:
        edge = row.get("mean_net_edge_pts_per_trade")
        edge_cell = f"{float(edge):.3f}" if isinstance(edge, (int, float)) else "(n/a)"
        body.append(
            (
                str(row.get("run_id", ""))[:28],
                str(row.get("strategy_name", ""))[:24],
                str(row.get("instrument", ""))[:12],
                str(row.get("strategy_type", ""))[:6],
                edge_cell,
                f"{share:.1f}%",
            )
        )

    widths = [max(len(h), max(len(b[i]) for b in body)) for i, h in enumerate(headers)]

    def _fmt(cells: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    out: list[str] = [_fmt(headers), _fmt(tuple("-" * w for w in widths))]
    out.extend(_fmt(c) for c in body)
    out.append(
        f"({len(offenders)} over cap {min_share:.1f}% of {len(rows)} matched rows)"
    )
    return "\n".join(out)


def loss_concentration_offenders(
    strategy_type: str | None = None,
    *,
    profile: str | None = None,
    min_share: float = _WORST_LOSS_SHARE_CAP_PCT,
) -> str:
    """List runs whose loss distribution is dominated by a few large losses.

    驗證標準 §5 requires OOS to check the loss distribution (虧損分布) — not
    only whether average edge clears the bar, but whether that edge survives a
    handful of outsized losing trades.  ``summary`` already surfaces the
    cohort context; this view names the offending ``run_id``s, mirroring
    ``force_flat_offenders``, so a reviewer can jump straight to the cohorts
    whose ``worst_loss_share_pct`` exceeds the strict 50.0% cap (one losing
    trade accounting for over half the gross loss).  Sorted by share
    descending (most concentrated first).

    Filters (AND-combined): strategy_type, profile.
    """
    rows = sub_gate_audit.read_runs()
    if strategy_type is not None:
        rows = [r for r in rows if r.get("strategy_type") == strategy_type]
    if profile is not None:
        rows = [r for r in rows if r.get("profile_name") == profile]
    if not rows:
        return "no audit rows match filter."

    offenders: list[tuple[float, dict]] = []
    for row in rows:
        share = row.get("worst_loss_share_pct")
        if not isinstance(share, (int, float)):
            continue
        if float(share) > min_share:
            offenders.append((float(share), row))

    if not offenders:
        return (
            f"no rows over worst-loss-share cap ({min_share:.1f}% of gross loss) "
            f"across {len(rows)} matched rows."
        )

    offenders.sort(key=lambda kv: kv[0], reverse=True)

    headers = ("run_id", "strategy_name", "instrument", "type", "mean_net_edge", "worst_loss_share")
    body: list[tuple[str, ...]] = []
    for share, row in offenders:
        edge = row.get("mean_net_edge_pts_per_trade")
        edge_cell = f"{float(edge):.3f}" if isinstance(edge, (int, float)) else "(n/a)"
        body.append(
            (
                str(row.get("run_id", ""))[:28],
                str(row.get("strategy_name", ""))[:24],
                str(row.get("instrument", ""))[:12],
                str(row.get("strategy_type", ""))[:6],
                edge_cell,
                f"{share:.1f}%",
            )
        )

    widths = [max(len(h), max(len(b[i]) for b in body)) for i, h in enumerate(headers)]

    def _fmt(cells: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    out: list[str] = [_fmt(headers), _fmt(tuple("-" * w for w in widths))]
    out.extend(_fmt(c) for c in body)
    out.append(
        f"({len(offenders)} over cap {min_share:.1f}% of {len(rows)} matched rows)"
    )
    return "\n".join(out)


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

    vs_p = sub.add_parser(
        "verify-spec",
        help="Per-field fill-state for one or all candidate specs.",
    )
    vs_p.add_argument("alpha_id", nargs="?", default=None)
    vs_p.add_argument("--root", default=str(_DEFAULT_ALPHAS_ROOT))
    vs_p.add_argument("--all", dest="all_specs", action="store_true")

    bf_p = sub.add_parser(
        "backfill-specs",
        help="Scaffold spec.yaml stubs for existing candidate dirs missing one.",
    )
    bf_p.add_argument("--root", default=str(_DEFAULT_ALPHAS_ROOT))
    bf_p.add_argument("--template", default=str(_DEFAULT_TEMPLATE))
    bf_p.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the stubs (default: dry-run).",
    )

    init_p = sub.add_parser(
        "init",
        help="Scaffold a new candidate directory + spec.yaml from the template.",
    )
    init_p.add_argument("alpha_id", help="Directory name under research/alphas/.")
    init_p.add_argument("--strategy-name", default=None, help="Defaults to alpha_id.")
    init_p.add_argument("--root", default=str(_DEFAULT_ALPHAS_ROOT))
    init_p.add_argument(
        "--template",
        default=None,
        help="Explicit template path; mutually exclusive with --shape.",
    )
    init_p.add_argument(
        "--shape",
        default=None,
        choices=sorted(_SHAPE_TEMPLATES),
        help="Pick exemplar by shape tag instead of a literal path.",
    )
    init_p.add_argument("--force", action="store_true")

    gat_p = sub.add_parser(
        "gates",
        help="Per-sub-gate failure-frequency view across audit rows.",
    )
    gat_p.add_argument("--strategy-type", choices=("maker", "taker"), default=None)
    gat_p.add_argument("--profile", default=None)
    gat_p.add_argument("--top", type=int, default=None, help="Truncate to worst N gates.")

    div_p = sub.add_parser(
        "divergence",
        help="List replay-parity divergence categorization per row.",
    )
    div_p.add_argument("--strategy-type", choices=("maker", "taker"), default=None)
    div_p.add_argument("--profile", default=None)
    div_p.add_argument(
        "--category",
        default=None,
        help="Restrict to rows whose dominant_divergence_category matches.",
    )
    div_p.add_argument(
        "--only-failed",
        action="store_true",
        help="Only rows where the replay_parity sub-gate failed.",
    )

    exp_p = sub.add_parser("export", help="Export audit rows as CSV or Markdown.")
    exp_p.add_argument("--fmt", choices=("csv", "md"), default="csv")
    exp_p.add_argument("--strategy-type", choices=("maker", "taker"), default=None)
    exp_p.add_argument("--profile", default=None)
    exp_p.add_argument("--edge-min", type=float, default=None)
    exp_p.add_argument("--only-passing", action="store_true")
    exp_p.add_argument(
        "--max-force-flat-share",
        type=float,
        default=None,
        help="Drop rows whose force_flat_trip_share_pct strictly exceeds this "
        "(rows missing the metric are kept).",
    )
    exp_p.add_argument(
        "--max-day-dominance",
        type=float,
        default=None,
        help="Drop rows whose single_day_dominance_pct strictly exceeds this "
        "(rows missing the metric are kept).",
    )
    exp_p.add_argument(
        "--only-ready",
        action="store_true",
        help="Restrict to promotion-ready rows (all credibility axes + "
        "blocking clear).",
    )

    sum_p = sub.add_parser("summary", help="Aggregate counts across audit rows.")
    sum_p.add_argument("--strategy-type", choices=("maker", "taker"), default=None)
    sum_p.add_argument("--profile", default=None, help="Exact profile_name filter.")

    ff_p = sub.add_parser(
        "force-flat",
        help="List runs whose edge is propped up by force-flat marks (over cap).",
    )
    ff_p.add_argument("--strategy-type", choices=("maker", "taker"), default=None)
    ff_p.add_argument("--profile", default=None, help="Exact profile_name filter.")
    ff_p.add_argument(
        "--min-share",
        type=float,
        default=30.0,
        help="Show rows whose force_flat_trip_share_pct strictly exceeds this "
        "(default: strict 30.0%% cap).",
    )

    lc_p = sub.add_parser(
        "loss-concentration",
        help="List runs whose loss distribution is dominated by a few large losses (over cap).",
    )
    lc_p.add_argument("--strategy-type", choices=("maker", "taker"), default=None)
    lc_p.add_argument("--profile", default=None, help="Exact profile_name filter.")
    lc_p.add_argument(
        "--min-share",
        type=float,
        default=_WORST_LOSS_SHARE_CAP_PCT,
        help="Show rows whose worst_loss_share_pct strictly exceeds this "
        "(default: strict 50.0%% cap).",
    )

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
    elif args.cmd == "force-flat":
        out = force_flat_offenders(
            strategy_type=args.strategy_type,
            profile=args.profile,
            min_share=args.min_share,
        )
    elif args.cmd == "loss-concentration":
        out = loss_concentration_offenders(
            strategy_type=args.strategy_type,
            profile=args.profile,
            min_share=args.min_share,
        )
    elif args.cmd == "verify-spec":
        out = verify_spec(
            args.alpha_id,
            root=args.root,
            all_specs=args.all_specs,
        )
    elif args.cmd == "backfill-specs":
        out = backfill_specs(
            template=args.template,
            root=args.root,
            apply=args.apply,
        )
    elif args.cmd == "init":
        out = init_candidate(
            args.alpha_id,
            template=args.template,
            shape=args.shape,
            root=args.root,
            strategy_name=args.strategy_name,
            force=args.force,
        )
    elif args.cmd == "gates":
        out = gates(
            strategy_type=args.strategy_type,
            profile=args.profile,
            top=args.top,
        )
    elif args.cmd == "divergence":
        out = divergence(
            strategy_type=args.strategy_type,
            profile=args.profile,
            category=args.category,
            only_failed=args.only_failed,
        )
    elif args.cmd == "export":
        out = export(
            fmt=args.fmt,
            strategy_type=args.strategy_type,
            profile=args.profile,
            edge_min=args.edge_min,
            only_passing=args.only_passing,
            max_force_flat_share=args.max_force_flat_share,
            max_day_dominance=args.max_day_dominance,
            only_ready=args.only_ready,
        )
    else:  # pragma: no cover — argparse already enforces this
        parser.print_usage()
        return 2

    print(out)
    if out.startswith("no audit row") or out.startswith("missing audit row"):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
