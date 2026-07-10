"""Iteration-25 review_hypothesis: t1i K0-i COST PRE-CHECK (review-before-build gate).

Seed: reports/readiness_candidate_literature_refresh_iteration24.json. Anchors _041 Gao et al.
2018 (Market Intraday Momentum), _042 Baltussen-Da 2021 (hedging-demand MIM on index FUTURES),
_043 Lai et al. 2022 (MIM 'predicts positive returns better than negative' => long-only), _044
Li et al. 2020 (ITSM stronger when liquidity LOW / impact HIGH), with _045 (HFT erodes MIM) as
the pre-registered failure-mode anchor.

USER MANDATE (verbatim iter-24): "trade only intradays and only long don't short. find a
momentum factor that find Liquidity and price impact." => intraday-only, LONG-ONLY, momentum
CONDITIONED on price impact / illiquidity (Amihud).

HONEST PRIOR: the UNCONDITIONAL intraday open->close momentum of Gao 2018 on TXF was already
KILLED as T1-D (median net -24, ~62% wrong-signed => it REVERTED). So t1i is a CONDITIONAL
re-examination of the T1-D family: does restricting to the long side AND conditioning on a
HIGH price-impact (illiquid) formation regime isolate a continuation corner that T1-D's pooled
average buried? The bar is therefore HIGHER than for a fresh idea.

K0-i (pre-registered): on existing front-TXF L2, for each formation window T in
{30,60,90 min, rest-of-day}: compute formation return r_f and formation Amihud illiquidity
ILLIQ = |r_f| / formation_volume (price impact per unit volume). KEEP ONLY days with r_f > 0
(LONG-ONLY). Split kept days into illiquidity TERCILES (LOW/MID/HIGH impact). For each
(T x tercile x exit-horizon) cell measure the GROSS long continuation move front_mid(exit) -
front_mid(entry) (mid-to-mid, no spread, no cost -- the MOST GENEROUS bound), and the 8pt-NET
move. PROCEED to build the frozen t1i detector ONLY IF some cell in the HIGH-impact tercile
(the regime the literature predicts) has 8pt-NET MEDIAN > 0 (i.e. gross median > 8pt) AND
frac(gross > 8pt) > 0.5 AND N >= 6. Otherwise STRUCTURAL KILL of the conditioned long version,
never built.

ANTI-TAIL-ARTIFACT: the gate is MEDIAN + frac-beating-cost, NOT mean. A positive MEAN with a
negative net median (the t1g HIGH_up signature: mean +40 / median -24 / pos_frac 0.33) is a
FAIL. The FULL grid is reported so multiple-comparisons across cells are visible.

This module is STANDALONE and ADDITIVE: it imports only stable low-level primitives from
research/t1/regime_viability.py (frame loaders, session/date helpers, NS_PER_SECOND/MINUTE) --
NONE of the t1f/t1g/t1h functions and NONE of the functions the parallel Codex session edits.
It does NOT modify any frozen detector, the production path, or the cost model. Inference is
DESCRIPTIVE-ONLY: no parameter is tuned to maximise PnL; terciles are data-defined splits and
the long-only gate is r_f > 0.

Honesty notes:
  * The reported move is GROSS front-TXF mid-to-mid. The real strategy executes on TMF, crosses
    the spread on both legs, and pays 8pt -> it is strictly worse. "gross median > 8pt" is a
    NECESSARY (not sufficient) condition; failing it is a hard kill.
  * Long-only halves the sample and the tercile split thirds it again, so per-cell N is small
    (~6-10). This is a FEASIBILITY bound, not a promotion claim; N is reported per cell.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from research.t1.regime_viability import (  # stable primitives only
    NS_PER_MINUTE,
    BboFrame,
    _date_from_path,
    _load_frames,
    _session_start_ns,
)

SESSION_MINUTES = 285
WARMUP_MIN = 5  # skip first 5m for the session-start mid to settle
COST_PTS = 8.0
MIN_FORMATION_TRADES = 30  # day must have a real, liquid front contract
MIN_CELL_N = 6
OOS_START = "2026-04-01"
CONTRACTS = ["b6", "c6", "d6", "e6", "f6", "g6"]
RAW_DIR = Path("research/data/raw")
OUT_DIR = Path("research/experiments/validations/t1i_intraday_momentum_impact_v0")

# formation windows in minutes; "rod" = rest-of-day (open -> close-30min)
FORMATIONS = ["30m", "60m", "90m", "rod"]
# exit horizons (minutes held from entry); "close" = force-flat at session close
EXITS_MIN = [30, 60, 120]  # plus an implicit hold-to-close added per formation


def _files_by_date() -> dict[str, dict[str, Path]]:
    out: dict[str, dict[str, Path]] = {}
    for c in CONTRACTS:
        d = RAW_DIR / f"txf{c}"
        if not d.is_dir():
            continue
        for p in d.glob(f"TXF{c.upper()}_*_l2.hftbt.npz"):
            out.setdefault(_date_from_path(p), {})[c] = p
    return out


def _mid_at(bbo: BboFrame, ts: int) -> float:
    idx = int(np.searchsorted(bbo.ts_ns, ts, side="right")) - 1
    if idx < 0:
        return float("nan")
    return float(bbo.mid[idx])


def _pick_front(date: str, files: dict[str, Path], s0: int) -> tuple[str, Path] | None:
    """front = present contract with the most trades in the first 90 minutes."""
    best_c, best_n, best_p = None, -1, None
    f_end = s0 + 90 * NS_PER_MINUTE
    for c, p in files.items():
        _, trades = _load_frames(p)
        n = int(((trades.ts_ns >= s0) & (trades.ts_ns < f_end)).sum())
        if n > best_n:
            best_c, best_n, best_p = c, n, p
    if best_p is None or best_n < MIN_FORMATION_TRADES:
        return None
    return best_c, best_p


def _formation_spec(name: str, s0: int, close: int) -> tuple[int, int]:
    """Return (entry_ts, formation_end_ts==entry_ts). formation window is [s0, entry)."""
    if name == "rod":
        return close - 30 * NS_PER_MINUTE, close - 30 * NS_PER_MINUTE
    minutes = int(name[:-1])
    entry = s0 + minutes * NS_PER_MINUTE
    return entry, entry


def _day_record(bbo: BboFrame, trades, s0: int) -> dict[str, dict] | None:
    """Per-formation: r_f, illiq, and long continuation move to each exit horizon."""
    close = s0 + SESSION_MINUTES * NS_PER_MINUTE
    start = s0 + WARMUP_MIN * NS_PER_MINUTE
    m_start = _mid_at(bbo, start)
    if not np.isfinite(m_start):
        return None
    out: dict[str, dict] = {}
    for name in FORMATIONS:
        entry, _ = _formation_spec(name, s0, close)
        if entry <= start or entry >= close:
            continue
        m_entry = _mid_at(bbo, entry)
        if not np.isfinite(m_entry):
            continue
        r_f = m_entry - m_start  # formation return in points
        vmask = (trades.ts_ns >= start) & (trades.ts_ns < entry)
        vol = float(trades.qty[vmask].sum())
        if vol <= 0:
            continue
        illiq = abs(r_f) / vol  # Amihud: price move per unit volume
        # exit horizons (held from entry), capped at close; always include hold-to-close
        exits: dict[str, float] = {}
        for h in EXITS_MIN:
            ex_ts = min(entry + h * NS_PER_MINUTE, close)
            if ex_ts <= entry:
                continue
            m_ex = _mid_at(bbo, ex_ts)
            if np.isfinite(m_ex):
                exits[f"{h}m"] = m_ex - m_entry  # long continuation (long = +)
        m_close = _mid_at(bbo, close)
        if np.isfinite(m_close) and close > entry:
            exits["close"] = m_close - m_entry
        if not exits:
            continue
        out[name] = {"r_f": r_f, "illiq": illiq, "exits": exits}
    return out or None


def _summ(vals: list[float]) -> dict[str, object]:
    if not vals:
        return {"n": 0}
    a = np.asarray(vals, dtype=np.float64)
    med = float(np.median(a))
    return {
        "n": int(a.size),
        "gross_median": round(med, 2),
        "gross_mean": round(float(a.mean()), 2),
        "net_median": round(med - COST_PTS, 2),
        "pos_frac_gross": round(float((a > 0).mean()), 3),
        "frac_beats_cost": round(float((a > COST_PTS).mean()), 3),
        "p25": round(float(np.quantile(a, 0.25)), 2),
        "p75": round(float(np.quantile(a, 0.75)), 2),
    }


def _collect_records(fbd: dict[str, dict[str, Path]]) -> tuple[dict[str, list[dict]], int]:
    records: dict[str, list[dict]] = {f: [] for f in FORMATIONS}
    n_dates = 0
    for date in sorted(fbd):
        s0 = _session_start_ns(date)
        pick = _pick_front(date, fbd[date], s0)
        if pick is None:
            continue
        front, fpath = pick
        bbo, trades = _load_frames(fpath)
        if len(bbo.ts_ns) == 0:
            continue
        rec = _day_record(bbo, trades, s0)
        if rec is None:
            continue
        n_dates += 1
        is_oos = date >= OOS_START
        for name, d in rec.items():
            records[name].append({"date": date, "front": front, "is_oos": is_oos, **d})
    return records, n_dates


def _split_terciles(long_rows: list[dict]) -> tuple[dict[str, list[dict]], float, float]:
    illiqs = np.asarray([r["illiq"] for r in long_rows], dtype=np.float64)
    q33, q66 = np.quantile(illiqs, [1 / 3, 2 / 3])
    terciles: dict[str, list[dict]] = {"LOW_impact": [], "MID_impact": [], "HIGH_impact": []}
    for r in long_rows:
        if r["illiq"] <= q33:
            terciles["LOW_impact"].append(r)
        elif r["illiq"] > q66:
            terciles["HIGH_impact"].append(r)
        else:
            terciles["MID_impact"].append(r)
    return terciles, float(q33), float(q66)


def _build_grid(records: dict[str, list[dict]]) -> tuple[dict[str, dict], dict[str, int]]:
    grid: dict[str, dict] = {}
    long_counts: dict[str, int] = {}
    exit_keys = [f"{h}m" for h in EXITS_MIN] + ["close"]
    for name in FORMATIONS:
        long_rows = [r for r in records[name] if r["r_f"] > 0]  # LONG-ONLY gate
        long_counts[name] = len(long_rows)
        if len(long_rows) < 3:
            grid[name] = {"long_days": len(long_rows), "note": "too few long days for terciles"}
            continue
        terciles, q33, q66 = _split_terciles(long_rows)
        cell: dict[str, dict] = {}
        for tname, trows in terciles.items():
            per_exit = {
                ek: _summ([r["exits"][ek] for r in trows if ek in r["exits"]]) for ek in exit_keys
            }
            cell[tname] = {
                "n_days": len(trows),
                "median_illiq": round(float(np.median([r["illiq"] for r in trows])), 6) if trows else None,
                "by_exit": per_exit,
            }
        grid[name] = {
            "long_days": len(long_rows),
            "illiq_tercile_thresholds": [round(q33, 6), round(q66, 6)],
            "terciles": cell,
        }
    return grid, long_counts


def main() -> None:
    fbd = _files_by_date()
    records, n_dates = _collect_records(fbd)
    grid, long_counts = _build_grid(records)

    # K0-i verdict: HIGH-impact tercile, any (formation x exit) cell with net_median > 0
    # AND frac_beats_cost > 0.5 AND n >= MIN_CELL_N.
    passing_cells: list[dict] = []
    for name in FORMATIONS:
        g = grid.get(name, {})
        hi = g.get("terciles", {}).get("HIGH_impact", {}) if "terciles" in g else {}
        for ek, s in hi.get("by_exit", {}).items():
            if (
                s.get("n", 0) >= MIN_CELL_N
                and s.get("net_median", -999) > 0
                and s.get("frac_beats_cost", 0) > 0.5
            ):
                passing_cells.append(
                    {"formation": name, "exit": ek, "n": s["n"],
                     "net_median": s["net_median"], "frac_beats_cost": s["frac_beats_cost"]}
                )
    k0i_pass = bool(passing_cells)

    # also surface the single best HIGH-impact cell (by net_median) for transparency
    best = None
    for name in FORMATIONS:
        hi = grid.get(name, {}).get("terciles", {}).get("HIGH_impact", {})
        for ek, s in hi.get("by_exit", {}).items():
            if s.get("n", 0) >= MIN_CELL_N:
                cand = {"formation": name, "exit": ek, **s}
                if best is None or cand["net_median"] > best["net_median"]:
                    best = cand

    result = {
        "schema": "research.t1i_cost_precheck.v1",
        "candidate": "t1i_intraday_momentum_impact_conditioned_long_v0",
        "iteration_index": 25,
        "route": "review_hypothesis",
        "gate": "K0i_cost_precheck",
        "anchors": ["_041", "_042", "_043", "_044", "_045(failure-mode)"],
        "user_mandate": "intraday-only, LONG-ONLY, momentum conditioned on liquidity/price-impact",
        "prior_reconciliation": "conditional re-examination of KILLED T1-D; bar=HIGH-impact long cell net median>0",
        "method": {
            "signal": "formation return r_f and formation Amihud illiquidity ILLIQ=|r_f|/volume",
            "long_only_gate": "keep days with r_f > 0 only",
            "regime_split": "ILLIQ terciles (LOW/MID/HIGH price impact) within each formation, per-window",
            "measure": "GROSS long continuation front_mid(exit)-front_mid(entry); net = gross - 8pt",
            "formations": FORMATIONS,
            "exits_min": EXITS_MIN,
            "exit_close": "force-flat at session close (intraday only, no overnight)",
            "front_rule": "front = most first-90min trades; min 30 formation trades",
            "warmup_min": WARMUP_MIN,
            "detector_changed": False,
            "production_behavior_changed": False,
            "cost_model_changed": False,
            "inference_policy": "descriptive_only_feasibility_bound_no_tuning",
        },
        "front_days_used": n_dates,
        "long_days_by_formation": long_counts,
        "grid": grid,
        "k0i": {
            "rule": ("build only if some HIGH-impact tercile (formation x exit) cell has "
                     "net_median > 0 (gross median > 8pt) AND frac_beats_cost > 0.5 AND n >= 6"),
            "anti_tail_artifact": "gate uses MEDIAN + frac-beats-cost, not mean",
            "passing_cells": passing_cells,
            "best_high_impact_cell": best,
            "k0i_pass": k0i_pass,
        },
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "t1i_cost_precheck.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    compact = {
        "front_days_used": n_dates,
        "long_days_by_formation": long_counts,
        "k0i_pass": k0i_pass,
        "passing_cells": passing_cells,
        "best_high_impact_cell": best,
    }
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
