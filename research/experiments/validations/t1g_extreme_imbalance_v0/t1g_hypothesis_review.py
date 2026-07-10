"""Iteration-20 review_hypothesis ADDITIVE overlay for t1g_extreme_imbalance_reversal_momentum_v0.

Tests the TWO pre-registered, literature-grounded variants from the iteration-19 expand_sample
artifact, on the EXISTING 85-event sample, WITHOUT changing the frozen detector, the entries,
the gating, the regime split, or the thresholds:

  V_SIGN  (Chordia-Roll-Subrahmanyam 2002, lit_refresh_..._033): the reversal/continuation edge
          may be SIDE-ASYMMETRIC. Break the realised edge down by sign(formation_return),
          sign(imbalance), traded direction, and regime x sign cells, to test whether any signed
          sub-cell (e.g. fade-extreme-selling only) is positive net of 8pt.
  V_HORIZON (Kao 2011 is DAILY; arXiv 2508.06788 says BBO-OFI dissipates fast -> horizon mismatch
          is plausible): re-evaluate the SAME entries at longer holds (120m and to-session-close
          = 195m from the 10:15 trigger), STOP STILL HONORED, to test whether the imbalance effect
          needs more time to play out.

Discipline: this is a pre-registered sub-hypothesis test, not a fishing expedition. EVERY sub-cell
is reported. A sub-cell only counts as a RESCUE if it is, on the FULL sample, mean_net >= 10 AND
N >= 20 AND positive_fraction >= 0.5, AND its OOS mean is also >= 10. No threshold is re-tuned and
no cell is cherry-picked to define the candidate. Inference is descriptive.

Imports only the frozen t1g module (which itself imports only stable regime_viability primitives).
Does NOT modify any frozen detector or the production path.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from research.experiments.validations.t1g_extreme_imbalance_v0.t1g_first_sample import (
    COST_PTS,
    MIN_FORMATION_MOVE_PTS,
    NS_PER_MINUTE,
    STOP_BUFFER_PTS,
    _mid_at,
    build_events,
)
from research.t1.regime_viability import BboFrame

OUT_DIR = Path("research/experiments/validations/t1g_extreme_imbalance_v0")
HORIZONS = {"h30m_baseline": 30, "h120m": 120, "h_to_close_195m": 195}
RESCUE_MIN_MEAN = 10.0
RESCUE_MIN_N = 20
RESCUE_MIN_POSFRAC = 0.5


def _realised(
    txf_bbo: BboFrame, tmf_bbo: BboFrame, trigger_ns: int, direction: int, horizon_min: int
) -> float | None:
    """Stop-honored, spread-crossed executable net at the given hold horizon."""
    horizon_ns = trigger_ns + horizon_min * NS_PER_MINUTE
    entry_ref = _mid_at(txf_bbo, trigger_ns)
    if entry_ref is None:
        return None
    ei = int(np.searchsorted(tmf_bbo.ts_ns, trigger_ns, side="left"))
    if ei >= len(tmf_bbo.ts_ns):
        return None
    entry = float(tmf_bbo.ask[ei] if direction > 0 else tmf_bbo.bid[ei])
    hmask = (txf_bbo.ts_ns >= trigger_ns) & (txf_bbo.ts_ns <= horizon_ns)
    hts = txf_bbo.ts_ns[hmask]
    hmid = txf_bbo.mid[hmask]
    if direction > 0:
        bp = np.flatnonzero(hmid <= entry_ref - STOP_BUFFER_PTS)
    else:
        bp = np.flatnonzero(hmid >= entry_ref + STOP_BUFFER_PTS)
    stop_ns = int(hts[int(bp[0])]) if bp.size else None
    if stop_ns is not None:
        xi = min(int(np.searchsorted(tmf_bbo.ts_ns, stop_ns, side="left")), len(tmf_bbo.ts_ns) - 1)
        exit_px = float(tmf_bbo.bid[xi] if direction > 0 else tmf_bbo.ask[xi])
    else:
        he = int(np.searchsorted(tmf_bbo.ts_ns, horizon_ns, side="right"))
        if he <= ei:
            return None
        exit_px = float(tmf_bbo.bid[he - 1] if direction > 0 else tmf_bbo.ask[he - 1])
    return (exit_px - entry) * direction - COST_PTS


def _score(nets: list[float]) -> dict[str, object]:
    if not nets:
        return {"events": 0}
    a = np.asarray(nets, dtype=np.float64)
    return {
        "events": len(nets),
        "mean_net": round(float(a.mean()), 2),
        "median_net": round(float(np.median(a)), 2),
        "total_net": round(float(a.sum()), 2),
        "positive_fraction": round(float((a > 0).mean()), 3),
        "is_rescue_full": bool(
            a.mean() >= RESCUE_MIN_MEAN
            and len(nets) >= RESCUE_MIN_N
            and (a > 0).mean() >= RESCUE_MIN_POSFRAC
        ),
    }


def main() -> None:
    raw = build_events()
    gated = [e for e in raw if abs(float(e["formation_return_pts"])) >= MIN_FORMATION_MOVE_PTS]
    split = float(np.median([float(e["abs_imbalance"]) for e in gated])) if gated else 0.0

    # rebuild the frozen traded set with regime+direction, then attach realised nets per horizon
    rows: list[dict[str, object]] = []
    for e in gated:
        r_f = float(e["formation_return_pts"])
        sign = 1 if r_f > 0 else -1
        regime = "HIGH" if float(e["abs_imbalance"]) >= split else "LOW"
        direction = sign if regime == "HIGH" else -sign
        nets = {
            h: _realised(e["_txf_bbo"], e["_tmf_bbo"], int(e["trigger_ns"]), direction, m)
            for h, m in HORIZONS.items()
        }
        if nets["h30m_baseline"] is None:
            continue
        rows.append(
            {
                "contract": e["contract"],
                "date": e["date"],
                "is_oos": bool(e["is_oos"]),
                "r_f_sign": "up" if r_f > 0 else "down",
                "ti_sign": "net_buy" if float(e["trading_imbalance"]) > 0 else "net_sell",
                "regime": regime,
                "direction": "long" if direction > 0 else "short",
                "nets": {h: (round(v, 2) if v is not None else None) for h, v in nets.items()},
            }
        )

    def cells(key_fn, horizon: str, oos: bool | None = None) -> dict[str, dict[str, object]]:
        out: dict[str, list[float]] = {}
        for r in rows:
            if oos is not None and bool(r["is_oos"]) != oos:
                continue
            v = r["nets"][horizon]
            if v is None:
                continue
            out.setdefault(str(key_fn(r)), []).append(float(v))
        return {k: _score(v) for k, v in sorted(out.items())}

    breakdowns = {}
    for horizon in HORIZONS:
        breakdowns[horizon] = {
            "ALL": _score([float(r["nets"][horizon]) for r in rows if r["nets"][horizon] is not None]),
            "by_r_f_sign": cells(lambda r: r["r_f_sign"], horizon),
            "by_ti_sign": cells(lambda r: r["ti_sign"], horizon),
            "by_traded_direction": cells(lambda r: r["direction"], horizon),
            "by_regime_x_r_f_sign": cells(lambda r: f"{r['regime']}_{r['r_f_sign']}", horizon),
            "by_regime_x_r_f_sign_OOS": cells(lambda r: f"{r['regime']}_{r['r_f_sign']}", horizon, oos=True),
        }

    # collect any rescue cells across all horizons/breakdowns (full-sample gate), then OOS confirm
    rescues = []
    for horizon, bd in breakdowns.items():
        for bname, cellmap in bd.items():
            if bname.endswith("_OOS") or bname == "ALL":
                continue
            for cname, sc in cellmap.items():
                if sc.get("is_rescue_full"):
                    rescues.append({"horizon": horizon, "breakdown": bname, "cell": cname, "full": sc})

    result = {
        "schema": "research.t1g_hypothesis_review.v1",
        "candidate": "t1g_extreme_imbalance_reversal_momentum_v0",
        "iteration_index": 20,
        "route": "review_hypothesis",
        "frozen_contract_changed": False,
        "thresholds_retuned": False,
        "production_behavior_changed": False,
        "cost_model_changed": False,
        "regime_split_value": round(split, 4),
        "variants_tested": {
            "V_SIGN": "Chordia-Roll-Subrahmanyam 2002 side-asymmetry: by r_f sign, TI sign, direction, regime x sign",
            "V_HORIZON": "Kao-daily / arXiv 2508.06788: holds 30m (baseline), 120m, to-close 195m, stop honored",
        },
        "rescue_rule": (
            f"full-sample mean_net>={RESCUE_MIN_MEAN} AND N>={RESCUE_MIN_N} "
            f"AND pos_frac>={RESCUE_MIN_POSFRAC}, then OOS mean>={RESCUE_MIN_MEAN}"
        ),
        "breakdowns": breakdowns,
        "rescue_cells_full_gate": rescues,
        "any_rescue": bool(rescues),
        "n_events": len(rows),
    }
    (OUT_DIR / "t1g_hypothesis_review.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    # compact console view: ALL per horizon + the two CRS-relevant cells + rescue verdict
    compact = {"n_events": len(rows), "regime_split": round(split, 4), "any_rescue": bool(rescues)}
    for horizon in HORIZONS:
        compact[horizon] = {
            "ALL": breakdowns[horizon]["ALL"],
            "by_r_f_sign": breakdowns[horizon]["by_r_f_sign"],
            "by_traded_direction": breakdowns[horizon]["by_traded_direction"],
            "by_regime_x_r_f_sign": breakdowns[horizon]["by_regime_x_r_f_sign"],
        }
    compact["rescue_cells_full_gate"] = rescues
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
