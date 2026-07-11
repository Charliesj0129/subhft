"""Iteration-16 review_hypothesis ADDITIVE exit-overlay diagnostic for
t1f_txf_expiration_vreversal_tmf.

Motivation (literature, iteration 15): Kaminski & Lo (2013) show a stop-loss has a
NEGATIVE stopping premium on a MEAN-REVERTING process -- it forces an exit right before
the reversal the strategy depends on.  A settlement V-reversal fade is a mean-reversion
bet, so its thrust-extension price stop is in structural tension with its own thesis
(iteration 14: E6 +214 frozen -> -68 once the price stop is honored).

This script does NOT modify the frozen detector, cost, parameters, or the production
path.  It imports the frozen functions, reuses the same detected entries, and evaluates
THREE pre-registered exit policies on each entry to decide whether t1f's realised
negativity is an artifact of the *stop choice* (a fade-consistent exit recovers it) or
intrinsic to a drawdown-controlled fade (it does not -> move toward kill):

  P0_frozen_30m_time   : pure 30-minute hold, no price stop  (== frozen headline; an
                         OPTIMISTIC upper bound with NO drawdown control).
  P1_thrust_price_stop : exit at first breach of the thrust-extension stop within the
                         hold, else 30m  (== iteration-14 realised; drawdown-controlled
                         but the stop fights the fade).
  P2_reversion_target  : fade-CONSISTENT.  Take profit at the first time the TXF mid
                         reverts fully to today's open (the pre-thrust reference), with
                         the SAME thrust-extension stop still active; whichever of
                         {stop, target} fires first wins, else 30m time-exit.

All policies are fully determined by frozen quantities (today_open, the recorded stop
levels, the 30m horizon).  NO new tunable parameter is introduced, and NOTHING is tuned
to make E6 positive.  Inference is descriptive only at N=3.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, median

import numpy as np

from research.t1.regime_viability import (
    NS_PER_MINUTE,
    ExpirationVReversalConfig,
    _date_from_path,
    _load_frames,
    _session_start_ns,
    _settlement_day_pairs,
    detect_expiration_v_reversal_events,
)

COST_PTS = 8.0
SESSION_MINUTES = 285
THRUST_WINDOW_MINUTES = 90
MIN_THRUST_PTS = 20.0
STOP_BUFFER_PTS = 15.0
PRIMARY_HORIZON_MINUTES = 30
OOS_START = "2026-04-01"
MONTHS = ["B6", "C6", "D6", "E6", "F6", "G6"]
RAW_DIR = Path("research/data/raw")
OUT_DIR = Path("research/experiments/validations/t1f_expiration_vreversal_v0")


def _tmf_exit_price(tmf_bbo, ts_ns: int, direction: int) -> float | None:
    """First executable TMF exit quote at/after ts_ns (long exits at bid, short at ask)."""
    idx = int(np.searchsorted(tmf_bbo.ts_ns, ts_ns, side="left"))
    if idx >= len(tmf_bbo.ts_ns):
        return None
    return float(tmf_bbo.bid[idx] if direction > 0 else tmf_bbo.ask[idx])


def _today_open(today_bbo, session_start_ns: int, session_minutes: int) -> float | None:
    end = session_start_ns + session_minutes * NS_PER_MINUTE
    mask = (today_bbo.ts_ns >= session_start_ns) & (today_bbo.ts_ns <= end)
    flat = np.flatnonzero(mask)
    if not flat.size:
        return None
    return float(today_bbo.mid[int(flat[0])])


def diagnose_pair(txf_path: Path, tmf_path: Path) -> list[dict[str, object]]:
    date_str = _date_from_path(txf_path)
    txf_contract = txf_path.name.split("_", 1)[0]
    tmf_contract = tmf_path.name.split("_", 1)[0]
    today_bbo, today_trades = _load_frames(txf_path)
    tmf_bbo, _ = _load_frames(tmf_path)
    session_start_ns = _session_start_ns(date_str, tz_offset_hours=8)
    config = ExpirationVReversalConfig(
        session_start_ns=session_start_ns,
        session_minutes=SESSION_MINUTES,
        thrust_window_minutes=THRUST_WINDOW_MINUTES,
        min_thrust_pts=MIN_THRUST_PTS,
        stop_buffer_pts=STOP_BUFFER_PTS,
    )
    today_open = _today_open(today_bbo, session_start_ns, SESSION_MINUTES)
    rows: list[dict[str, object]] = []
    for event in detect_expiration_v_reversal_events(
        today_bbo, today_trades, contract=txf_contract, date=date_str, config=config
    ):
        d = event.direction
        trig = event.trigger_time_ns
        horizon_ns = trig + PRIMARY_HORIZON_MINUTES * NS_PER_MINUTE

        # TMF entry (identical to frozen: long buys ask, short sells bid).
        e_idx = int(np.searchsorted(tmf_bbo.ts_ns, trig, side="left"))
        if e_idx >= len(tmf_bbo.ts_ns):
            continue
        entry = float(tmf_bbo.ask[e_idx] if d > 0 else tmf_bbo.bid[e_idx])

        # ---- P0: pure 30m time exit, no stop (frozen headline) ----
        h_end = int(np.searchsorted(tmf_bbo.ts_ns, horizon_ns, side="right"))
        if h_end <= e_idx:
            continue
        p0_exit = float(tmf_bbo.bid[h_end - 1] if d > 0 else tmf_bbo.ask[h_end - 1])
        p0_net = (p0_exit - entry) * d - COST_PTS

        # TXF hold-window path for event timing.
        hmask = (today_bbo.ts_ns >= trig) & (today_bbo.ts_ns <= horizon_ns)
        hts = today_bbo.ts_ns[hmask]
        hmid = today_bbo.mid[hmask]

        # First stop breach (continuation past thrust extension).
        if d > 0:
            stop_pos = np.flatnonzero(hmid <= event.opening_range_low)
        else:
            stop_pos = np.flatnonzero(hmid >= event.opening_range_high)
        stop_ts = int(hts[int(stop_pos[0])]) if stop_pos.size else None

        # First full reversion to today's open (fade thesis completed).
        target_ts = None
        if today_open is not None and hts.size:
            if d > 0:  # long fade of a down-thrust: profit when mid rises back to open
                tgt_pos = np.flatnonzero(hmid >= today_open)
            else:  # short fade of an up-thrust: profit when mid falls back to open
                tgt_pos = np.flatnonzero(hmid <= today_open)
            target_ts = int(hts[int(tgt_pos[0])]) if tgt_pos.size else None

        # ---- P1: thrust price stop, else 30m ----
        if stop_ts is not None:
            px = _tmf_exit_price(tmf_bbo, stop_ts, d)
            p1_net = ((px - entry) * d - COST_PTS) if px is not None else p0_net
            p1_mode = "stop_exit" if px is not None else "stop_no_quote_fallback_30m"
        else:
            p1_net, p1_mode = p0_net, "held_to_30m"

        # ---- P2: reversion target + same stop, earliest wins, else 30m ----
        candidates = []
        if stop_ts is not None:
            candidates.append((stop_ts, "stop"))
        if target_ts is not None:
            candidates.append((target_ts, "target"))
        if candidates:
            first_ts, first_kind = min(candidates, key=lambda c: c[0])
            px = _tmf_exit_price(tmf_bbo, first_ts, d)
            if px is None:
                p2_net, p2_mode = p0_net, f"{first_kind}_no_quote_fallback_30m"
            else:
                p2_net = (px - entry) * d - COST_PTS
                p2_mode = f"{first_kind}_exit"
        else:
            p2_net, p2_mode = p0_net, "held_to_30m"

        rows.append(
            {
                "contract": f"{txf_contract}->{tmf_contract}",
                "date": date_str,
                "direction": d,
                "thrust_pts": event.realized_vol_ratio,
                "today_open": today_open,
                "tmf_entry": entry,
                "stop_breach_in_hold": stop_ts is not None,
                "reversion_target_hit_in_hold": target_ts is not None,
                "P0_frozen_30m_time_net": round(p0_net, 2),
                "P1_thrust_price_stop_net": round(p1_net, 2),
                "P1_mode": p1_mode,
                "P2_reversion_target_net": round(p2_net, 2),
                "P2_mode": p2_mode,
            }
        )
    return rows


def _score(nets: list[float]) -> dict[str, object]:
    if not nets:
        return {"events": 0}
    return {
        "events": len(nets),
        "mean_net": round(mean(nets), 2),
        "median_net": round(median(nets), 2),
        "min_net": round(min(nets), 2),
        "max_net": round(max(nets), 2),
        "total_net": round(sum(nets), 2),
        "positive_fraction": round(sum(1 for n in nets if n > 0) / len(nets), 3),
        "clears_10pt_floor_on_mean": bool(mean(nets) >= 10.0),
    }


def main() -> None:
    pairs = _settlement_day_pairs(RAW_DIR, MONTHS)
    rows: list[dict[str, object]] = []
    for txf_path, tmf_path in pairs:
        rows.extend(diagnose_pair(txf_path, tmf_path))
    rows.sort(key=lambda r: str(r["date"]))

    def col(name: str, oos: bool = False) -> list[float]:
        return [
            float(r[name]) for r in rows if (not oos or str(r["date"]) >= OOS_START)
        ]

    result = {
        "schema": "research.t1f_fade_consistent_exit_overlay.v1",
        "diagnostic_type": "review_hypothesis_additive",
        "literature_basis": "Kaminski & Lo (2013) lit_refresh_20260607_026",
        "frozen_contract_changed": False,
        "cost_model_changed": False,
        "parameters_changed": False,
        "production_behavior_changed": False,
        "inference_policy": "descriptive_only_N3_no_significance_no_promotion_not_tuned_to_E6",
        "horizon_minutes": PRIMARY_HORIZON_MINUTES,
        "cost_pts": COST_PTS,
        "policies": {
            "P0_frozen_30m_time": "pure 30m hold, no price stop (frozen headline; no drawdown control)",
            "P1_thrust_price_stop": "exit at first thrust-extension breach else 30m (iter-14 realised; drawdown-controlled)",
            "P2_reversion_target": "fade-consistent: stop OR full reversion-to-open, earliest wins, else 30m",
        },
        "events": rows,
        "scorecards_full": {
            "P0_frozen_30m_time": _score(col("P0_frozen_30m_time_net")),
            "P1_thrust_price_stop": _score(col("P1_thrust_price_stop_net")),
            "P2_reversion_target": _score(col("P2_reversion_target_net")),
        },
        "scorecards_oos": {
            "P0_frozen_30m_time": _score(col("P0_frozen_30m_time_net", oos=True)),
            "P1_thrust_price_stop": _score(col("P1_thrust_price_stop_net", oos=True)),
            "P2_reversion_target": _score(col("P2_reversion_target_net", oos=True)),
        },
    }
    (OUT_DIR / "fade_consistent_exit_overlay.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
