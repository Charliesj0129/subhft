"""Iteration-22 review_hypothesis: t1h COST PRE-CHECK (review-before-build gate).

Seed: reports/readiness_candidate_literature_refresh_iteration21.json, primary anchor
_036 Li-Chen-Liu 2025 (arXiv:2501.03171) -- on CFFEX index futures the near-month leads
the next-month, and the short-term TREND theta of the calendar spread S = F_near - F_next
exerts a NEGATIVE FEEDBACK on the leading (near) contract's own subsequent return (a
contrarian, relative-value signal). The strategy is profitable IS+OOS on CFFEX but the
edge is HFT-scale (~1 price tick per trade).

The iteration-21 artifact pre-registered the DOMINANT a-priori failure mode as
COST-NULLIFICATION: our all-in cost is 8 INDEX POINTS on the TMF leg, so the contrarian
move on the near contract must exceed ~8pt at SOME tradeable horizon for the family to be
viable at all. This module is the cheap gate that answers exactly that, BEFORE any frozen
detector is built.

K0 (pre-registered): across a horizon ladder (5s / 30s / 2m / 10m), measure the
distribution of the post-trigger CONTRARIAN near-contract mid-move (gross, no spread, no
cost -- the most generous possible bound). PROCEED to build a frozen t1h detector ONLY if
some horizon shows a median contrarian gross move > 8pt with a stable sign (pos_frac > 0.5)
AND adequate next-month liquidity. Otherwise t1h is a STRUCTURAL COST KILL.

This module is STANDALONE and ADDITIVE. It imports only stable low-level primitives from
research/t1/regime_viability.py (frame loaders, session/date helpers, NS_PER_SECOND/MINUTE)
-- NONE of the t1f functions and NONE of the functions the parallel Codex session edits. It
does NOT modify any frozen detector, the production path, or the cost model. Inference is
DESCRIPTIVE-ONLY: no parameter is tuned to maximise PnL.

Honesty notes:
  * The reported move is GROSS mid-to-mid on the NEAR contract. The real strategy additionally
    crosses the spread on both legs and pays 8pt. So "gross median > 8pt" is a NECESSARY (not
    sufficient) condition; failing it is a hard kill.
  * The trigger threshold is the within-DAY top-decile of |theta| (a mild intraday look-ahead
    used only to locate extreme spread moves for a feasibility bound; documented, not a
    promotion claim).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from research.t1.regime_viability import (  # stable primitives only
    NS_PER_MINUTE,
    NS_PER_SECOND,
    BboFrame,
    _date_from_path,
    _load_frames,
    _session_start_ns,
)

SESSION_MINUTES = 285
WARMUP_MIN = 15  # skip first 15m for spread stability
GRID_SEC = 1
HORIZONS_SEC = [5, 30, 120, 600]  # 5s / 30s / 2m / 10m
COST_PTS = 8.0
TOP_DECILE = 0.90  # trigger on |theta| >= this within-day quantile
OOS_START = "2026-04-01"
CONTRACTS = ["b6", "c6", "d6", "e6", "f6", "g6"]
MONTH_IDX = {"b6": 2, "c6": 3, "d6": 4, "e6": 5, "f6": 6, "g6": 7}
RAW_DIR = Path("research/data/raw")
OUT_DIR = Path("research/experiments/validations/t1h_calendar_spread_v0")


def _files_by_date() -> dict[str, dict[str, Path]]:
    """date -> {contract_token 'c6': txf_path} for every TXF contract present that date."""
    out: dict[str, dict[str, Path]] = {}
    for c in CONTRACTS:
        d = RAW_DIR / f"txf{c}"
        if not d.is_dir():
            continue
        for p in d.glob(f"TXF{c.upper()}_*_l2.hftbt.npz"):
            out.setdefault(_date_from_path(p), {})[c] = p
    return out


def _formation_trade_count(path: Path, s0: int) -> int:
    _, trades = _load_frames(path)
    f_end = s0 + 90 * NS_PER_MINUTE
    return int(((trades.ts_ns >= s0) & (trades.ts_ns < f_end)).sum())


def _grid_mid(bbo: BboFrame, grid: np.ndarray) -> np.ndarray:
    """Last-known mid at each grid timestamp; NaN before the first quote."""
    idx = np.searchsorted(bbo.ts_ns, grid, side="right") - 1
    mid = np.full(grid.shape, np.nan, dtype=np.float64)
    valid = idx >= 0
    mid[valid] = bbo.mid[idx[valid]]
    return mid


def _grid_spreadwidth(bbo: BboFrame, grid: np.ndarray) -> np.ndarray:
    idx = np.searchsorted(bbo.ts_ns, grid, side="right") - 1
    w = np.full(grid.shape, np.nan, dtype=np.float64)
    valid = idx >= 0
    w[valid] = (bbo.ask[idx[valid]] - bbo.bid[idx[valid]])
    return w


def _pick_pair(date: str, files: dict[str, Path], s0: int) -> tuple[str, str, Path, Path] | None:
    """front = present contract with the most formation-window trades; next = smallest
    month-index strictly greater than front that is also present that date."""
    present = list(files)
    if len(present) < 2:
        return None
    counts = {c: _formation_trade_count(files[c], s0) for c in present}
    front = max(counts, key=lambda c: counts[c])
    later = [c for c in present if MONTH_IDX[c] > MONTH_IDX[front]]
    if not later:
        return None
    nxt = min(later, key=lambda c: MONTH_IDX[c])
    return front, nxt, files[front], files[nxt]


def _day_triggers(
    front_bbo: BboFrame, next_bbo: BboFrame, s0: int
) -> tuple[dict[int, dict[str, list[float]]], dict[str, float]]:
    """Return per-horizon {contrarian_moves, momentum_moves} and liquidity stats for one day."""
    start = s0 + WARMUP_MIN * NS_PER_MINUTE
    end = s0 + SESSION_MINUTES * NS_PER_MINUTE
    grid = np.arange(start, end, GRID_SEC * NS_PER_SECOND, dtype=np.int64)
    fmid = _grid_mid(front_bbo, grid)
    nmid = _grid_mid(next_bbo, grid)
    fwidth = _grid_spreadwidth(front_bbo, grid)
    nwidth = _grid_spreadwidth(next_bbo, grid)
    both = ~np.isnan(fmid) & ~np.isnan(nmid)
    coverage = float(both.mean())
    liq = {
        "next_coverage": round(coverage, 3),
        "next_median_spread_pts": round(float(np.nanmedian(nwidth)), 2) if np.isfinite(nwidth).any() else None,
        "front_median_spread_pts": round(float(np.nanmedian(fwidth)), 2) if np.isfinite(fwidth).any() else None,
    }
    spread = fmid - nmid  # NaN where either missing
    per_h: dict[int, dict[str, list[float]]] = {}
    n = grid.size
    for h_sec in HORIZONS_SEC:
        hs = h_sec // GRID_SEC
        contrarian: list[float] = []
        momentum: list[float] = []
        if n <= 2 * hs:
            per_h[h_sec] = {"contrarian": contrarian, "momentum": momentum}
            continue
        lo, hi = hs, n - hs - 1
        theta = np.full(n, np.nan, dtype=np.float64)
        theta[lo:hi + 1] = spread[lo:hi + 1] - spread[lo - hs:hi + 1 - hs]
        fwd = np.full(n, np.nan, dtype=np.float64)
        fwd[lo:hi + 1] = fmid[lo + hs:hi + 1 + hs] - fmid[lo:hi + 1]
        valid = ~np.isnan(theta) & ~np.isnan(fwd)
        if valid.sum() < 20:
            per_h[h_sec] = {"contrarian": contrarian, "momentum": momentum}
            continue
        thr = float(np.quantile(np.abs(theta[valid]), TOP_DECILE))
        # greedy non-overlapping triggers
        t = lo
        while t <= hi:
            if valid[t] and abs(theta[t]) >= thr and thr > 0:
                s = 1.0 if theta[t] > 0 else -1.0
                contrarian.append(float(-s * fwd[t]))
                momentum.append(float(s * fwd[t]))
                t += hs
            else:
                t += 1
        per_h[h_sec] = {"contrarian": contrarian, "momentum": momentum}
    return per_h, liq


def _summ(vals: list[float]) -> dict[str, object]:
    if not vals:
        return {"n": 0}
    a = np.asarray(vals, dtype=np.float64)
    return {
        "n": int(a.size),
        "median": round(float(np.median(a)), 2),
        "mean": round(float(a.mean()), 2),
        "p25": round(float(np.quantile(a, 0.25)), 2),
        "p75": round(float(np.quantile(a, 0.75)), 2),
        "pos_frac": round(float((a > 0).mean()), 3),
        "median_gt_cost": bool(np.median(a) > COST_PTS),
    }


def main() -> None:
    fbd = _files_by_date()
    pooled: dict[int, dict[str, list[float]]] = {h: {"contrarian": [], "momentum": []} for h in HORIZONS_SEC}
    pooled_oos: dict[int, list[float]] = {h: [] for h in HORIZONS_SEC}
    liq_rows: list[dict[str, object]] = []
    pairs_used: list[dict[str, object]] = []
    for date in sorted(fbd):
        s0 = _session_start_ns(date)
        pick = _pick_pair(date, fbd[date], s0)
        if pick is None:
            continue
        front, nxt, fpath, npath = pick
        front_bbo, _ = _load_frames(fpath)
        next_bbo, _ = _load_frames(npath)
        if len(front_bbo.ts_ns) == 0 or len(next_bbo.ts_ns) == 0:
            continue
        per_h, liq = _day_triggers(front_bbo, next_bbo, s0)
        if liq["next_coverage"] < 0.2:  # next leg essentially absent -> skip, record as data-thin
            liq_rows.append({"date": date, "front": front, "next": nxt, **liq, "used": False})
            continue
        is_oos = date >= OOS_START
        for h in HORIZONS_SEC:
            pooled[h]["contrarian"].extend(per_h[h]["contrarian"])
            pooled[h]["momentum"].extend(per_h[h]["momentum"])
            if is_oos:
                pooled_oos[h].extend(per_h[h]["contrarian"])
        liq_rows.append({"date": date, "front": front, "next": nxt, **liq, "used": True})
        pairs_used.append({"date": date, "front": front, "next": nxt, "is_oos": is_oos})

    by_horizon = {}
    for h in HORIZONS_SEC:
        by_horizon[f"{h}s"] = {
            "contrarian": _summ(pooled[h]["contrarian"]),
            "momentum_alt": _summ(pooled[h]["momentum"]),
            "contrarian_oos": _summ(pooled_oos[h]),
        }

    # K0 verdict: any horizon with contrarian median > cost AND pos_frac > 0.5 AND coverage ok
    used_cov = [float(r["next_coverage"]) for r in liq_rows if r["used"]]
    median_next_cov = round(float(np.median(used_cov)), 3) if used_cov else 0.0
    passing = []
    for h in HORIZONS_SEC:
        c = by_horizon[f"{h}s"]["contrarian"]
        if c.get("n", 0) >= 20 and c.get("median_gt_cost") and c.get("pos_frac", 0) > 0.5:
            passing.append(f"{h}s")
    k0_pass = bool(passing) and median_next_cov >= 0.5

    result = {
        "schema": "research.t1h_cost_precheck.v1",
        "candidate": "t1h_calendar_spread_negative_feedback_v0",
        "iteration_index": 22,
        "route": "review_hypothesis",
        "gate": "K0_cost_precheck",
        "anchor": "_036 Li-Chen-Liu 2025 (arXiv:2501.03171) calendar-spread negative feedback",
        "method": {
            "signal": "theta = trend of S = front_TXF_mid - next_TXF_mid over a trailing window of length h",
            "contrarian_rule": "direction = -sign(theta); realised = -sign(theta)*(front_mid(t+h)-front_mid(t))",
            "trigger": f"within-day |theta| >= {TOP_DECILE} quantile, greedy non-overlapping",
            "measure": "GROSS mid-to-mid near move (no spread, no cost) -- most generous bound",
            "horizons_sec": HORIZONS_SEC,
            "grid_sec": GRID_SEC,
            "warmup_min": WARMUP_MIN,
            "front_next_rule": "front = most formation-window trades that date; next = smallest later month present",
            "detector_changed": False,
            "production_behavior_changed": False,
            "cost_model_changed": False,
            "inference_policy": "descriptive_only_feasibility_bound_no_tuning",
        },
        "pairs_used_count": len(pairs_used),
        "distinct_dates": len({r["date"] for r in pairs_used}),
        "next_month_liquidity": {
            "median_next_coverage": median_next_cov,
            "per_day": liq_rows,
        },
        "by_horizon": by_horizon,
        "k0": {
            "rule": "build only if some horizon median > 8pt AND pos_frac > 0.5 AND next coverage >= 0.5",
            "passing_horizons": passing,
            "median_next_coverage": median_next_cov,
            "k0_pass": k0_pass,
        },
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "t1h_cost_precheck.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    compact = {
        "pairs_used_count": len(pairs_used),
        "distinct_dates": result["distinct_dates"],
        "median_next_coverage": median_next_cov,
        "by_horizon": by_horizon,
        "k0_pass": k0_pass,
        "passing_horizons": passing,
    }
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
