"""Iteration-14 review_hypothesis ADDITIVE diagnostic for t1f_txf_expiration_vreversal_tmf.

This script does NOT modify the frozen T1-F detector, cost model, parameters, or the
production trading path.  It imports the frozen functions from
``research.t1.regime_viability`` and, for each already-detected settlement event,
re-derives a *stop-exit-adjusted realised return* that answers the iteration-13
confound:

    The frozen headline ``net_after_cost_30m`` always holds the position for the full
    30-minute horizon, even when the (TXF) stop structure is breached during the hold.
    For E6 the headline is +214 *with the stop breached*, i.e. an optimistic upper
    bound rather than a realisable fill.

The realised model:
  * Entry is identical to the frozen audit (TMF ask if long, bid if short, at trigger).
  * The stop level is the same one the frozen detector records: a long fades a
    down-thrust and stops at ``opening_range_low`` (thrust_low - buffer); a short fades
    an up-thrust and stops at ``opening_range_high`` (thrust_high + buffer).
  * We find the FIRST TXF mid timestamp within [trigger, trigger+30m] that breaches the
    stop.  If one exists, the position is exited at the prevailing TMF quote at/after
    that timestamp (TMF bid if long, ask if short) -- a realised stop loss -- instead of
    being held to 30m.
  * If the stop is not breached within the 30m hold, the realised return equals the
    frozen 30m return.
  * Full frozen cost (8.0 pts) is deducted in every case.

Output is descriptive only.  N is tiny (3 settlement events); no significance claim,
no re-parameterisation, no promotion implication is drawn here.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, median

import numpy as np

from research.t1.regime_viability import (
    NS_PER_MINUTE,
    ExpirationVReversalConfig,
    _load_frames,
    _session_start_ns,
    _settlement_day_pairs,
    _date_from_path,
    detect_expiration_v_reversal_events,
    evaluate_executable_returns,
)

# Frozen V0 contract (mirrors research/t1/regime_viability.py CLI defaults; NOT changed).
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


def _tmf_quote_at(tmf_bbo, ts_ns: int, direction: int) -> tuple[int, float] | None:
    """Return (index, exit price) for the first TMF quote at/after ts_ns.

    Exit a long at the bid, a short at the ask (the price we can actually hit)."""
    idx = int(np.searchsorted(tmf_bbo.ts_ns, ts_ns, side="left"))
    if idx >= len(tmf_bbo.ts_ns):
        return None
    px = float(tmf_bbo.bid[idx] if direction > 0 else tmf_bbo.ask[idx])
    return idx, px


def diagnose_pair(txf_path: Path, tmf_path: Path) -> list[dict[str, object]]:
    date_str = _date_from_path(txf_path)
    txf_contract = txf_path.name.split("_", 1)[0]
    tmf_contract = tmf_path.name.split("_", 1)[0]
    today_bbo, today_trades = _load_frames(txf_path)
    tmf_bbo, _ = _load_frames(tmf_path)
    config = ExpirationVReversalConfig(
        session_start_ns=_session_start_ns(date_str, tz_offset_hours=8),
        session_minutes=SESSION_MINUTES,
        thrust_window_minutes=THRUST_WINDOW_MINUTES,
        min_thrust_pts=MIN_THRUST_PTS,
        stop_buffer_pts=STOP_BUFFER_PTS,
    )
    rows: list[dict[str, object]] = []
    for event in detect_expiration_v_reversal_events(
        today_bbo, today_trades, contract=txf_contract, date=date_str, config=config
    ):
        try:
            eval_row = evaluate_executable_returns(
                tmf_bbo, trigger_time_ns=event.trigger_time_ns, direction=event.direction
            )
        except ValueError:
            continue
        gross_30m = eval_row.get("return_30m")
        if gross_30m is None:
            continue
        frozen_net = float(gross_30m) - COST_PTS

        # TMF entry (identical to frozen).
        entry_idx = int(np.searchsorted(tmf_bbo.ts_ns, event.trigger_time_ns, side="left"))
        entry = float(tmf_bbo.ask[entry_idx] if event.direction > 0 else tmf_bbo.bid[entry_idx])

        # Stop breach search restricted to the 30m realised hold window on the TXF path.
        horizon_ns = event.trigger_time_ns + PRIMARY_HORIZON_MINUTES * NS_PER_MINUTE
        hold_mask = (today_bbo.ts_ns >= event.trigger_time_ns) & (today_bbo.ts_ns <= horizon_ns)
        hold_ts = today_bbo.ts_ns[hold_mask]
        hold_mid = today_bbo.mid[hold_mask]
        if event.direction > 0:  # long: stop if TXF mid falls to/below the low stop
            breach_pos = np.flatnonzero(hold_mid <= event.opening_range_low)
        else:  # short: stop if TXF mid rises to/above the high stop
            breach_pos = np.flatnonzero(hold_mid >= event.opening_range_high)

        breached_in_hold = bool(breach_pos.size)
        if breached_in_hold:
            breach_ts = int(hold_ts[int(breach_pos[0])])
            quote = _tmf_quote_at(tmf_bbo, breach_ts, event.direction)
            if quote is None:
                # No executable TMF quote before data end -> fall back to frozen.
                realised_net = frozen_net
                exit_mode = "stop_breach_no_tmf_quote_fallback_30m"
                exit_price = None
            else:
                _, exit_price = quote
                realised_gross = (exit_price - entry) * event.direction
                realised_net = float(realised_gross) - COST_PTS
                exit_mode = "stop_exit"
        else:
            breach_ts = None
            realised_net = frozen_net
            exit_mode = "held_to_30m"
            exit_price = None

        rows.append(
            {
                "contract": f"{txf_contract}->{tmf_contract}",
                "date": date_str,
                "direction": event.direction,
                "thrust_pts": event.realized_vol_ratio,
                "tmf_entry": entry,
                "stop_high": event.opening_range_high,
                "stop_low": event.opening_range_low,
                "frozen_net_30m": round(frozen_net, 2),
                "stop_breached_in_hold": breached_in_hold,
                "exit_mode": exit_mode,
                "stop_exit_price": exit_price,
                "realised_net_after_cost": round(realised_net, 2),
                "realised_minus_frozen": round(realised_net - frozen_net, 2),
            }
        )
    return rows


def _scorecard(nets: list[float]) -> dict[str, object]:
    if not nets:
        return {"events": 0}
    return {
        "events": len(nets),
        "mean_net": round(mean(nets), 2),
        "median_net": round(median(nets), 2),
        "min_net": round(min(nets), 2),
        "max_net": round(max(nets), 2),
        "positive_fraction": round(sum(1 for n in nets if n > 0) / len(nets), 3),
        "total_net": round(sum(nets), 2),
    }


def main() -> None:
    pairs = _settlement_day_pairs(RAW_DIR, MONTHS)
    all_rows: list[dict[str, object]] = []
    for txf_path, tmf_path in pairs:
        all_rows.extend(diagnose_pair(txf_path, tmf_path))
    all_rows.sort(key=lambda r: str(r["date"]))

    frozen = [float(r["frozen_net_30m"]) for r in all_rows]
    realised = [float(r["realised_net_after_cost"]) for r in all_rows]
    oos_realised = [
        float(r["realised_net_after_cost"]) for r in all_rows if str(r["date"]) >= OOS_START
    ]
    clean_realised = [
        float(r["realised_net_after_cost"]) for r in all_rows if not r["stop_breached_in_hold"]
    ]

    result = {
        "schema": "research.t1f_stop_exit_adjusted_diagnostic.v1",
        "diagnostic_type": "review_hypothesis_additive",
        "frozen_contract_changed": False,
        "cost_model_changed": False,
        "parameters_changed": False,
        "production_behavior_changed": False,
        "inference_policy": "descriptive_only_N3_no_significance_no_promotion",
        "horizon_minutes": PRIMARY_HORIZON_MINUTES,
        "cost_pts": COST_PTS,
        "events": all_rows,
        "frozen_headline_scorecard": _scorecard(frozen),
        "stop_exit_adjusted_scorecard": _scorecard(realised),
        "stop_exit_adjusted_oos_scorecard": _scorecard(oos_realised),
        "clean_only_scorecard": _scorecard(clean_realised),
        "vol_regime_dependence_hypothesis": {
            "statement": (
                "The settlement-day thrust-fade pays in HIGH-volatility settlement "
                "mornings (realized_vol >= ~300 pts over 08:45-13:30 TPE, RANGE-like "
                "trend efficiency) and is flat-to-weak in lower-volatility mornings."
            ),
            "status": "provisional_descriptive_N3",
            "supporting_observation_frozen": (
                "C6 (vol 328) +65 and E6 (vol 462) +214 vs D6 (vol 253) +25"
            ),
            "test_when_available": (
                "Re-evaluate on the stop-exit-adjusted return after the F6 (2026-06-17) "
                "and later paired settlements are exported; do not condition entry on "
                "the regime label until an independent OOS settlement confirms it."
            ),
            "falsification": (
                "If the highest-vol settlement morning produces a NEGATIVE stop-exit-"
                "adjusted realised return, the vol-regime-dependence claim is refuted."
            ),
        },
    }

    stamp_path = OUT_DIR / "stop_exit_adjusted_diagnostic.json"
    stamp_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
