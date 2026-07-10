"""Causal Python port of LuxAlgo 'Order Flow VWAP Deviation' (Pine v6).

Ports the per-bar, tradeable order-flow components verbatim from the published
source (defaults unchanged):

* Session-anchored VWAP with +/- stdDevMult volume-weighted std-dev bands
  (vValue / vUpper / vLower), reset on each new session (date change).
* Significant pivot 'stop zones' (ta.pivothigh/low, lookback 50) and the
  stop-run sweep flags: a stored pivot level consumed by price on volume
  > 1.2x SMA20(volume) -> stopTriggeredUpper / stopTriggeredLower.
* Inversion fair-value gaps (IFVGs): a bear FVG whose top is reclaimed by close
  becomes bullish support; a bull FVG whose bottom is lost becomes bearish
  resistance; mitigated when close breaks back through. The volatility filter
  (avgBody*1.2) is honoured. (showIFVG is a display toggle in Pine; the logic is
  computed here unconditionally so the IFVG rule can be tested.)

From these, four per-bar entry-signal pairs are emitted for the backtest:
  - cross_long / cross_short : close crossing vValue (VWAP reclaim / loss)
  - fade_long  / fade_short  : close beyond vLower / vUpper (mean-revert to VWAP)
  - sweep_long / sweep_short : stop-run sweep reversal (sweep low -> long)
  - ifvg_long  / ifvg_short  : re-entry into an active bullish / bearish IFVG

Everything is strictly causal: bar i reads only information available at the
close of bar i (pivots are detected 'lookback' bars after they occur, exactly
as ta.pivothigh delays its output).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from research.experiments.validations.ml_rsi_zeiierman_v0.indicator import _sma

# --- published Pine defaults (verbatim) ------------------------------------
STD_DEV_MULT = 2.0
PIVOT_LOOKBACK = 50
MAX_ACTIVE_LINES = 10
STOP_VOL_MULT = 1.2          # hardcoded in the sweep trigger (not the VP threshold)
IFVG_VOL_FILTER = 1.2
IFVG_HISTORY = 5
AVG_VOL_LEN = 20
AVG_BODY_LEN = 50


@dataclass
class Signals:
    vwap: np.ndarray
    vupper: np.ndarray
    vlower: np.ndarray
    cross_long: np.ndarray
    cross_short: np.ndarray
    fade_long: np.ndarray
    fade_short: np.ndarray
    sweep_long: np.ndarray
    sweep_short: np.ndarray
    ifvg_long: np.ndarray
    ifvg_short: np.ndarray
    diag: dict = field(default_factory=dict)


def _session_vwap(hlc3: np.ndarray, vol: np.ndarray, date: np.ndarray, mult: float):
    """Volume-weighted VWAP and +/- mult*stdev bands, reset on date change."""
    n = len(hlc3)
    vwap = np.full(n, np.nan)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    cum_v = cum_pv = cum_p2v = 0.0
    for i in range(n):
        if i == 0 or date[i] != date[i - 1]:
            cum_v = cum_pv = cum_p2v = 0.0
        w = vol[i] if (not np.isnan(vol[i]) and vol[i] > 0) else 0.0
        p = hlc3[i]
        cum_v += w
        cum_pv += w * p
        cum_p2v += w * p * p
        if cum_v > 0:
            vw = cum_pv / cum_v
            var = max(0.0, cum_p2v / cum_v - vw * vw)
            sd = np.sqrt(var)
            vwap[i] = vw
            upper[i] = vw + mult * sd
            lower[i] = vw - mult * sd
    return vwap, upper, lower


def _pivots(high: np.ndarray, low: np.ndarray, lb: int):
    """ta.pivothigh/pivotlow(lb, lb): strict pivot detected lb bars later."""
    n = len(high)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    for i in range(2 * lb, n):
        c = i - lb
        if c - lb < 0:
            continue
        hc = high[c]
        if hc > high[c - lb:c].max() and hc > high[c + 1:i + 1].max():
            ph[i] = hc
        lc = low[c]
        if lc < low[c - lb:c].min() and lc < low[c + 1:i + 1].min():
            pl[i] = lc
    return ph, pl


def _stop_sweeps(high, low, ph, pl, vol, avg_vol, max_lines):
    """Replicate the active-pivot-line sweep logic -> stop-run flags."""
    n = len(high)
    sweep_up = np.zeros(n, dtype=bool)   # high pivot swept (sell-side liquidity)
    sweep_dn = np.zeros(n, dtype=bool)   # low pivot swept (buy-side liquidity)
    upper_levels: list[float] = []       # newest first (unshift)
    lower_levels: list[float] = []
    for i in range(n):
        if not np.isnan(ph[i]):
            upper_levels.insert(0, float(ph[i]))
            if len(upper_levels) > max_lines:
                upper_levels.pop()
        if not np.isnan(pl[i]):
            lower_levels.insert(0, float(pl[i]))
            if len(lower_levels) > max_lines:
                lower_levels.pop()
        hot = (not np.isnan(avg_vol[i])) and (vol[i] > avg_vol[i] * STOP_VOL_MULT)
        # consume any level price has reached this bar
        kept_u = []
        for lvl in upper_levels:
            if high[i] >= lvl:
                if hot:
                    sweep_up[i] = True
            else:
                kept_u.append(lvl)
        upper_levels = kept_u
        kept_l = []
        for lvl in lower_levels:
            if low[i] <= lvl:
                if hot:
                    sweep_dn[i] = True
            else:
                kept_l.append(lvl)
        lower_levels = kept_l
    return sweep_up, sweep_dn


def _ifvg(open_, high, low, close, avg_body, vol_filter):  # noqa: C901 — faithful FVG state machine
    """Inversion FVG state machine -> re-entry long/short triggers."""
    n = len(close)
    ifvg_long = np.zeros(n, dtype=bool)
    ifvg_short = np.zeros(n, dtype=bool)
    pend_bear: list[tuple[float, float]] = []  # (top=low[i-2], btm=high@form)
    pend_bull: list[tuple[float, float]] = []  # (top=low@form, btm=high[i-2])
    boxes: list[dict] = []                     # active IFVG boxes {top,btm,is_bull}
    for i in range(n):
        if i >= 2:
            vvalid = abs(close[i - 1] - open_[i - 1]) > avg_body[i] * vol_filter if not np.isnan(avg_body[i]) else False
            if vvalid and high[i] < low[i - 2]:
                pend_bear.insert(0, (low[i - 2], high[i]))
            if vvalid and low[i] > high[i - 2]:
                pend_bull.insert(0, (low[i], high[i - 2]))
        # inversions: bear FVG reclaimed (close>top) -> bullish IFVG; bull FVG lost -> bearish
        keep_bear = []
        for top, btm in pend_bear:
            if close[i] > top:
                boxes.insert(0, {"top": top, "btm": btm, "is_bull": True})
                if len(boxes) > IFVG_HISTORY:
                    boxes.pop()
            else:
                keep_bear.append((top, btm))
        pend_bear = keep_bear
        keep_bull = []
        for top, btm in pend_bull:
            if close[i] < btm:
                boxes.insert(0, {"top": top, "btm": btm, "is_bull": False})
                if len(boxes) > IFVG_HISTORY:
                    boxes.pop()
            else:
                keep_bull.append((top, btm))
        pend_bull = keep_bull
        # re-entry continuation triggers (using prior close to detect the cross-in)
        if i >= 1:
            for b in boxes:
                if b["is_bull"] and close[i] <= b["top"] and close[i - 1] > b["top"]:
                    ifvg_long[i] = True
                elif (not b["is_bull"]) and close[i] >= b["btm"] and close[i - 1] < b["btm"]:
                    ifvg_short[i] = True
        # mitigation: bull box lost (close<btm) / bear box reclaimed (close>top)
        boxes = [b for b in boxes if not ((b["is_bull"] and close[i] < b["btm"])
                                          or ((not b["is_bull"]) and close[i] > b["top"]))]
    return ifvg_long, ifvg_short


def compute_signals(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    date: np.ndarray,
) -> Signals:
    """Run the full per-bar order-flow engine and return causal entry signals."""
    open_ = open_.astype(float)
    high = high.astype(float)
    low = low.astype(float)
    close = close.astype(float)
    vol = volume.astype(float)
    hlc3 = (high + low + close) / 3.0

    vwap, vupper, vlower = _session_vwap(hlc3, vol, date, STD_DEV_MULT)

    # VWAP cross (reclaim / loss)
    n = len(close)
    cross_long = np.zeros(n, dtype=bool)
    cross_short = np.zeros(n, dtype=bool)
    fade_long = np.zeros(n, dtype=bool)
    fade_short = np.zeros(n, dtype=bool)
    for i in range(1, n):
        if np.isnan(vwap[i]) or np.isnan(vwap[i - 1]):
            continue
        if close[i] > vwap[i] and close[i - 1] <= vwap[i - 1]:
            cross_long[i] = True
        elif close[i] < vwap[i] and close[i - 1] >= vwap[i - 1]:
            cross_short[i] = True
        if not np.isnan(vlower[i]) and close[i] <= vlower[i]:
            fade_long[i] = True
        if not np.isnan(vupper[i]) and close[i] >= vupper[i]:
            fade_short[i] = True

    avg_vol = _sma(vol, AVG_VOL_LEN)
    ph, pl = _pivots(high, low, PIVOT_LOOKBACK)
    sweep_up, sweep_dn = _stop_sweeps(high, low, ph, pl, vol, avg_vol, MAX_ACTIVE_LINES)
    # reversal: a swept low (buy-side liquidity grab) -> long; swept high -> short
    sweep_long, sweep_short = sweep_dn, sweep_up

    avg_body = _sma(np.abs(close - open_), AVG_BODY_LEN)
    ifvg_long, ifvg_short = _ifvg(open_, high, low, close, avg_body, IFVG_VOL_FILTER)

    return Signals(
        vwap=vwap, vupper=vupper, vlower=vlower,
        cross_long=cross_long, cross_short=cross_short,
        fade_long=fade_long, fade_short=fade_short,
        sweep_long=sweep_long, sweep_short=sweep_short,
        ifvg_long=ifvg_long, ifvg_short=ifvg_short,
        diag={
            "n_bars": n,
            "n_cross": int(cross_long.sum() + cross_short.sum()),
            "n_fade": int(fade_long.sum() + fade_short.sum()),
            "n_sweep": int(sweep_long.sum() + sweep_short.sum()),
            "n_ifvg": int(ifvg_long.sum() + ifvg_short.sum()),
            "n_pivots": int((~np.isnan(ph)).sum() + (~np.isnan(pl)).sum()),
        },
    )
