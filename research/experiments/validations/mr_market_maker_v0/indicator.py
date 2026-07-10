"""Faithful port of 'Mr. Market Maker' (MR.MM, officialjackofalltrades, MPL-2.0).

The published script is an *indicator* (no strategy entries). Its one mechanical,
tradeable setup is the CHoCH (Change of Character) + Fibonacci entry zone:

  Structure tracking (causal):
    * ta.pivothigh/low(pLen, pLen) confirm `pLen` bars late (len=10 -> pLen=5).
    * upLvl / dnLvl = the latest confirmed swing high / low; each carries a
      `broken` flag reset whenever a fresh pivot prints.
    * requireBody=true: a break needs a CLOSE beyond the level.
    * os = market-structure direction (+1 last broke up, -1 last broke down).

  CHoCH = a break that flips os against the prior structure (the script's only
  default-on engine; BOS is default-off and not traded here):
    * brkUp & trendBullish(close>EMA200) while os==-1  -> bullish CHoCH
        anchor = dnLvl (prior swing low), trigger = high[i] (breakout extreme)
    * brkDn & trendBearish(close<EMA200) while os==+1  -> bearish CHoCH
        anchor = upLvl (prior swing high), trigger = low[i]

  Entry zone (the script's "ENTRY ZONE" / 0.382-0.618 Gann/Fib band of the leg):
        y0382 = anchor + (trigger-anchor)*0.382
        y0618 = anchor + (trigger-anchor)*0.618
    The published alert fires when a bar CLOSES back inside [min,max] of that
    band -> a retracement limit entry in the CHoCH direction.

Only the latest CHoCH setup is monitored at a time (showLastN=1 per engine).
The "probability/decoration" labels are display-only and not traded. Published
defaults verbatim, zero tuning. Strictly causal: pivots confirm pLen bars late;
EMA and every structure transition read only bars <= i.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Published defaults (verbatim from the script inputs).
FRACTAL_LEN = 10
PIVOT_LEN = FRACTAL_LEN // 2  # int(len/2) = 5
MA_LEN = 200
REQUIRE_BODY = True
USE_FILTER = True
FIB_LO = 0.382
FIB_HI = 0.618


@dataclass
class Setups:
    enter_long: np.ndarray    # bool: close re-entered a bullish CHoCH Fib zone at i -> long
    enter_short: np.ndarray   # bool: close re-entered a bearish CHoCH Fib zone at i -> short
    stop_long: np.ndarray     # protective stop = anchor swing low (nan otherwise)
    stop_short: np.ndarray    # protective stop = anchor swing high (nan otherwise)
    target_long: np.ndarray   # target = trigger breakout high (nan otherwise)
    target_short: np.ndarray  # target = trigger breakout low (nan otherwise)
    diag: dict


def _ema(src: np.ndarray, length: int) -> np.ndarray:
    """ta.ema(src, length): recursive EMA seeded with the first source value (Pine semantics)."""
    n = len(src)
    out = np.empty(n)
    alpha = 2.0 / (length + 1.0)
    out[0] = src[0]
    for i in range(1, n):
        out[i] = alpha * src[i] + (1.0 - alpha) * out[i - 1]
    return out


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


def compute_setups(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, date: np.ndarray
) -> Setups:
    n = len(close)
    ema = _ema(close, MA_LEN)
    ph, pl = _pivots(high, low, PIVOT_LEN)

    enter_long = np.zeros(n, dtype=bool)
    enter_short = np.zeros(n, dtype=bool)
    stop_long = np.full(n, np.nan)
    stop_short = np.full(n, np.nan)
    target_long = np.full(n, np.nan)
    target_short = np.full(n, np.nan)

    up_lvl = np.nan
    dn_lvl = np.nan
    up_broken = False
    dn_broken = False
    os = 0  # +1 last broke up, -1 last broke down

    # single active CHoCH setup (showLastN=1 per engine)
    active = None  # dict(dir, z_lo, z_hi, anchor, trigger, taken)

    d = {"choch_bull": 0, "choch_bear": 0, "entries_long": 0, "entries_short": 0}

    for i in range(n):
        # --- candidate motoru: refresh swing levels on confirmed pivots ---
        if not np.isnan(ph[i]):
            up_lvl = ph[i]
            up_broken = False
        if not np.isnan(pl[i]):
            dn_lvl = pl[i]
            dn_broken = False

        brk_up = (not np.isnan(up_lvl)) and (not up_broken) and (
            close[i] > up_lvl if REQUIRE_BODY else high[i] > up_lvl
        )
        brk_dn = (not np.isnan(dn_lvl)) and (not dn_broken) and (
            close[i] < dn_lvl if REQUIRE_BODY else low[i] < dn_lvl
        )

        trend_bull = (not USE_FILTER) or (close[i] > ema[i])
        trend_bear = (not USE_FILTER) or (close[i] < ema[i])

        # --- breaks (CHoCH engine only; Pine evaluates up then down) ---
        if brk_up and trend_bull:
            up_broken = True
            if os == -1:  # CHoCH (bullish reversal)
                anchor, trigger = dn_lvl, high[i]
                y_lo = anchor + (trigger - anchor) * FIB_LO
                y_hi = anchor + (trigger - anchor) * FIB_HI
                active = {"dir": 1, "z_lo": min(y_lo, y_hi), "z_hi": max(y_lo, y_hi),
                          "anchor": anchor, "trigger": trigger, "taken": False}
                d["choch_bull"] += 1
            os = 1

        if brk_dn and trend_bear:
            dn_broken = True
            if os == 1:  # CHoCH (bearish reversal)
                anchor, trigger = up_lvl, low[i]
                y_lo = anchor + (trigger - anchor) * FIB_LO
                y_hi = anchor + (trigger - anchor) * FIB_HI
                active = {"dir": -1, "z_lo": min(y_lo, y_hi), "z_hi": max(y_lo, y_hi),
                          "anchor": anchor, "trigger": trigger, "taken": False}
                d["choch_bear"] += 1
            os = -1

        # --- entry: price closes back into the active CHoCH Fib zone (once) ---
        if active is not None and not active["taken"]:
            if active["z_lo"] <= close[i] <= active["z_hi"]:
                active["taken"] = True
                if active["dir"] == 1:
                    enter_long[i] = True
                    stop_long[i] = active["anchor"]
                    target_long[i] = active["trigger"]
                    d["entries_long"] += 1
                else:
                    enter_short[i] = True
                    stop_short[i] = active["anchor"]
                    target_short[i] = active["trigger"]
                    d["entries_short"] += 1

    return Setups(
        enter_long=enter_long,
        enter_short=enter_short,
        stop_long=stop_long,
        stop_short=stop_short,
        target_long=target_long,
        target_short=target_short,
        diag=d,
    )
