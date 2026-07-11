"""Iteration-19 expand_sample: FROZEN t1g_extreme_imbalance_reversal_momentum_v0
detector + first event sample.

Literature basis (iteration-18 refresh, reports/readiness_candidate_literature_refresh_iteration18.json):
  Kao (2011, lit_refresh_..._031): daily TAIFEX index-futures returns show MOMENTUM
  following extreme-HIGH trading imbalance and REVERSAL following extreme-LOW trading
  imbalance, using a decision-time-observable uptick/downtick volume metric.
  Chordia-Roll-Subrahmanyam (2002, _033): reversal is side-asymmetric.
  Chordia-Subrahmanyam (2004, _034) + MDPI tick-size (2021, _035): the imbalance edge is
  thin and is the leading cost-nullification risk -> the EXECUTABLE, stop-honored,
  spread-and-cost-deducted realised return is the headline (NOT a mid-price edge).

This module is STANDALONE and ADDITIVE. It imports only stable low-level primitives from
research/t1/regime_viability.py (frame loaders, session/date helpers, NS_PER_MINUTE) --
NONE of the t1f functions, and NONE of the functions the parallel Codex session is editing.
It does NOT modify any frozen detector, the production path, or the cost model.

================================ FROZEN V0 CONTRACT ================================
Lane                : TXF front-month = SIGNAL, TMF = EXECUTION (same envelope as all T1).
Cost                : 8.0 pt all-in, spread CROSSED on both legs (entry long->TMF ask /
                      short->TMF bid; exit long->bid / short->ask), per the frozen
                      evaluate_executable_returns convention.
Session             : start 08:45 TPE, 285 minutes.
Formation window    : first 90 minutes (08:45 -> 10:15). On TXF compute:
                        r_f  = formation return = mid(end) - mid(start)               [pts]
                        TI   = tick-rule signed-volume fraction in [-1, 1]
                               (Lee-Ready tick test, carry-forward on zero-tick)
Signal gate         : trade only if |r_f| >= MIN_FORMATION_MOVE_PTS (a real thrust to
                      continue / reverse); else NO TRADE.
Regime (|TI| tail)  : HIGH if |TI| >= split, LOW if |TI| < split, where `split` is the
                      pooled median of |TI| over the sampled contract-days.
                      *** V0 LOOK-AHEAD CAVEAT: the pooled median uses the whole sample to
                          define the regime threshold. This is a known V0 limitation; it is
                          a MAGNITUDE partition (not a PnL/return partition), so it does not
                          directly leak the outcome. The v1 fix is an expanding-window /
                          per-contract walk-forward quantile. V0 is DESCRIPTIVE ONLY. ***
Direction (Kao)     : HIGH regime -> MOMENTUM   -> trade  sign(r_f)
                      LOW  regime -> REVERSAL   -> trade -sign(r_f)
Entry               : at formation-window end (10:15) on TMF.
Stop                : TXF mid adverse beyond entry_ref -/+ STOP_BUFFER_PTS within the hold.
Exit                : at first stop breach (prevailing TMF quote) else at 30m time-exit.
Headline metric     : STOP-HONORED, spread-crossed, 8pt-net realised return per event.
=================================================================================

Pre-registered kill criteria (evaluated, not tuned):
  K1  executable stop-honored mean net <= 10pt on the full sample.
  K2  mid-price (no-spread, no-stop) edge is positive but the executable net is not
      -> cost-nullification (Chordia-Subrahmanyam / MDPI).
  K3  single-event share of total positive PnL too high (single-day-dominance pathology).
  K4  direction does not separate by regime (HIGH and LOW means not distinct)
      -> it is just an unconditional fade == already-killed T1-C/T1-D.

NO PARAMETER IS TUNED TO MAXIMISE PnL. 90 / 30 / 15 / 8 are the exact frozen T1-spine
constants reused for consistency; MIN_FORMATION_MOVE_PTS = 10 is the edge-floor magnitude.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, median

import numpy as np

from research.t1.regime_viability import (  # stable primitives only
    NS_PER_MINUTE,
    BboFrame,
    TradeFrame,
    _date_from_path,
    _load_frames,
    _session_start_ns,
)

# ---- frozen constants (pre-registered) ----
SESSION_MINUTES = 285
FORMATION_WINDOW_MIN = 90
HORIZON_MIN = 30
STOP_BUFFER_PTS = 15.0
COST_PTS = 8.0
MIN_FORMATION_MOVE_PTS = 10.0
OOS_START = "2026-04-01"
CONTRACTS = ["b6", "c6", "d6", "e6", "f6", "g6"]  # txf<X>6 <-> tmf<X>6; txfi6 single-file excluded
RAW_DIR = Path("research/data/raw")
OUT_DIR = Path("research/experiments/validations/t1g_extreme_imbalance_v0")


def _pairs() -> list[tuple[Path, Path, str, str]]:
    """All (txf_path, tmf_path, contract, date) with both sides present."""
    out: list[tuple[Path, Path, str, str]] = []
    for c in CONTRACTS:
        txf_dir = RAW_DIR / f"txf{c}"
        tmf_dir = RAW_DIR / f"tmf{c}"
        if not (txf_dir.is_dir() and tmf_dir.is_dir()):
            continue
        for txf_path in sorted(txf_dir.glob(f"TXF{c.upper()}_*_l2.hftbt.npz")):
            date = _date_from_path(txf_path)
            tmf_path = tmf_dir / f"TMF{c.upper()}_{date}_l2.hftbt.npz"
            if tmf_path.exists():
                out.append((txf_path, tmf_path, f"TXF{c.upper()}->TMF{c.upper()}", date))
    return out


def _tick_signed_fraction(trades: TradeFrame, lo_ns: int, hi_ns: int) -> tuple[float, float, int]:
    """Lee-Ready tick-rule signed-volume fraction over [lo_ns, hi_ns)."""
    mask = (trades.ts_ns >= lo_ns) & (trades.ts_ns < hi_ns)
    px = trades.price[mask]
    qty = trades.qty[mask]
    n = int(px.size)
    if n == 0:
        return 0.0, 0.0, 0
    signs = np.zeros(n, dtype=np.float64)
    if n > 1:
        signs[1:] = np.sign(np.diff(px))
    # forward-fill zero-ticks with the last non-zero sign
    nz = signs != 0.0
    idx = np.where(nz, np.arange(n), 0)
    np.maximum.accumulate(idx, out=idx)
    signs = signs[idx]
    total = float(np.sum(qty))
    signed = float(np.sum(signs * qty))
    ti = signed / total if total > 0 else 0.0
    return ti, total, n


def _mid_at(bbo: BboFrame, ts_ns: int) -> float | None:
    i = int(np.searchsorted(bbo.ts_ns, ts_ns, side="left"))
    if i >= len(bbo.ts_ns):
        return None
    return float(bbo.mid[i])


def _realised(
    txf_bbo: BboFrame, tmf_bbo: BboFrame, trigger_ns: int, direction: int
) -> dict[str, object] | None:
    """Stop-honored, spread-crossed realised return + a mid-only optimistic comparator."""
    horizon_ns = trigger_ns + HORIZON_MIN * NS_PER_MINUTE
    entry_ref = _mid_at(txf_bbo, trigger_ns)
    if entry_ref is None:
        return None
    ei = int(np.searchsorted(tmf_bbo.ts_ns, trigger_ns, side="left"))
    if ei >= len(tmf_bbo.ts_ns):
        return None
    entry = float(tmf_bbo.ask[ei] if direction > 0 else tmf_bbo.bid[ei])

    # stop time on TXF mid within the hold
    hmask = (txf_bbo.ts_ns >= trigger_ns) & (txf_bbo.ts_ns <= horizon_ns)
    hts = txf_bbo.ts_ns[hmask]
    hmid = txf_bbo.mid[hmask]
    if direction > 0:
        bp = np.flatnonzero(hmid <= entry_ref - STOP_BUFFER_PTS)
    else:
        bp = np.flatnonzero(hmid >= entry_ref + STOP_BUFFER_PTS)
    stop_ns = int(hts[int(bp[0])]) if bp.size else None

    if stop_ns is not None:
        xi = int(np.searchsorted(tmf_bbo.ts_ns, stop_ns, side="left"))
        xi = min(xi, len(tmf_bbo.ts_ns) - 1)
        exit_px = float(tmf_bbo.bid[xi] if direction > 0 else tmf_bbo.ask[xi])
        mode = "stop_exit"
    else:
        he = int(np.searchsorted(tmf_bbo.ts_ns, horizon_ns, side="right"))
        if he <= ei:
            return None
        exit_px = float(tmf_bbo.bid[he - 1] if direction > 0 else tmf_bbo.ask[he - 1])
        mode = "time_exit_30m"

    exec_gross = (exit_px - entry) * direction
    exec_net = exec_gross - COST_PTS

    # mid-only optimistic comparator (no spread, no stop, no cost) for K2 cost-nullification
    mid_end = None
    he2 = int(np.searchsorted(tmf_bbo.ts_ns, horizon_ns, side="right"))
    if he2 > ei:
        mid_end = float(tmf_bbo.mid[he2 - 1])
    mid_entry = float(tmf_bbo.mid[ei])
    mid_edge = ((mid_end - mid_entry) * direction) if mid_end is not None else None

    return {
        "tmf_entry": round(entry, 2),
        "exit_mode": mode,
        "stop_breached": stop_ns is not None,
        "exec_gross_pts": round(exec_gross, 2),
        "exec_net_pts": round(exec_net, 2),
        "mid_only_edge_pts": round(mid_edge, 2) if mid_edge is not None else None,
    }


def build_events() -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for txf_path, tmf_path, contract, date in _pairs():
        txf_bbo, txf_trades = _load_frames(txf_path)
        tmf_bbo, _ = _load_frames(tmf_path)
        if len(txf_bbo.ts_ns) == 0 or len(tmf_bbo.ts_ns) == 0:
            continue
        s0 = _session_start_ns(date)
        f_end = s0 + FORMATION_WINDOW_MIN * NS_PER_MINUTE
        mid_start = _mid_at(txf_bbo, s0)
        mid_fend = _mid_at(txf_bbo, f_end)
        if mid_start is None or mid_fend is None:
            continue
        r_f = mid_fend - mid_start
        ti, vol, ntr = _tick_signed_fraction(txf_trades, s0, f_end)
        events.append(
            {
                "contract": contract,
                "date": date,
                "is_oos": date >= OOS_START,
                "formation_return_pts": round(r_f, 2),
                "trading_imbalance": round(ti, 4),
                "abs_imbalance": round(abs(ti), 4),
                "formation_volume": round(vol, 1),
                "formation_trades": ntr,
                "trigger_ns": int(f_end),
                "_txf_bbo": txf_bbo,
                "_tmf_bbo": tmf_bbo,
            }
        )
    return events


def _score(nets: list[float]) -> dict[str, object]:
    if not nets:
        return {"events": 0}
    arr = np.asarray(nets, dtype=np.float64)
    return {
        "events": len(nets),
        "mean_net": round(float(arr.mean()), 2),
        "median_net": round(float(np.median(arr)), 2),
        "min_net": round(float(arr.min()), 2),
        "max_net": round(float(arr.max()), 2),
        "total_net": round(float(arr.sum()), 2),
        "positive_fraction": round(float((arr > 0).mean()), 3),
        "clears_10pt_floor_on_mean": bool(arr.mean() >= 10.0),
    }


def main() -> None:
    raw = build_events()
    # signal gate + regime split (pooled-median |TI|, documented look-ahead)
    gated = [e for e in raw if abs(float(e["formation_return_pts"])) >= MIN_FORMATION_MOVE_PTS]
    split = float(median([float(e["abs_imbalance"]) for e in gated])) if gated else 0.0

    traded: list[dict[str, object]] = []
    for e in gated:
        r_f = float(e["formation_return_pts"])
        sign = 1 if r_f > 0 else -1
        regime = "HIGH" if float(e["abs_imbalance"]) >= split else "LOW"
        direction = sign if regime == "HIGH" else -sign  # momentum vs reversal
        res = _realised(e["_txf_bbo"], e["_tmf_bbo"], int(e["trigger_ns"]), direction)
        if res is None:
            continue
        row = {k: v for k, v in e.items() if not k.startswith("_")}
        row.update({"regime": regime, "direction": direction, **res})
        traded.append(row)

    def nets(rows: list[dict[str, object]], oos: bool | None = None) -> list[float]:
        return [
            float(r["exec_net_pts"])
            for r in rows
            if (oos is None or bool(r["is_oos"]) == oos) and r["exec_net_pts"] is not None
        ]

    # single-event dominance on positive PnL (K3)
    pos = [float(r["exec_net_pts"]) for r in traded if float(r["exec_net_pts"]) > 0]
    total_pos = sum(pos) if pos else 0.0
    max_share = round(max(pos) / total_pos, 3) if total_pos > 0 else None

    # regime separation (K4)
    high_nets = [float(r["exec_net_pts"]) for r in traded if r["regime"] == "HIGH"]
    low_nets = [float(r["exec_net_pts"]) for r in traded if r["regime"] == "LOW"]

    # cost-nullification (K2)
    mid_edges = [float(r["mid_only_edge_pts"]) for r in traded if r["mid_only_edge_pts"] is not None]
    exec_nets = nets(traded)
    mid_mean = round(mean(mid_edges), 2) if mid_edges else None
    exec_mean = round(mean(exec_nets), 2) if exec_nets else None

    # per-contract + front-month-deduped (one event per date = higher formation_volume)
    by_contract: dict[str, dict[str, object]] = {}
    for c in sorted({str(r["contract"]) for r in traded}):
        by_contract[c] = _score([float(r["exec_net_pts"]) for r in traded if r["contract"] == c])
    by_date: dict[str, dict[str, object]] = {}
    for r in traded:
        d = str(r["date"])
        if d not in by_date or float(r["formation_volume"]) > float(by_date[d]["formation_volume"]):
            by_date[d] = r
    deduped = list(by_date.values())

    full = _score(exec_nets)
    oos = _score(nets(traded, oos=True))

    k1 = not bool(full.get("clears_10pt_floor_on_mean", False))
    k2 = bool(mid_mean is not None and exec_mean is not None and mid_mean > 0 and exec_mean <= 0)
    k3 = bool(max_share is not None and max_share >= 0.5)
    k4 = bool(
        high_nets and low_nets and (np.sign(mean(high_nets)) == np.sign(mean(low_nets)))
        and abs(mean(high_nets) - mean(low_nets)) < COST_PTS
    )

    result = {
        "schema": "research.t1g_first_sample.v1",
        "candidate": "t1g_extreme_imbalance_reversal_momentum_v0",
        "iteration_index": 19,
        "route": "expand_sample",
        "frozen_contract": {
            "session_minutes": SESSION_MINUTES,
            "formation_window_min": FORMATION_WINDOW_MIN,
            "horizon_min": HORIZON_MIN,
            "stop_buffer_pts": STOP_BUFFER_PTS,
            "cost_pts": COST_PTS,
            "min_formation_move_pts": MIN_FORMATION_MOVE_PTS,
            "regime_split_rule": "pooled_median_abs_imbalance",
            "regime_split_value": round(split, 4),
            "oos_start": OOS_START,
            "detector_changed": False,
            "production_behavior_changed": False,
            "cost_model_changed": False,
            "inference_policy": "descriptive_only_v0_no_promotion_no_tuning_lookahead_caveat_on_regime_split",
        },
        "sample_floor": {
            "events_floor": 80,
            "trading_days_floor": 20,
            "contracts_floor": 4,
            "events_observed": len(traded),
            "distinct_dates_observed": len(by_date),
            "contracts_observed": len({str(r["contract"]) for r in traded}),
            "floor_reached": bool(
                len(traded) >= 80 and len(by_date) >= 20 and len({str(r["contract"]) for r in traded}) >= 4
            ),
        },
        "headline_executable_stop_honored": full,
        "oos_executable_stop_honored": oos,
        "front_month_deduped": _score([float(r["exec_net_pts"]) for r in deduped]),
        "by_contract": by_contract,
        "regime_separation": {
            "HIGH_momentum": _score(high_nets),
            "LOW_reversal": _score(low_nets),
        },
        "cost_nullification_check": {
            "mid_only_mean_edge_pts": mid_mean,
            "executable_net_mean_pts": exec_mean,
        },
        "single_event_dominance": {"max_positive_share": max_share},
        "kill_criteria": {
            "K1_floor_not_cleared": k1,
            "K2_cost_nullification": k2,
            "K3_single_event_dominance": k3,
            "K4_no_regime_separation": k4,
        },
        "counts": {
            "raw_contract_days": len(raw),
            "passed_signal_gate": len(gated),
            "traded": len(traded),
        },
        "events": [{k: v for k, v in r.items()} for r in sorted(traded, key=lambda r: str(r["date"]))],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "t1g_first_sample.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    # compact console summary
    summary = {k: result[k] for k in (
        "sample_floor", "headline_executable_stop_honored", "oos_executable_stop_honored",
        "front_month_deduped", "regime_separation", "cost_nullification_check",
        "single_event_dominance", "kill_criteria", "counts",
    )}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
