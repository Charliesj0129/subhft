"""PoC-reversion signal from a faithful rolling DVP-Pro volume profile.

Causal construction (bar i reads only bars <= i):

  Rolling profile over the trailing `LOOKBACK` bars (DVP-Pro default 200), `BINS`
  price bins (default 60) spanning [min low, max high] of the window. Each bar's
  true contract volume is dropped into the bin holding its close (matching the
  script's price=close[i] assignment).

    * PoC  = centre of the max-volume bin (the volume mode / "fair value" magnet).
    * Value Area (70%) = greedily grow outward from the PoC bin, each step adding
      the heavier adjacent bin, until cumulative volume >= 70% of the window total
      (the universal volume-profile convention -- not a tuned threshold).

  Entry (the reversion event): a bar whose close pushes *outside* the Value Area
  for the first time (prev close was inside) -> fade back toward the PoC.
    * close > VA_high -> SHORT;  target = PoC,  structural stop = profile top.
    * close < VA_low  -> LONG;   target = PoC,  structural stop = profile bottom.

  Both the target (PoC) and stop (profile extreme) are observables of the same
  window -- no tuned distance. Zero free parameters beyond the script's own
  published defaults.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# DVP-Pro published defaults (verbatim from the script inputs).
LOOKBACK = 200       # input lookBack (Session Mode OFF)
BINS = 60            # input bins
VALUE_AREA = 0.70    # standard 70% value-area convention
MIN_WINDOW = 50      # need a minimally-populated profile before trusting PoC/VA


@dataclass
class Setups:
    enter_long: np.ndarray    # bool: close broke below the Value Area -> fade up to PoC
    enter_short: np.ndarray   # bool: close broke above the Value Area -> fade down to PoC
    stop_long: np.ndarray     # structural stop = profile bottom (nan otherwise)
    stop_short: np.ndarray    # structural stop = profile top (nan otherwise)
    target_long: np.ndarray   # target = PoC price (nan otherwise)
    target_short: np.ndarray  # target = PoC price (nan otherwise)
    diag: dict


def _profile_levels(close_w: np.ndarray, vol_w: np.ndarray, bot: float, top: float) -> tuple[float, float, float]:
    """Return (poc_price, va_low, va_high) for one trailing window. Caller guards top>bot."""
    step = (top - bot) / BINS
    idx = ((close_w - bot) / step).astype(int)
    np.clip(idx, 0, BINS - 1, out=idx)
    bin_vol = np.zeros(BINS)
    np.add.at(bin_vol, idx, vol_w)

    poc = int(np.argmax(bin_vol))
    poc_price = bot + step * (poc + 0.5)

    total = bin_vol.sum()
    target_vol = VALUE_AREA * total
    lo = hi = poc
    acc = bin_vol[poc]
    while acc < target_vol and (lo > 0 or hi < BINS - 1):
        below = bin_vol[lo - 1] if lo > 0 else -1.0
        above = bin_vol[hi + 1] if hi < BINS - 1 else -1.0
        if above >= below:
            hi += 1
            acc += bin_vol[hi]
        else:
            lo -= 1
            acc += bin_vol[lo]
    va_low = bot + step * lo
    va_high = bot + step * (hi + 1)
    return poc_price, va_low, va_high


def compute_setups(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
    volume: np.ndarray, date: np.ndarray
) -> Setups:
    n = len(close)
    enter_long = np.zeros(n, dtype=bool)
    enter_short = np.zeros(n, dtype=bool)
    stop_long = np.full(n, np.nan)
    stop_short = np.full(n, np.nan)
    target_long = np.full(n, np.nan)
    target_short = np.full(n, np.nan)

    d = {"excursions_up": 0, "excursions_dn": 0, "entries_long": 0, "entries_short": 0}
    prev_inside = True  # treat the start as "inside" so the first excursion is an event

    for i in range(n):
        if i < MIN_WINDOW:
            continue
        lo_w = max(0, i - LOOKBACK + 1)
        close_w = close[lo_w:i + 1]
        vol_w = volume[lo_w:i + 1]
        top = float(high[lo_w:i + 1].max())
        bot = float(low[lo_w:i + 1].min())
        if not (top > bot) or vol_w.sum() <= 0:
            continue

        poc_price, va_low, va_high = _profile_levels(close_w, vol_w, bot, top)
        c = close[i]
        inside = va_low <= c <= va_high

        # fire only on the fresh excursion out of the Value Area (was inside, now out)
        if (not inside) and prev_inside:
            if c > va_high and poc_price < c:        # extended above value -> fade down to PoC
                d["excursions_up"] += 1
                enter_short[i] = True
                target_short[i] = poc_price
                stop_short[i] = top
                d["entries_short"] += 1
            elif c < va_low and poc_price > c:       # extended below value -> fade up to PoC
                d["excursions_dn"] += 1
                enter_long[i] = True
                target_long[i] = poc_price
                stop_long[i] = bot
                d["entries_long"] += 1
        prev_inside = inside

    return Setups(
        enter_long=enter_long,
        enter_short=enter_short,
        stop_long=stop_long,
        stop_short=stop_short,
        target_long=target_long,
        target_short=target_short,
        diag=d,
    )
