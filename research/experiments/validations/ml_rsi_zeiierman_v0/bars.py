"""Build TAIFEX day-session OHLC bars from dense TXF trade prints.

Day session = 08:45-13:45 TPE (00:45-05:45 UTC, 285 min). Bars are aligned to
the session open. Gross bad prints (auction/stale outliers) are dropped via a
per-session median band before aggregation. Bars are concatenated per contract
in date order so the indicator's analog memory persists across days, while each
bar is tagged with its date / session-close flag for intraday force-flat.
"""

from __future__ import annotations

import glob
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from research.t1.regime_viability import NS_PER_MINUTE, _date_from_path, _load_frames, _session_start_ns

# session -> (open hour TPE, open minute TPE, length minutes)
SESSION_SPECS = {
    "day": (8, 45, 285),    # 08:45-13:45 TPE
    "night": (15, 0, 840),  # 15:00-05:00 TPE (after-hours)
}
OUTLIER_BAND_FRAC = 0.025  # drop trades >2.5% from session median (fat-finger / corruption clusters)
MAX_DAY_SPAN_FRAC = 0.03  # skip a day if cleaned p0.5-p99.5 span exceeds 3% (corrupted / illiquid back-month)


@dataclass
class Bars:
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    date: np.ndarray  # str per bar
    is_session_close: np.ndarray  # bool: last bar of its day
    contract: str
    bid_open: np.ndarray | None = None  # as-of best bid at this bar's open ts (NaN if no quote)
    ask_open: np.ndarray | None = None  # as-of best ask at this bar's open ts (NaN if no quote)


def _files_for_contract(raw_dir: Path, contract: str) -> list[Path]:
    cu = contract.upper()
    pat = str(raw_dir / contract / f"{cu}_*_l2.hftbt.npz")
    return sorted(Path(p) for p in glob.glob(pat))


def build_bars(
    raw_dir: Path, contract: str, bar_min: int, *, session: str = "day", min_bars_per_day: int = 10
) -> Bars:
    """Concatenate OHLC bars across all dates for a contract within one session."""
    s_hour, s_min, s_len = SESSION_SPECS[session]
    bar_ns = bar_min * NS_PER_MINUTE
    n_slots = s_len // bar_min
    o_all, h_all, l_all, c_all, v_all, d_all, close_flag = [], [], [], [], [], [], []
    bid_all, ask_all = [], []

    for path in _files_for_contract(raw_dir, contract):
        date = _date_from_path(path)
        _bbo, trd = _load_frames(path)
        # session BBO, sorted, valid quotes only (for as-of fills at each bar open)
        b_ts = np.asarray(_bbo.ts_ns)
        b_bid = np.asarray(_bbo.bid, dtype=float)
        b_ask = np.asarray(_bbo.ask, dtype=float)
        bq = (b_bid > 0) & (b_ask >= b_bid) & ((b_ask - b_bid) < 100.0)
        b_ts, b_bid, b_ask = b_ts[bq], b_bid[bq], b_ask[bq]
        b_order = np.argsort(b_ts)
        b_ts, b_bid, b_ask = b_ts[b_order], b_bid[b_order], b_ask[b_order]
        ts = np.asarray(trd.ts_ns)
        px = np.asarray(trd.price, dtype=float)
        qty = np.asarray(getattr(trd, "qty", np.ones(px.shape)), dtype=float)
        if ts.size == 0:
            continue
        s0 = _session_start_ns(date, hour=s_hour, minute=s_min)
        s_end = s0 + s_len * NS_PER_MINUTE
        m = (ts >= s0) & (ts < s_end) & (px > 0)
        if m.sum() < 50:
            continue
        ts, px, qty = ts[m], px[m], qty[m]
        # drop gross outlier prints relative to the session median
        med = float(np.median(px))
        keep = np.abs(px - med) <= OUTLIER_BAND_FRAC * med
        ts, px, qty = ts[keep], px[keep], qty[keep]
        if ts.size < 50:
            continue
        # day plausibility gate: reject corrupted / illiquid back-month days
        lo_q, hi_q = np.percentile(px, [0.5, 99.5])
        if (hi_q - lo_q) > MAX_DAY_SPAN_FRAC * med:
            continue
        slot = ((ts - s0) // bar_ns).astype(int)
        slot = np.clip(slot, 0, n_slots - 1)

        day_bars = []
        for s in range(n_slots):
            sel = slot == s
            if not np.any(sel):
                continue
            p = px[sel]
            if p.size >= 20:  # winsorize slot prints to kill single-print spikes
                wlo, whi = np.percentile(p, [1, 99])
                p = np.clip(p, wlo, whi)
            # volume = true summed contract qty in the slot (real VWAP weight)
            vol = float(qty[sel].sum())
            day_bars.append((s, float(p[0]), float(p.max()), float(p.min()), float(p[-1]), vol))
        if len(day_bars) < min_bars_per_day:
            continue
        for j, (s_idx, o, hi, lo, c, vol) in enumerate(day_bars):
            o_all.append(o)
            h_all.append(hi)
            l_all.append(lo)
            c_all.append(c)
            v_all.append(vol)
            d_all.append(date)
            close_flag.append(j == len(day_bars) - 1)
            # as-of best bid/ask prevailing at this bar's open timestamp
            slot_open = s0 + s_idx * bar_ns
            if b_ts.size:
                k = int(np.searchsorted(b_ts, slot_open, side="right")) - 1
                if k >= 0:
                    bid_all.append(float(b_bid[k]))
                    ask_all.append(float(b_ask[k]))
                else:
                    bid_all.append(np.nan)
                    ask_all.append(np.nan)
            else:
                bid_all.append(np.nan)
                ask_all.append(np.nan)

    return Bars(
        open=np.array(o_all),
        high=np.array(h_all),
        low=np.array(l_all),
        close=np.array(c_all),
        volume=np.array(v_all),
        date=np.array(d_all),
        is_session_close=np.array(close_flag, dtype=bool),
        contract=contract,
        bid_open=np.array(bid_all),
        ask_open=np.array(ask_all),
    )
