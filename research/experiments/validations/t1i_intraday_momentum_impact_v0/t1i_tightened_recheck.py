"""Iteration-26 review_hypothesis: t1i TIGHTENED K0-i RE-CHECK (build-or-kill controls).

Iteration 25 ran the K0-i cost pre-check. The mechanical gate (some HIGH-impact tercile cell
with net_median>0 AND frac_beats_cost>0.5 AND N>=6) passed at the LETTER (4 cells), but the
full-grid scrutiny refuted it as a small-N multiple-comparisons artifact:
  * NO coherent impact effect -- HIGH-impact is the BEST tercile in only one formation (30m) and
    the WORST / SIGN-REVERSED in the canonical rest-of-day MIM formation (HIGH net -20.5 vs MID
    +5.25).
  * The headline 30m->close HIGH cell has gross_mean +23.68 << gross_median +144.5, p25 -157.5
    (5 of 11 days catastrophic over an unhedged 4.75h long); frac_beats_cost only 0.545.
  * Intra-cell instability: same 30m-HIGH tercile is NEGATIVE at 60m (-6) and 120m (-47.5).
  * Likely drift/selection: choosing r_f>0 up-mornings then holding long to close captures index
    drift on a selected subset; the K0-i did not net out an always-long benchmark.

This module adds the four controls the grid demanded, as a STANDALONE / ADDITIVE descriptive
re-check. It does NOT build a frozen detector, touch the production path, or change the cost
model. It reuses only stable primitives plus my own iteration-25 helpers.

Pre-registered tightened gate -- a HIGH-impact (formation x exit) cell PASSES only if ALL hold:
  (1) N >= 6,
  (2) raw net_median > 0 (gross median > 8pt)  AND  frac_beats_cost > 0.5,
  (3) PROTECTIVE-STOP version still net_median > 0 -- a 15pt momentum-protective stop (truncates
      the catastrophic left tail) does not destroy the edge,
  (4) ALPHA vs ALWAYS-LONG: HIGH-cell gross_median - all-days unconditional gross_median at the
      same exit > 0 (the impact-conditioning + long-selection beats passive intraday drift),
  (5) OOS HOLDS: on dates >= 2026-04-01 the HIGH-cell net_median > 0 (n_oos >= 3, else INSUFF),
  (6) TERCILE COHERENCE: HIGH gross_median >= MID at this exit AND at >=1 adjacent exit
      (a real impact effect is monotone and stable, not a single lucky cell).

If ANY HIGH cell clears all six -> iteration 27 expand_sample (build frozen t1i).
Otherwise -> archive_candidate_set: long-only + price-impact conditioning does NOT rescue
intraday TXF momentum at 8pt, killed alongside T1-D (the 9th structural negative).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from research.experiments.validations.t1i_intraday_momentum_impact_v0.t1i_cost_precheck import (
    COST_PTS,
    EXITS_MIN,
    FORMATIONS,
    MIN_CELL_N,
    OOS_START,
    SESSION_MINUTES,
    WARMUP_MIN,
    _files_by_date,
    _formation_spec,
    _mid_at,
    _pick_front,
    _split_terciles,
    _summ,
)
from research.t1.regime_viability import (  # stable primitives only
    NS_PER_MINUTE,
    BboFrame,
    _load_frames,
    _session_start_ns,
)

STOP_PTS = 15.0  # momentum-protective long stop; truncates the catastrophic left tail
EXIT_KEYS = [f"{h}m" for h in EXITS_MIN] + ["close"]
OUT_DIR = Path("research/experiments/validations/t1i_intraday_momentum_impact_v0")


def _long_pnl_stop(bbo: BboFrame, entry_ts: int, exit_ts: int, m_entry: float) -> float:
    """Long PnL from entry to exit with a 15pt protective stop applied along the mid path."""
    lo = int(np.searchsorted(bbo.ts_ns, entry_ts, side="right"))
    hi = int(np.searchsorted(bbo.ts_ns, exit_ts, side="right"))
    if hi > lo:
        seg_min = float(bbo.mid[lo:hi].min())
        if seg_min <= m_entry - STOP_PTS:
            return -STOP_PTS  # stopped out before natural exit
    m_ex = _mid_at(bbo, exit_ts)
    if not np.isfinite(m_ex):
        return float("nan")
    return m_ex - m_entry


def _day_record(bbo: BboFrame, trades, s0: int) -> dict[str, dict] | None:
    """Per-formation: r_f, illiq, and BOTH raw + stop long continuation to each exit."""
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
        r_f = m_entry - m_start
        vmask = (trades.ts_ns >= start) & (trades.ts_ns < entry)
        vol = float(trades.qty[vmask].sum())
        if vol <= 0:
            continue
        illiq = abs(r_f) / vol
        raw: dict[str, float] = {}
        stop: dict[str, float] = {}
        for h in EXITS_MIN:
            ex_ts = min(entry + h * NS_PER_MINUTE, close)
            if ex_ts <= entry:
                continue
            m_ex = _mid_at(bbo, ex_ts)
            if np.isfinite(m_ex):
                raw[f"{h}m"] = m_ex - m_entry
                stop[f"{h}m"] = _long_pnl_stop(bbo, entry, ex_ts, m_entry)
        m_close = _mid_at(bbo, close)
        if np.isfinite(m_close) and close > entry:
            raw["close"] = m_close - m_entry
            stop["close"] = _long_pnl_stop(bbo, entry, close, m_entry)
        if not raw:
            continue
        out[name] = {"r_f": r_f, "illiq": illiq, "raw": raw, "stop": stop}
    return out or None


def _collect(fbd: dict[str, dict[str, Path]]) -> tuple[dict[str, list[dict]], int]:
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


def _median_or_none(vals: list[float]) -> float | None:
    return round(float(np.median(np.asarray(vals, dtype=np.float64))), 2) if vals else None


def _benchmark_always_long(rows: list[dict]) -> dict[str, float | None]:
    """Unconditional always-long gross median per exit over ALL days (no r_f gate, no tercile)."""
    return {ek: _median_or_none([r["raw"][ek] for r in rows if ek in r["raw"]]) for ek in EXIT_KEYS}


def _tercile_gross_medians(terciles: dict[str, list[dict]]) -> dict[str, dict[str, float | None]]:
    """{exit: {LOW/MID/HIGH: gross_median}} for the monotonicity/coherence test."""
    out: dict[str, dict[str, float | None]] = {}
    for ek in EXIT_KEYS:
        out[ek] = {
            t: _median_or_none([r["raw"][ek] for r in rows if ek in r["raw"]])
            for t, rows in terciles.items()
        }
    return out


def _coherent(terc_med: dict[str, dict[str, float | None]], ek: str) -> bool:
    """HIGH >= MID at ek AND at >= 1 adjacent exit (stable monotone-up impact effect)."""
    def hi_ge_mid(e: str) -> bool:
        m = terc_med.get(e, {})
        h, mid = m.get("HIGH_impact"), m.get("MID_impact")
        return h is not None and mid is not None and h >= mid

    if not hi_ge_mid(ek):
        return False
    i = EXIT_KEYS.index(ek)
    neighbours = [EXIT_KEYS[j] for j in (i - 1, i + 1) if 0 <= j < len(EXIT_KEYS)]
    return any(hi_ge_mid(n) for n in neighbours)


def _eval_high_cells(records: dict[str, list[dict]]) -> tuple[dict[str, dict], list[dict]]:
    grid: dict[str, dict] = {}
    passing: list[dict] = []
    for name in FORMATIONS:
        long_rows = [r for r in records[name] if r["r_f"] > 0]
        if len(long_rows) < 3:
            grid[name] = {"long_days": len(long_rows), "note": "too few long days"}
            continue
        terciles, q33, q66 = _split_terciles(long_rows)
        bench = _benchmark_always_long(records[name])  # all days, unconditional
        terc_med = _tercile_gross_medians(terciles)
        hi_rows = terciles["HIGH_impact"]
        by_exit: dict[str, dict] = {}
        for ek in EXIT_KEYS:
            raw_vals = [r["raw"][ek] for r in hi_rows if ek in r["raw"]]
            stop_vals = [r["stop"][ek] for r in hi_rows if ek in r["stop"]]
            oos_raw = [r["raw"][ek] for r in hi_rows if ek in r["raw"] and r["is_oos"]]
            raw_s, stop_s = _summ(raw_vals), _summ(stop_vals)
            n = raw_s.get("n", 0)
            hi_gross = raw_s.get("gross_median")
            alpha_vs_always_long = (
                round(hi_gross - bench[ek], 2)
                if (hi_gross is not None and bench.get(ek) is not None)
                else None
            )
            oos_summ = _summ(oos_raw)
            cell = {
                "n": n,
                "raw": raw_s,
                "stop": stop_s,
                "always_long_bench_gross_median": bench.get(ek),
                "alpha_vs_always_long_gross": alpha_vs_always_long,
                "oos": {"n": oos_summ.get("n", 0), "net_median": oos_summ.get("net_median")},
                "tercile_coherent": _coherent(terc_med, ek),
            }
            by_exit[ek] = cell
            checks = {
                "n_ok": n >= MIN_CELL_N,
                "raw_net_pos": raw_s.get("net_median", -999) > 0,
                "raw_beats_cost": raw_s.get("frac_beats_cost", 0) > 0.5,
                "stop_net_pos": stop_s.get("net_median", -999) > 0,
                "beats_always_long": (alpha_vs_always_long or -999) > 0,
                "oos_holds": oos_summ.get("n", 0) >= 3 and oos_summ.get("net_median", -999) > 0,
                "coherent": cell["tercile_coherent"],
            }
            if all(checks.values()):
                passing.append({"formation": name, "exit": ek, **{k: cell[k] for k in
                               ("n", "raw", "stop", "alpha_vs_always_long_gross", "oos")}})
        grid[name] = {
            "long_days": len(long_rows),
            "illiq_tercile_thresholds": [round(q33, 6), round(q66, 6)],
            "tercile_gross_median_by_exit": terc_med,
            "always_long_bench_gross_median": bench,
            "HIGH_impact_by_exit": by_exit,
        }
    return grid, passing


def main() -> None:
    fbd = _files_by_date()
    records, n_dates = _collect(fbd)
    grid, passing = _eval_high_cells(records)
    build_pass = bool(passing)
    result = {
        "schema": "research.t1i_tightened_recheck.v1",
        "candidate": "t1i_intraday_momentum_impact_conditioned_long_v0",
        "iteration_index": 26,
        "route": "review_hypothesis",
        "gate": "K0i_tightened (monotonicity + always-long benchmark + OOS split + 15pt stop)",
        "stop_pts": STOP_PTS,
        "cost_pts": COST_PTS,
        "controls": {
            "protective_stop": f"{STOP_PTS}pt momentum-protective long stop on the mid path",
            "drift_control": "alpha vs unconditional always-long gross median (same exit)",
            "oos_split": f">= {OOS_START}",
            "coherence": "HIGH>=MID at exit AND >=1 adjacent exit",
        },
        "tightened_gate_rule": (
            "HIGH-impact cell passes only if N>=6 AND raw net_median>0 AND frac_beats_cost>0.5 "
            "AND stop net_median>0 AND alpha_vs_always_long>0 AND OOS net_median>0 (n_oos>=3) "
            "AND tercile coherent"
        ),
        "detector_changed": False,
        "production_behavior_changed": False,
        "cost_model_changed": False,
        "front_days_used": n_dates,
        "grid": grid,
        "passing_cells": passing,
        "build_pass": build_pass,
        "verdict_route": "expand_sample" if build_pass else "archive_candidate_set",
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "t1i_tightened_recheck.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    compact = {
        "front_days_used": n_dates,
        "build_pass": build_pass,
        "passing_cells": passing,
        "verdict_route": result["verdict_route"],
        "high_impact_summary": {
            name: {
                ek: {
                    "n": c["n"],
                    "raw_net_med": c["raw"].get("net_median"),
                    "stop_net_med": c["stop"].get("net_median"),
                    "alpha_vs_long": c["alpha_vs_always_long_gross"],
                    "oos_net_med": c["oos"]["net_median"],
                    "coherent": c["tercile_coherent"],
                }
                for ek, c in grid[name].get("HIGH_impact_by_exit", {}).items()
            }
            for name in FORMATIONS
            if "HIGH_impact_by_exit" in grid.get(name, {})
        },
    }
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
