"""Iteration-28 review_hypothesis: t1j K0-j PASSIVE-MAKER LONG-ENTRY FEASIBILITY GATE.

USER chose Option A (iter27): test directly whether entering a LONG PASSIVELY (resting bid)
instead of crossing the spread recovers enough of the 8pt all-in cost to change the arithmetic
that killed the taker-side program (9 consecutive structural negatives). Review-before-build,
additive, descriptive-only; no detector, production path, or cost model touched.

Seed: reports/readiness_candidate_literature_refresh_iteration27.json. Anchors:
  _046 Moallemi-Yuan 2016 (passive value = spread earned - adverse selection; you keep REALIZED
       spread, not quoted spread),
  _047 Chen-Wu 2009 (TAIFEX pure-LOB futures = HIGH adverse selection; on up-moves buyers
       withdraw bids -> a passive long is adversely selected in the momentum regime),
  _048 arXiv 2409.12721 (adverse fills are the MAJORITY in futures; front-of-queue backtests =
       phantom gains = the r47 live/backtest divergence),
  _049 Lehalle-Mounjid 2016 (passive-timing edge eroded by latency; imbalance not enough to beat
       the spread),
  _050 Cartea-Jaimungal 2015 (passive saves at most ~1 spread/share, only when working a
       schedule -- the CEILING).

THE HONEST COMPARISON (no phantom gains). The maker pivot attacks the ENTRY leg only; the EXIT is
force-flat at close so it stays a taker cross. For each long entry at t0 on the front TMF
contract:
  * Taker arm: enter by crossing -> entry_price = ask(t0).
  * Maker arm: post a resting bid at bid(t0); if it fills passively within a wait window W, enter
    at bid(t0); else TAKER-FALLBACK chase at ask(t0+W).
Both arms exit identically, so the realized exit mid CANCELS and the per-entry execution saving is
exactly:
      saving = ask(t0) - maker_entry_price
             = +spread(t0)              if passively filled (good)
             = ask(t0) - ask(t0+W)      if not filled (negative when price ran up = the chase).
Adverse selection enters as a SELECTION effect across entries: a resting bid fills mainly when
price ticks DOWN to it, and FAILS to fill on the up-gaps (the good long days) -> those become a
fallback chase at a higher ask. The saving metric captures this automatically; we also report the
adverse-fill rate (_048 definition) for transparency.

REALISTIC, NON-PHANTOM FILL MODEL:
  * Queue position: we post BEHIND the existing best-bid size Q0 = bid_qty(t0) (price-time
    priority). Our infinitesimal order fills only after cumulative trade volume executed at price
    <= bid(t0) since t0 EXCEEDS Q0. We IGNORE cancellations of the queue ahead -> UNDERSTATES
    fill rate -> CONSERVATIVE (cannot manufacture a phantom saving).
  * No-latency assumption: we post at t0 and rest instantly. This is OPTIMISTIC (real P99 ~500ms;
    _049 says the timing edge erodes with latency). So if the pivot fails even here, the KILL is
    robust.
  * Size = 1 lot (TMF deploy is max_pos=1), so the infinitesimal-order assumption is realistic.

GATE (pre-registered). The maker pivot PROCEEDS to a build only if, at some wait window, the
passive long entry on TMF:
  (1) mean_saving > 0 AND mean_saving >= 2.0 pts (>= ~1/4 of the 8pt all-in -- "enough to
      matter"), AND
  (2) fill_rate >= 0.5 (a real strategy, not mostly fallback chase), AND
  (3) adverse_fill_rate < 0.5 (materially below the _048 majority baseline).
Otherwise STRUCTURAL KILL of the maker pivot under the current intraday + long-only mandate.

Standalone/ADDITIVE: imports only stable primitives from research/t1/regime_viability.py. Touches
no frozen detector, no production path, no cost model. Executes on TMF L2 (where we actually
trade), front contract per date.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from research.t1.regime_viability import (  # stable primitives only
    NS_PER_MINUTE,
    NS_PER_SECOND,
    BboFrame,
    TradeFrame,
    _date_from_path,
    _load_frames,
    _session_start_ns,
)

SESSION_MINUTES = 285
WARMUP_MIN = 5
COST_PTS = 8.0  # taker all-in baseline (both legs); maker attacks the entry leg (~half)
MIN_FORMATION_TRADES = 30
MIN_CELL_N = 6
OOS_START = "2026-04-01"
CONTRACTS = ["b6", "c6", "d6", "e6", "f6", "g6"]
RAW_DIR = Path("research/data/raw")
OUT_DIR = Path("research/experiments/validations/t1j_passive_maker_long_entry_v0")

FORMATION_MIN = [30, 60, 90]  # long-entry timestamps (minutes into session)
WAIT_WINDOWS_S = [30, 120, 300]  # passive wait before taker-fallback chase
MARK_LAG_S = 30  # post-fill mark horizon for the adverse-fill classification

# gate thresholds
GATE_MIN_SAVING = 2.0
GATE_MIN_FILL_RATE = 0.5
GATE_MAX_ADVERSE_FILL = 0.5


def _files_by_date() -> dict[str, dict[str, Path]]:
    out: dict[str, dict[str, Path]] = {}
    for c in CONTRACTS:
        d = RAW_DIR / f"tmf{c}"
        if not d.is_dir():
            continue
        for p in d.glob(f"TMF{c.upper()}_*_l2.hftbt.npz"):
            out.setdefault(_date_from_path(p), {})[c] = p
    return out


def _val_at(arr: np.ndarray, ts_ns: np.ndarray, ts: int) -> float:
    idx = int(np.searchsorted(ts_ns, ts, side="right")) - 1
    if idx < 0:
        return float("nan")
    return float(arr[idx])


def _pick_front(files: dict[str, Path], s0: int) -> tuple[str, Path] | None:
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


def _passive_fill_ts(trades: TradeFrame, t0: int, t_end: int, bid0: float, q_ahead: float) -> int | None:
    """Fill time of a resting bid at bid0 posted behind q_ahead, ignoring cancellations.

    Fills when cumulative volume of trades executed at price <= bid0 in (t0, t_end] exceeds
    q_ahead. Conservative (ignores queue-ahead cancellations -> understates fill rate).
    """
    mask = (trades.ts_ns > t0) & (trades.ts_ns <= t_end) & (trades.price <= bid0)
    if not mask.any():
        return None
    idx = np.flatnonzero(mask)
    cum = np.cumsum(trades.qty[idx])
    hit = np.searchsorted(cum, q_ahead, side="right")
    if hit >= idx.size:
        return None  # never cleared the queue ahead within the window
    return int(trades.ts_ns[idx[hit]])


def _entry_record(bbo: BboFrame, trades: TradeFrame, t0: int) -> dict | None:
    """Per-entry passive-vs-taker execution outcome at each wait window."""
    mid0 = _val_at(bbo.mid, bbo.ts_ns, t0)
    bid0 = _val_at(bbo.bid, bbo.ts_ns, t0)
    ask0 = _val_at(bbo.ask, bbo.ts_ns, t0)
    q0 = _val_at(bbo.bid_qty, bbo.ts_ns, t0)
    if not all(np.isfinite(x) for x in (mid0, bid0, ask0, q0)):
        return None
    if ask0 <= bid0:
        return None
    by_window: dict[str, dict] = {}
    for w in WAIT_WINDOWS_S:
        t_end = t0 + w * NS_PER_SECOND
        tf = _passive_fill_ts(trades, t0, t_end, bid0, q0)
        if tf is not None:
            maker_entry = bid0
            filled = True
            mid_tf = _val_at(bbo.mid, bbo.ts_ns, tf)
            mid_mark = _val_at(bbo.mid, bbo.ts_ns, tf + MARK_LAG_S * NS_PER_SECOND)
            adverse = bool(np.isfinite(mid_tf) and np.isfinite(mid_mark) and mid_mark < mid_tf)
        else:
            ask_w = _val_at(bbo.ask, bbo.ts_ns, t_end)
            maker_entry = ask_w if np.isfinite(ask_w) else ask0
            filled = False
            adverse = False
        by_window[f"{w}s"] = {
            "filled": filled,
            "saving": ask0 - maker_entry,  # exit cancels; this is the entry-cost reduction
            "adverse": adverse,
            "spread": ask0 - bid0,
        }
    return by_window


def _collect(fbd: dict[str, dict[str, Path]]) -> tuple[list[dict], int]:
    rows: list[dict] = []
    n_dates = 0
    for date in sorted(fbd):
        s0 = _session_start_ns(date)
        pick = _pick_front(fbd[date], s0)
        if pick is None:
            continue
        front, fpath = pick
        bbo, trades = _load_frames(fpath)
        if len(bbo.ts_ns) == 0:
            continue
        start = s0 + WARMUP_MIN * NS_PER_MINUTE
        m_start = _val_at(bbo.mid, bbo.ts_ns, start)
        if not np.isfinite(m_start):
            continue
        n_dates += 1
        is_oos = date >= OOS_START
        for fm in FORMATION_MIN:
            t0 = s0 + fm * NS_PER_MINUTE
            m_entry = _val_at(bbo.mid, bbo.ts_ns, t0)
            if not np.isfinite(m_entry):
                continue
            if m_entry - m_start <= 0:  # LONG-ONLY: up-context entries (the hard adverse case)
                continue
            rec = _entry_record(bbo, trades, t0)
            if rec is None:
                continue
            rows.append({"date": date, "front": front, "formation_min": fm,
                         "is_oos": is_oos, "windows": rec})
    return rows, n_dates


def _summ_window(rows: list[dict], w_key: str, oos: bool | None = None) -> dict:
    sub = [r["windows"][w_key] for r in rows
           if (oos is None or r["is_oos"] == oos)]
    n = len(sub)
    if n == 0:
        return {"n": 0}
    savings = np.asarray([s["saving"] for s in sub], dtype=np.float64)
    fills = np.asarray([s["filled"] for s in sub], dtype=bool)
    spreads = np.asarray([s["spread"] for s in sub], dtype=np.float64)
    n_fill = int(fills.sum())
    adverse_among_fills = [s["adverse"] for s in sub if s["filled"]]
    return {
        "n": n,
        "fill_rate": round(float(fills.mean()), 3),
        "mean_saving": round(float(savings.mean()), 3),
        "median_saving": round(float(np.median(savings)), 3),
        "p25_saving": round(float(np.quantile(savings, 0.25)), 3),
        "p75_saving": round(float(np.quantile(savings, 0.75)), 3),
        "mean_spread": round(float(spreads.mean()), 3),
        "mean_saving_on_fills": round(float(savings[fills].mean()), 3) if n_fill else None,
        "mean_saving_on_nofills": round(float(savings[~fills].mean()), 3) if n_fill < n else None,
        "adverse_fill_rate": round(float(np.mean(adverse_among_fills)), 3) if adverse_among_fills else None,
    }


def main() -> None:
    fbd = _files_by_date()
    rows, n_dates = _collect(fbd)

    by_window: dict[str, dict] = {}
    passing_windows: list[dict] = []
    for w in WAIT_WINDOWS_S:
        wk = f"{w}s"
        full = _summ_window(rows, wk, None)
        by_window[wk] = {
            "full": full,
            "is": _summ_window(rows, wk, False),
            "oos": _summ_window(rows, wk, True),
        }
        if full.get("n", 0) >= MIN_CELL_N:
            adverse = full.get("adverse_fill_rate")
            if (
                full["mean_saving"] > 0
                and full["mean_saving"] >= GATE_MIN_SAVING
                and full["fill_rate"] >= GATE_MIN_FILL_RATE
                and adverse is not None
                and adverse < GATE_MAX_ADVERSE_FILL
            ):
                passing_windows.append({"window": wk, **{k: full[k] for k in
                                       ("n", "fill_rate", "mean_saving", "adverse_fill_rate")}})
    gate_pass = bool(passing_windows)

    result = {
        "schema": "research.t1j_maker_cost_gate.v1",
        "candidate": "t1j_passive_maker_long_entry_feasibility_v0",
        "iteration_index": 28,
        "route": "review_hypothesis",
        "gate": "K0j_passive_maker_long_entry",
        "user_choice": "A (run the K0-j feasibility gate)",
        "anchors": ["_046", "_047(TAIFEX adverse-sel)", "_048(adverse-fills-majority)",
                    "_049(latency)", "_050(saving ceiling ~1 spread)"],
        "method": {
            "instrument": "front TMF contract per date (execution venue)",
            "long_only": "keep only up-context entries (formation mid > warmup mid) -- the hard adverse-selection case",
            "entry_times_min": FORMATION_MIN,
            "wait_windows_s": WAIT_WINDOWS_S,
            "mark_lag_s": MARK_LAG_S,
            "fill_model": "rest behind Q0=bid_qty(t0); fill when cum vol<=bid0 > Q0; cancels ignored (conservative)",
            "saving_metric": "ask0 - maker_entry_price (exit cancels; +spread if filled, chase if not)",
            "latency": "none modeled (OPTIMISTIC; real P99 ~500ms erodes the edge per _049)",
            "exit_leg": "stays a TAKER cross (force-flat at close); maker attacks entry leg only",
            "detector_changed": False,
            "production_behavior_changed": False,
            "cost_model_changed": False,
            "inference_policy": "descriptive_only_feasibility_bound_no_tuning",
        },
        "gate_rule": {
            "thresholds": {"min_mean_saving_pts": GATE_MIN_SAVING,
                           "min_fill_rate": GATE_MIN_FILL_RATE,
                           "max_adverse_fill_rate": GATE_MAX_ADVERSE_FILL},
            "ceiling_note": "max recoverable on entry leg ~= one spread (~half of 8pt); _050 ceiling ~1 spread/share",
        },
        "front_days_used": n_dates,
        "long_entries": len(rows),
        "by_window": by_window,
        "passing_windows": passing_windows,
        "gate_pass": gate_pass,
        "verdict_route": "expand_sample" if gate_pass else "archive_candidate_set",
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "t1j_maker_cost_gate.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    compact = {
        "front_days_used": n_dates,
        "long_entries": len(rows),
        "gate_pass": gate_pass,
        "verdict_route": result["verdict_route"],
        "by_window": {wk: {"n": d["full"].get("n"), "fill_rate": d["full"].get("fill_rate"),
                           "mean_saving": d["full"].get("mean_saving"),
                           "median_saving": d["full"].get("median_saving"),
                           "mean_spread": d["full"].get("mean_spread"),
                           "saving_on_fills": d["full"].get("mean_saving_on_fills"),
                           "saving_on_nofills": d["full"].get("mean_saving_on_nofills"),
                           "adverse_fill_rate": d["full"].get("adverse_fill_rate"),
                           "oos_mean_saving": d["oos"].get("mean_saving")}
                      for wk, d in by_window.items()},
        "passing_windows": passing_windows,
    }
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
