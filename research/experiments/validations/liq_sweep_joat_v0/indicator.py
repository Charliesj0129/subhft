"""Faithful port of 'Liquidity Sweep Probability [JOAT]' (officialjackofalltrades, MPL-2.0).

The published script draws equal-high/equal-low liquidity pools and a heuristic
"probability panel". Its only mechanical, tradeable event is the SWEEP -> RECLAIM
of a pool:

  Equal highs (double-top buy-side pool):
    sweep   = high wicks ABOVE the pool (high > zoneTop) but bar closes back below
              it (close < zoneTop)
    reclaim = a later bar then closes below the pool BOTTOM (close < zoneBottom)
              => failed breakout / bull trap => SHORT, target = close - 0.75*ATR

  Equal lows mirror -> LONG, target = close + 0.75*ATR.

The panel "chance" score is a hand-built display formula
(sample_rate*0.62 + 19 + distance/age/reclaim boosts, clamped 5..95) with no fit to
realized outcomes -> it is NOT a predictive probability and is NOT used as a signal.
Only the mechanical sweep->reclaim is traded, with published defaults and zero tuning.

Strictly causal: pivots confirm `PIVOT_LEN` bars late exactly as ta.pivothigh/low do;
ATR (Wilder RMA) and every zone transition read only bars <= i.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Published defaults (verbatim from the script inputs).
PIVOT_LEN = 5
EQUAL_ATR_MULT = 0.18
ZONE_ATR_MULT = 0.08
ATR_LEN = 21
RECLAIM_BARS = 8
OUTCOME_BARS = 20
TARGET_ATR_MULT = 0.75
MAX_ZONES = 18
MINTICK = 1.0  # TXF index tick = 1 point


@dataclass
class Signals:
    reclaim_long: np.ndarray   # bool: low-pool reclaim confirmed at close[i] -> long
    reclaim_short: np.ndarray  # bool: high-pool reclaim confirmed at close[i] -> short
    target_long: np.ndarray    # TP price for a long reclaim at i (nan otherwise)
    target_short: np.ndarray   # TP price for a short reclaim at i (nan otherwise)
    stop_long: np.ndarray      # protective stop = swept extreme low (nan otherwise)
    stop_short: np.ndarray     # protective stop = swept extreme high (nan otherwise)
    diag: dict


def _wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int) -> np.ndarray:
    """ta.atr(length): RMA (Wilder) of true range, seeded with the SMA of the first `length` TRs."""
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = np.full(n, np.nan)
    if n >= length:
        atr[length - 1] = float(tr[:length].mean())
        for i in range(length, n):
            atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / length
    return atr


def _pivots(high: np.ndarray, low: np.ndarray, length: int) -> tuple[np.ndarray, np.ndarray]:
    """ta.pivothigh/pivotlow(length, length): confirmed at bar i for candidate bar i-length.

    Candidate is a pivot high iff strictly greater than every other high in the
    [i-2*length, i] window (mirror for pivot low). Returns the pivot VALUE at the
    confirmation bar i (nan if none), matching Pine's non-na semantics.
    """
    n = len(high)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    for i in range(2 * length, n):
        c = i - length
        others_h = np.concatenate([high[i - 2 * length:c], high[c + 1:i + 1]])
        if high[c] > others_h.max():
            ph[i] = high[c]
        others_l = np.concatenate([low[i - 2 * length:c], low[c + 1:i + 1]])
        if low[c] < others_l.min():
            pl[i] = low[c]
    return ph, pl


def _step_high_zones(zones, i, high, close, safe_atr, out, d) -> None:
    """High pool: wick above (sweep) then close below pool bottom (reclaim) -> short."""
    for z in zones:
        top, bot, state = z[0], z[1], z[2]
        if state == 0:
            if high[i] > top and close[i] < top:
                z[2], z[3], z[4] = 1, i, high[i]
                d["sweeps_high"] += 1
            elif close[i] > top:
                z[2] = 3
                d["breaks"] += 1
        elif state == 1:
            z[4] = max(z[4], high[i])  # swept extreme -> protective stop
            if close[i] < bot:
                z[2] = 2
                out["reclaim_short"][i] = True
                out["target_short"][i] = close[i] - safe_atr * TARGET_ATR_MULT
                out["stop_short"][i] = z[4]
                d["reclaims_short"] += 1
            elif close[i] > top or (i - z[3]) > RECLAIM_BARS:
                z[2] = 3
                d["breaks"] += 1


def _step_low_zones(zones, i, low, close, safe_atr, out, d) -> None:
    """Low pool: wick below (sweep) then close above pool top (reclaim) -> long."""
    for z in zones:
        top, bot, state = z[0], z[1], z[2]
        if state == 0:
            if low[i] < bot and close[i] > bot:
                z[2], z[3], z[4] = 1, i, low[i]
                d["sweeps_low"] += 1
            elif close[i] < bot:
                z[2] = 3
                d["breaks"] += 1
        elif state == 1:
            z[4] = min(z[4], low[i])
            if close[i] > top:
                z[2] = 2
                out["reclaim_long"][i] = True
                out["target_long"][i] = close[i] + safe_atr * TARGET_ATR_MULT
                out["stop_long"][i] = z[4]
                d["reclaims_long"] += 1
            elif close[i] < bot or (i - z[3]) > RECLAIM_BARS:
                z[2] = 3
                d["breaks"] += 1


def compute_signals(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, date: np.ndarray
) -> Signals:
    n = len(close)
    atr = _wilder_atr(high, low, close, ATR_LEN)
    ph, pl = _pivots(high, low, PIVOT_LEN)

    out = {
        "reclaim_long": np.zeros(n, dtype=bool),
        "reclaim_short": np.zeros(n, dtype=bool),
        "target_long": np.full(n, np.nan),
        "target_short": np.full(n, np.nan),
        "stop_long": np.full(n, np.nan),
        "stop_short": np.full(n, np.nan),
    }

    # active pools per side; each: [top, bottom, state, sweep_bar, extreme]
    # state: 0 formed, 1 swept, 2 reclaimed (emitted), 3 broken
    high_zones: list[list] = []
    low_zones: list[list] = []
    last_high_pivot = np.nan
    last_low_pivot = np.nan

    d = {"zones_high": 0, "zones_low": 0, "sweeps_high": 0, "sweeps_low": 0,
         "reclaims_short": 0, "reclaims_long": 0, "breaks": 0}

    for i in range(n):
        base = atr[i] if not np.isnan(atr[i]) else (high[i] - low[i])
        safe_atr = max(base, MINTICK)
        tol = safe_atr * EQUAL_ATR_MULT
        pad = safe_atr * ZONE_ATR_MULT

        # --- pool formation on confirmed equal pivots ---
        if not np.isnan(ph[i]):
            if not np.isnan(last_high_pivot) and abs(ph[i] - last_high_pivot) <= tol:
                top = max(ph[i], last_high_pivot) + pad
                bot = min(ph[i], last_high_pivot) - pad
                high_zones.append([top, bot, 0, -1, np.nan])
                d["zones_high"] += 1
                if len(high_zones) > MAX_ZONES:
                    high_zones.pop(0)
            last_high_pivot = ph[i]
        if not np.isnan(pl[i]):
            if not np.isnan(last_low_pivot) and abs(pl[i] - last_low_pivot) <= tol:
                top = max(pl[i], last_low_pivot) + pad
                bot = min(pl[i], last_low_pivot) - pad
                low_zones.append([top, bot, 0, -1, np.nan])
                d["zones_low"] += 1
                if len(low_zones) > MAX_ZONES:
                    low_zones.pop(0)
            last_low_pivot = pl[i]

        _step_high_zones(high_zones, i, high, close, safe_atr, out, d)
        _step_low_zones(low_zones, i, low, close, safe_atr, out, d)

    return Signals(
        reclaim_long=out["reclaim_long"],
        reclaim_short=out["reclaim_short"],
        target_long=out["target_long"],
        target_short=out["target_short"],
        stop_long=out["stop_long"],
        stop_short=out["stop_short"],
        diag=d,
    )
