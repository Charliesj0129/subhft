"""
R33 APLR MACD — Stage 2: Python Prototype
Active-Passive Liquidity Resonance via dual-channel MACD

Instruments:
  TXFC6 (14 near-month days: 2026-02-25 → 2026-03-18)
  TXFD6 (11 near-month days: 2026-03-19 → 2026-04-02)

Usage:
  uv run python research/alphas/aplr_macd/impl.py
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import stats

warnings.filterwarnings("ignore")

# ── Constants ──────────────────────────────────────────────────────────────────

PRICE_SCALE = 1_000_000  # golden data x1e6
BAR_SEC = 30
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
WARMUP_BARS = MACD_SLOW + MACD_SIGNAL  # 35 bars to discard

# Cost model: TAIFEX TX futures
POINT_VALUE_NTD = 200
RT_COST_PTS = 2.0  # 1 tick each way

# Forward return horizons (in bars of BAR_SEC)
FWD_HORIZONS = {"30s": 1, "90s": 3, "150s": 5, "300s": 10}

# Detrended IC gate thresholds (pre-registered in Stage 1.5)
DETRENDED_IC_MIN = 0.02
RAW_IC_MIN = 0.03
IC_RATIO_MIN = 0.50
TREND_CONTAMINATION_MAX = 0.50  # correlation with simple momentum

# Data paths
GOLDEN_TXFC6 = Path("research/data/real/golden/TXFC6")
GOLDEN_TXFD6 = Path("research/data/real/golden/TXFD6")

# Shared columns across both instruments (ignore extra columns in newer files)
SHARED_COLS = [
    "symbol", "exchange", "type", "exch_ts", "ingest_ts",
    "price_scaled", "volume", "bids_price", "bids_vol",
    "asks_price", "asks_vol", "seq_no",
]

# TXFD6 far-month dates to skip (spread > 200 pts)
TXFD6_SKIP_DATES = {
    "2026-02-05", "2026-02-06", "2026-02-07",
    "2026-02-10", "2026-02-11", "2026-02-23",
    "2026-02-24", "2026-02-25", "2026-02-26",
}


# ── Data Loading ──────────────────────────────────────────────────────────────


def load_day(path: Path) -> pd.DataFrame:
    """Load a single day's parquet, keeping only shared columns."""
    t = pq.read_table(path, columns=SHARED_COLS)
    df = t.to_pandas()
    df = df.sort_values("seq_no").reset_index(drop=True)
    return df


def discover_files() -> list[tuple[str, str, Path]]:
    """
    Return list of (contract, date_str, path) for all valid near-month days.
    Sorted by date.
    """
    files: list[tuple[str, str, Path]] = []
    # TXFC6: all files are near-month
    for f in sorted(GOLDEN_TXFC6.glob("*.parquet")):
        files.append(("TXFC6", f.stem, f))
    # TXFD6: skip far-month
    for f in sorted(GOLDEN_TXFD6.glob("*.parquet")):
        if f.stem not in TXFD6_SKIP_DATES:
            files.append(("TXFD6", f.stem, f))
    files.sort(key=lambda x: x[1])
    return files


# ── Trade Classification (Lee-Ready) ─────────────────────────────────────────


def classify_trades(ticks: pd.DataFrame, bidasks: pd.DataFrame) -> pd.DataFrame:
    """
    Lee-Ready trade classification using most recent BidAsk snapshot.
    Returns ticks with 'direction' column (+1 buy, -1 sell).
    """
    if len(ticks) == 0:
        ticks = ticks.copy()
        ticks["direction"] = np.array([], dtype=np.int8)
        return ticks

    ba_seq = bidasks["seq_no"].values
    ba_best_bid = np.array(
        [bp[0] if len(bp) > 0 else 0 for bp in bidasks["bids_price"].values],
        dtype=np.int64,
    )
    ba_best_ask = np.array(
        [ap[0] if len(ap) > 0 else 0 for ap in bidasks["asks_price"].values],
        dtype=np.int64,
    )

    tick_prices = ticks["price_scaled"].values
    tick_seqs = ticks["seq_no"].values
    n_ticks = len(ticks)
    directions = np.zeros(n_ticks, dtype=np.int8)

    prev_price: int = 0
    for i in range(n_ticks):
        tp = int(tick_prices[i])
        sn = tick_seqs[i]
        idx = np.searchsorted(ba_seq, sn, side="right") - 1

        if idx >= 0:
            bb = int(ba_best_bid[idx])
            ba = int(ba_best_ask[idx])
        else:
            bb, ba = 0, 0

        if bb > 0 and ba > 0:
            mid = (bb + ba) // 2
            if tp >= ba:
                d = 1
            elif tp <= bb:
                d = -1
            elif tp > mid:
                d = 1
            elif tp < mid:
                d = -1
            else:
                # At midpoint: tick rule
                if tp > prev_price:
                    d = 1
                elif tp < prev_price:
                    d = -1
                else:
                    d = -1  # default
        else:
            # No BidAsk context: tick rule
            if tp > prev_price:
                d = 1
            elif tp < prev_price:
                d = -1
            else:
                d = 0

        directions[i] = d
        prev_price = tp

    ticks = ticks.copy()
    ticks["direction"] = directions
    return ticks


# ── Bar Aggregation ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class BarRow:
    bar_ts: int
    cvd_delta: float
    obi: float
    mid_price: float
    volume: int
    spread_pts: float
    n_ticks: int


def aggregate_bars(
    ticks: pd.DataFrame, bidasks: pd.DataFrame, bar_sec: int = BAR_SEC
) -> pd.DataFrame:
    """
    Aggregate into time bars of bar_sec seconds.
    Returns DataFrame with bar-level features.
    """
    # Pre-extract arrays for speed
    tick_ts = ticks["exch_ts"].values if len(ticks) > 0 else np.array([], dtype=np.int64)
    tick_dir = ticks["direction"].values if "direction" in ticks.columns else np.array([], dtype=np.int8)
    tick_vol = ticks["volume"].values if len(ticks) > 0 else np.array([], dtype=np.int64)
    tick_price = ticks["price_scaled"].values if len(ticks) > 0 else np.array([], dtype=np.int64)

    ba_ts = bidasks["exch_ts"].values if len(bidasks) > 0 else np.array([], dtype=np.int64)

    # Precompute OBI and mid/spread for each BidAsk row
    n_ba = len(bidasks)
    ba_obi = np.zeros(n_ba, dtype=np.float64)
    ba_mid = np.zeros(n_ba, dtype=np.float64)
    ba_spread = np.zeros(n_ba, dtype=np.float64)

    if n_ba > 0:
        bids_price_arr = bidasks["bids_price"].values
        bids_vol_arr = bidasks["bids_vol"].values
        asks_price_arr = bidasks["asks_price"].values
        asks_vol_arr = bidasks["asks_vol"].values

        for j in range(n_ba):
            bp = bids_price_arr[j]
            bv = bids_vol_arr[j]
            ap = asks_price_arr[j]
            av = asks_vol_arr[j]
            total_bid = sum(bv) if len(bv) > 0 else 0
            total_ask = sum(av) if len(av) > 0 else 0
            denom = total_bid + total_ask
            ba_obi[j] = (total_bid - total_ask) / denom if denom > 0 else 0.0
            if len(bp) > 0 and len(ap) > 0:
                ba_mid[j] = (bp[0] + ap[0]) / 2.0
                ba_spread[j] = (ap[0] - bp[0]) / PRICE_SCALE
            else:
                ba_mid[j] = 0.0
                ba_spread[j] = 0.0

    # Determine bar boundaries
    all_ts_parts = []
    if len(tick_ts) > 0:
        all_ts_parts.append(tick_ts)
    if len(ba_ts) > 0:
        all_ts_parts.append(ba_ts)
    if not all_ts_parts:
        return pd.DataFrame()
    all_ts = np.concatenate(all_ts_parts)
    t_min, t_max = int(all_ts.min()), int(all_ts.max())
    bar_ns = bar_sec * 1_000_000_000
    bar_edges = np.arange(t_min, t_max + bar_ns, bar_ns)

    bars: list[dict] = []
    for k in range(len(bar_edges) - 1):
        t0, t1 = bar_edges[k], bar_edges[k + 1]

        # Ticks in bar
        tmask = (tick_ts >= t0) & (tick_ts < t1)
        n_ticks_bar = int(tmask.sum())
        cvd_delta = float((tick_dir[tmask] * tick_vol[tmask]).sum()) if n_ticks_bar > 0 else 0.0
        vol = int(tick_vol[tmask].sum()) if n_ticks_bar > 0 else 0

        # BidAsk in bar
        bmask = (ba_ts >= t0) & (ba_ts < t1)
        n_ba_bar = int(bmask.sum())

        if n_ticks_bar == 0 and n_ba_bar == 0:
            continue

        obi_val = float(ba_obi[bmask].mean()) if n_ba_bar > 0 else 0.0
        mid_val = float(ba_mid[bmask][-1]) if n_ba_bar > 0 else 0.0
        spread_val = float(ba_spread[bmask].mean()) if n_ba_bar > 0 else 0.0

        bars.append({
            "bar_ts": t0,
            "cvd_delta": cvd_delta,
            "obi": obi_val,
            "mid_price": mid_val,
            "volume": vol,
            "spread_pts": spread_val,
            "n_ticks": n_ticks_bar,
        })

    df = pd.DataFrame(bars)
    if len(df) > 0:
        df["cvd_cum"] = df["cvd_delta"].cumsum()
    return df


# ── MACD Computation ─────────────────────────────────────────────────────────


def compute_macd(
    series: pd.Series,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    sig: int = MACD_SIGNAL,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (macd_line, signal_line, histogram)."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=sig, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


# ── Forward Returns ──────────────────────────────────────────────────────────


def compute_forward_returns(mid: np.ndarray, horizons: dict[str, int]) -> dict[str, np.ndarray]:
    """Compute forward returns for each horizon. NaN-padded at end."""
    n = len(mid)
    result: dict[str, np.ndarray] = {}
    for name, h in horizons.items():
        fwd = np.full(n, np.nan, dtype=np.float64)
        if h < n:
            fwd[:n - h] = (mid[h:] - mid[:n - h]) / np.where(mid[:n - h] != 0, mid[:n - h], 1.0)
        result[name] = fwd
    return result


# ── IC Computation ───────────────────────────────────────────────────────────


def spearman_ic(signal: np.ndarray, returns: np.ndarray) -> tuple[float, float]:
    """Spearman rank correlation, handling NaN. Returns (rho, pvalue)."""
    mask = np.isfinite(signal) & np.isfinite(returns)
    if mask.sum() < 20:
        return np.nan, np.nan
    rho, pval = stats.spearmanr(signal[mask], returns[mask])
    return float(rho), float(pval)


# ── Detrended IC ─────────────────────────────────────────────────────────────


def detrend_returns(fwd_ret: np.ndarray, window: int = 60) -> np.ndarray:
    """Subtract rolling mean (window bars = 30 min at 30s bars) from forward returns."""
    s = pd.Series(fwd_ret)
    rolling_mean = s.rolling(window=window, min_periods=1, center=False).mean()
    return (s - rolling_mean).values


def simple_momentum(mid: np.ndarray, lookback: int = 26) -> np.ndarray:
    """Simple momentum = return over lookback bars. For trend contamination check."""
    n = len(mid)
    mom = np.full(n, np.nan, dtype=np.float64)
    for i in range(lookback, n):
        if mid[i - lookback] != 0:
            mom[i] = (mid[i] - mid[i - lookback]) / mid[i - lookback]
    return mom


# ── Quintile Analysis ────────────────────────────────────────────────────────


def quintile_returns(signal: np.ndarray, returns: np.ndarray, n_q: int = 5) -> np.ndarray:
    """
    Sort bars by signal into n_q quantiles, return mean return per quantile.
    Returns array of shape (n_q,).
    """
    mask = np.isfinite(signal) & np.isfinite(returns)
    s = signal[mask]
    r = returns[mask]
    if len(s) < n_q * 10:
        return np.full(n_q, np.nan)
    # Rank-based quintiles
    ranks = stats.rankdata(s)
    edges = np.linspace(0, len(s), n_q + 1, dtype=int)
    order = np.argsort(ranks)
    means = np.zeros(n_q)
    for q in range(n_q):
        idx = order[edges[q]:edges[q + 1]]
        means[q] = r[idx].mean()
    return means


def check_monotonicity(quintile_means: np.ndarray) -> bool:
    """
    Check if quintile means are strictly monotonically increasing.
    Returns True if monotonic (FAIL condition).
    """
    if np.any(np.isnan(quintile_means)):
        return False
    diffs = np.diff(quintile_means)
    return bool(np.all(diffs > 0))


# ── Process One Day ──────────────────────────────────────────────────────────


@dataclass
class DayResult:
    date: str
    contract: str
    n_bars: int = 0
    n_ticks_total: int = 0
    avg_spread_pts: float = 0.0
    # Zone counts
    green: int = 0
    red: int = 0
    grey: int = 0
    resonance_pct: float = 0.0
    # IC per horizon {horizon_name: (raw_ic, detrended_ic)}
    ic_raw: dict = field(default_factory=dict)
    ic_detrended: dict = field(default_factory=dict)
    # Baseline ICs for 90s horizon
    ic_macd_active: float = np.nan
    ic_macd_passive: float = np.nan
    ic_aplr: float = np.nan
    ic_cvd_ema: float = np.nan
    ic_obi_ema: float = np.nan
    # Resonance-only IC
    ic_resonance_only: float = np.nan
    ic_resonance_detrended: float = np.nan
    # MACD-level correlation
    macd_correlation: float = np.nan
    # Trade sim
    pnl_ntd: float = 0.0
    n_trades: int = 0
    avg_hold_bars: float = 0.0
    # Detrended gate
    trend_contamination: float = np.nan
    quintile_monotonic_raw: bool = False
    quintile_monotonic_detrended: bool = False
    am_pm_consistent: bool = True
    # Average resonance episode length
    avg_episode_len: float = 0.0


def process_day(contract: str, date_str: str, path: Path) -> DayResult | None:
    """Process a single trading day. Returns DayResult or None if insufficient data."""
    result = DayResult(date=date_str, contract=contract)

    df = load_day(path)
    ticks = df[df["type"] == "Tick"].copy()
    bidasks = df[df["type"] == "BidAsk"].copy()

    if len(ticks) < 10 or len(bidasks) < 10:
        print(f"  {date_str}: Insufficient data (ticks={len(ticks)}, ba={len(bidasks)}), skipping")
        return None

    # Classify trades
    ticks = classify_trades(ticks, bidasks)
    result.n_ticks_total = len(ticks)

    # Aggregate to bars
    bars = aggregate_bars(ticks, bidasks)
    if len(bars) < WARMUP_BARS + 20:
        print(f"  {date_str}: Only {len(bars)} bars, need {WARMUP_BARS + 20}, skipping")
        return None

    result.n_bars = len(bars)
    result.avg_spread_pts = float(bars["spread_pts"].mean())

    # ── MACD computation ─────────────────────────────────────────────────
    macd_active, sig_active, hist_active = compute_macd(bars["cvd_cum"])
    macd_passive, sig_passive, hist_passive = compute_macd(bars["obi"])

    # ── Baseline signals ─────────────────────────────────────────────────
    # Simple EMA baselines (span=60 bars = 30 min, matching ofi_l1_ema30s)
    cvd_ema60 = bars["cvd_cum"].ewm(span=60, adjust=False).mean()
    obi_ema60 = bars["obi"].ewm(span=60, adjust=False).mean()

    # ── Resonance classification (after warmup) ──────────────────────────
    n = len(bars)
    w = WARMUP_BARS

    active_sign = np.sign(macd_active.values)
    passive_sign = np.sign(macd_passive.values)

    zone = np.full(n, 2, dtype=np.int8)  # 0=GREEN, 1=RED, 2=GREY
    for i in range(w, n):
        if active_sign[i] > 0 and passive_sign[i] > 0:
            zone[i] = 0  # GREEN
        elif active_sign[i] < 0 and passive_sign[i] < 0:
            zone[i] = 1  # RED
        else:
            zone[i] = 2  # GREY

    result.green = int((zone[w:] == 0).sum())
    result.red = int((zone[w:] == 1).sum())
    result.grey = int((zone[w:] == 2).sum())
    total_post_warmup = n - w
    result.resonance_pct = (result.green + result.red) / total_post_warmup * 100 if total_post_warmup > 0 else 0.0

    # ── Average resonance episode length ─────────────────────────────────
    episode_lengths: list[int] = []
    cur_len = 0
    for i in range(w, n):
        if zone[i] in (0, 1):
            cur_len += 1
        else:
            if cur_len > 0:
                episode_lengths.append(cur_len)
            cur_len = 0
    if cur_len > 0:
        episode_lengths.append(cur_len)
    result.avg_episode_len = float(np.mean(episode_lengths)) if episode_lengths else 0.0

    # ── Continuous signal: aplr = macd_active * macd_passive ──────────────
    aplr_signal = macd_active.values * macd_passive.values

    # ── Forward returns ──────────────────────────────────────────────────
    mid = bars["mid_price"].values
    fwd_rets = compute_forward_returns(mid, FWD_HORIZONS)

    # ── Momentum for trend contamination ─────────────────────────────────
    momentum_26 = simple_momentum(mid, lookback=26)

    # ── MACD-level correlation ───────────────────────────────────────────
    valid_macd = np.isfinite(macd_active.values[w:]) & np.isfinite(macd_passive.values[w:])
    if valid_macd.sum() > 20:
        rho_macd, _ = stats.spearmanr(macd_active.values[w:][valid_macd], macd_passive.values[w:][valid_macd])
        result.macd_correlation = float(rho_macd)

    # ── IC computation for all horizons ──────────────────────────────────
    for hz_name, hz_bars in FWD_HORIZONS.items():
        fr = fwd_rets[hz_name]
        fr_detrended = detrend_returns(fr, window=60)

        # Raw IC (aplr signal, post-warmup)
        sig_post = aplr_signal[w:]
        fr_post = fr[w:]
        fr_det_post = fr_detrended[w:]

        ic_raw, _ = spearman_ic(sig_post, fr_post)
        ic_det, _ = spearman_ic(sig_post, fr_det_post)

        result.ic_raw[hz_name] = float(ic_raw) if not np.isnan(ic_raw) else 0.0
        result.ic_detrended[hz_name] = float(ic_det) if not np.isnan(ic_det) else 0.0

    # ── Baseline ICs at 90s horizon ──────────────────────────────────────
    fr_90s = fwd_rets["90s"][w:]
    result.ic_macd_active, _ = spearman_ic(macd_active.values[w:], fr_90s)
    result.ic_macd_passive, _ = spearman_ic(macd_passive.values[w:], fr_90s)
    result.ic_aplr, _ = spearman_ic(aplr_signal[w:], fr_90s)
    result.ic_cvd_ema, _ = spearman_ic(cvd_ema60.values[w:], fr_90s)
    result.ic_obi_ema, _ = spearman_ic(obi_ema60.values[w:], fr_90s)

    # ── Resonance-only IC ────────────────────────────────────────────────
    res_mask = (zone[w:] == 0) | (zone[w:] == 1)
    if res_mask.sum() > 50:
        result.ic_resonance_only, _ = spearman_ic(aplr_signal[w:][res_mask], fr_90s[res_mask])
        fr_90s_det = detrend_returns(fwd_rets["90s"], window=60)[w:]
        result.ic_resonance_detrended, _ = spearman_ic(aplr_signal[w:][res_mask], fr_90s_det[res_mask])

    # ── Trend contamination (correlation with momentum) ──────────────────
    mom_post = momentum_26[w:]
    mask_both = np.isfinite(aplr_signal[w:]) & np.isfinite(mom_post)
    if mask_both.sum() > 20:
        rho_trend, _ = stats.spearmanr(aplr_signal[w:][mask_both], mom_post[mask_both])
        result.trend_contamination = float(abs(rho_trend))

    # ── Quintile monotonicity check ──────────────────────────────────────
    qr_raw = quintile_returns(aplr_signal[w:], fwd_rets["90s"][w:])
    qr_det = quintile_returns(aplr_signal[w:], detrend_returns(fwd_rets["90s"], window=60)[w:])
    result.quintile_monotonic_raw = check_monotonicity(qr_raw)
    result.quintile_monotonic_detrended = check_monotonicity(qr_det)

    # ── AM/PM split-half IC consistency ──────────────────────────────────
    half = w + (n - w) // 2
    if half > w and half < n:
        am_sig = aplr_signal[w:half]
        pm_sig = aplr_signal[half:]
        fr_90s_full = fwd_rets["90s"]
        am_ret = detrend_returns(fr_90s_full, 60)[w:half]
        pm_ret = detrend_returns(fr_90s_full, 60)[half:]
        ic_am, _ = spearman_ic(am_sig, am_ret)
        ic_pm, _ = spearman_ic(pm_sig, pm_ret)
        if not np.isnan(ic_am) and not np.isnan(ic_pm):
            result.am_pm_consistent = (np.sign(ic_am) == np.sign(ic_pm))

    # ── Trade Simulation ─────────────────────────────────────────────────
    # LONG on GREEN entry, SHORT on RED entry, EXIT on GREY or opposite
    pnl_pts: list[float] = []
    hold_bars_list: list[int] = []
    position = 0  # +1 long, -1 short, 0 flat
    entry_price = 0.0
    entry_bar = 0

    for i in range(w + 1, n):
        z = zone[i]
        z_prev = zone[i - 1]

        # Entry: first bar of GREEN or RED episode
        if position == 0:
            if z == 0 and z_prev != 0:  # GREEN entry
                position = 1
                entry_price = mid[i]
                entry_bar = i
            elif z == 1 and z_prev != 1:  # RED entry
                position = -1
                entry_price = mid[i]
                entry_bar = i
        else:
            # Exit: on GREY or opposite color
            should_exit = False
            if z == 2:  # GREY
                should_exit = True
            elif position == 1 and z == 1:  # long but turned RED
                should_exit = True
            elif position == -1 and z == 0:  # short but turned GREEN
                should_exit = True

            if should_exit and entry_price > 0:
                exit_price = mid[i]
                trade_pnl_pts = position * (exit_price - entry_price) / PRICE_SCALE
                trade_pnl_pts -= RT_COST_PTS  # subtract cost
                pnl_pts.append(trade_pnl_pts)
                hold_bars_list.append(i - entry_bar)
                position = 0
                entry_price = 0.0

    # Close any open position at end of day
    if position != 0 and entry_price > 0:
        exit_price = mid[-1]
        trade_pnl_pts = position * (exit_price - entry_price) / PRICE_SCALE
        trade_pnl_pts -= RT_COST_PTS
        pnl_pts.append(trade_pnl_pts)
        hold_bars_list.append(n - 1 - entry_bar)

    result.n_trades = len(pnl_pts)
    result.pnl_ntd = float(sum(pnl_pts) * POINT_VALUE_NTD)
    result.avg_hold_bars = float(np.mean(hold_bars_list)) if hold_bars_list else 0.0

    return result


# ── Main Execution ───────────────────────────────────────────────────────────


def main() -> None:
    files = discover_files()
    print(f"Discovered {len(files)} near-month trading days")
    for c, d, p in files:
        print(f"  {c} {d}")

    # Process all days
    results: list[DayResult] = []
    for contract, date_str, path in files:
        print(f"\n{'='*70}")
        print(f"Processing {contract} {date_str}")
        print(f"{'='*70}")
        r = process_day(contract, date_str, path)
        if r is not None:
            results.append(r)
            print(f"  Bars: {r.n_bars}, Ticks: {r.n_ticks_total}, Spread: {r.avg_spread_pts:.1f} pts")
            print(f"  Zones: G={r.green} R={r.red} Grey={r.grey} ({r.resonance_pct:.1f}% resonance)")
            print(f"  MACD corr: {r.macd_correlation:.3f}")
            print(f"  IC(aplr,90s): raw={r.ic_raw.get('90s', 0):.4f} det={r.ic_detrended.get('90s', 0):.4f}")
            print(f"  Baselines(90s): active={r.ic_macd_active:.4f} passive={r.ic_macd_passive:.4f} "
                  f"cvd_ema={r.ic_cvd_ema:.4f} obi_ema={r.ic_obi_ema:.4f}")
            print(f"  Trade sim: {r.n_trades} trades, PnL={r.pnl_ntd:.0f} NTD, "
                  f"avg_hold={r.avg_hold_bars:.1f} bars")

    if not results:
        print("\nERROR: No valid days processed!")
        return

    # ── Aggregate Results ────────────────────────────────────────────────
    print(f"\n\n{'='*70}")
    print("AGGREGATE RESULTS")
    print(f"{'='*70}")

    n_days = len(results)
    txfc6_results = [r for r in results if r.contract == "TXFC6"]
    txfd6_results = [r for r in results if r.contract == "TXFD6"]

    # ── 1. Per-Day IC Table ──────────────────────────────────────────────
    print(f"\n--- Per-Day IC Table (APLR signal, all horizons) ---")
    print(f"{'Date':<12} {'Ctr':<6} {'Bars':>5} {'Sprd':>5} "
          f"{'IC30s':>7} {'IC90s':>7} {'IC150s':>7} {'IC300s':>7} "
          f"{'Det90s':>7} {'TrnCnt':>7}")
    for r in results:
        print(f"{r.date:<12} {r.contract:<6} {r.n_bars:>5} {r.avg_spread_pts:>5.1f} "
              f"{r.ic_raw.get('30s', 0):>7.4f} {r.ic_raw.get('90s', 0):>7.4f} "
              f"{r.ic_raw.get('150s', 0):>7.4f} {r.ic_raw.get('300s', 0):>7.4f} "
              f"{r.ic_detrended.get('90s', 0):>7.4f} {r.trend_contamination:>7.3f}")

    # ── 2. Pooled IC ± SE ────────────────────────────────────────────────
    print(f"\n--- Pooled IC ± SE ---")
    for hz in FWD_HORIZONS:
        raw_ics = [r.ic_raw.get(hz, 0) for r in results]
        det_ics = [r.ic_detrended.get(hz, 0) for r in results]
        raw_mean = np.mean(raw_ics)
        raw_se = np.std(raw_ics) / np.sqrt(len(raw_ics))
        det_mean = np.mean(det_ics)
        det_se = np.std(det_ics) / np.sqrt(len(det_ics))
        print(f"  {hz:>5}: Raw IC = {raw_mean:+.4f} ± {raw_se:.4f}  |  "
              f"Detrended IC = {det_mean:+.4f} ± {det_se:.4f}")

    # ── 3. IC by Contract Period ─────────────────────────────────────────
    print(f"\n--- IC by Contract Period (90s horizon) ---")
    for name, subset in [("TXFC6", txfc6_results), ("TXFD6", txfd6_results)]:
        if not subset:
            continue
        raw_ics = [r.ic_raw.get("90s", 0) for r in subset]
        det_ics = [r.ic_detrended.get("90s", 0) for r in subset]
        raw_m = np.mean(raw_ics)
        raw_s = np.std(raw_ics) / np.sqrt(len(raw_ics))
        det_m = np.mean(det_ics)
        det_s = np.std(det_ics) / np.sqrt(len(det_ics))
        print(f"  {name} ({len(subset)} days): Raw = {raw_m:+.4f} ± {raw_s:.4f} | "
              f"Det = {det_m:+.4f} ± {det_s:.4f}")

    # ── 4. Baseline Comparison (CRITICAL) ────────────────────────────────
    print(f"\n--- Baseline Comparison (90s horizon, pooled IC) ---")
    signals = {
        "macd_active (CVD MACD)": [r.ic_macd_active for r in results],
        "macd_passive (OBI MACD)": [r.ic_macd_passive for r in results],
        "aplr (active × passive)": [r.ic_aplr for r in results],
        "cvd_ema60 (simple EMA)": [r.ic_cvd_ema for r in results],
        "obi_ema60 (simple EMA)": [r.ic_obi_ema for r in results],
    }
    print(f"  {'Signal':<30} {'Mean IC':>8} {'SE':>7} {'t-stat':>7}")
    for sig_name, ics in signals.items():
        ics_clean = [x for x in ics if not np.isnan(x)]
        if not ics_clean:
            continue
        m = np.mean(ics_clean)
        se = np.std(ics_clean) / np.sqrt(len(ics_clean))
        t = m / se if se > 0 else 0
        print(f"  {sig_name:<30} {m:>+8.4f} {se:>7.4f} {t:>7.2f}")

    # ── 5. Grey-Zone Analysis ────────────────────────────────────────────
    print(f"\n--- Grey-Zone Analysis ---")
    total_green = sum(r.green for r in results)
    total_red = sum(r.red for r in results)
    total_grey = sum(r.grey for r in results)
    total_all = total_green + total_red + total_grey
    print(f"  GREEN: {total_green} ({total_green/total_all*100:.1f}%)")
    print(f"  RED:   {total_red} ({total_red/total_all*100:.1f}%)")
    print(f"  GREY:  {total_grey} ({total_grey/total_all*100:.1f}%)")
    print(f"  Resonance rate: {(total_green+total_red)/total_all*100:.1f}%")

    # IC resonance-only vs all
    res_only_ics = [r.ic_resonance_only for r in results if not np.isnan(r.ic_resonance_only)]
    res_det_ics = [r.ic_resonance_detrended for r in results if not np.isnan(r.ic_resonance_detrended)]
    all_ics = [r.ic_aplr for r in results if not np.isnan(r.ic_aplr)]
    print(f"\n  IC (all bars):       {np.mean(all_ics):+.4f} ± {np.std(all_ics)/np.sqrt(len(all_ics)):.4f}"
          if all_ics else "  IC (all bars): N/A")
    print(f"  IC (resonance only): {np.mean(res_only_ics):+.4f} ± {np.std(res_only_ics)/np.sqrt(len(res_only_ics)):.4f}"
          if res_only_ics else "  IC (resonance only): N/A")
    print(f"  IC (res, detrended): {np.mean(res_det_ics):+.4f} ± {np.std(res_det_ics)/np.sqrt(len(res_det_ics)):.4f}"
          if res_det_ics else "  IC (res, detrended): N/A")

    # Average episode length
    avg_ep = np.mean([r.avg_episode_len for r in results if r.avg_episode_len > 0])
    print(f"\n  Average resonance episode: {avg_ep:.1f} bars ({avg_ep * BAR_SEC:.0f}s)")

    # ── 6. MACD-Level Correlation ────────────────────────────────────────
    print(f"\n--- MACD-Level Correlation (Active vs Passive) ---")
    macd_corrs = [r.macd_correlation for r in results if not np.isnan(r.macd_correlation)]
    if macd_corrs:
        print(f"  Mean MACD correlation: {np.mean(macd_corrs):.3f} ± {np.std(macd_corrs)/np.sqrt(len(macd_corrs)):.3f}")
        for r in results:
            if not np.isnan(r.macd_correlation):
                print(f"    {r.date} ({r.contract}): {r.macd_correlation:+.3f}")

    # ── 7. Trade Simulation Summary ──────────────────────────────────────
    print(f"\n--- Trade Simulation (1 lot, RT cost = {RT_COST_PTS} pts) ---")
    print(f"  {'Date':<12} {'Ctr':<6} {'#Trades':>7} {'PnL(NTD)':>10} {'AvgHold':>8}")
    total_pnl = 0.0
    total_trades = 0
    for r in results:
        print(f"  {r.date:<12} {r.contract:<6} {r.n_trades:>7} {r.pnl_ntd:>10.0f} "
              f"{r.avg_hold_bars:>8.1f}")
        total_pnl += r.pnl_ntd
        total_trades += r.n_trades

    avg_hold = np.mean([r.avg_hold_bars for r in results if r.n_trades > 0])
    print(f"\n  Total: {total_trades} trades, PnL = {total_pnl:.0f} NTD")
    print(f"  Avg PnL/trade: {total_pnl/total_trades:.0f} NTD" if total_trades > 0 else "  No trades")
    print(f"  Avg holding: {avg_hold:.1f} bars ({avg_hold * BAR_SEC:.0f}s)" if not np.isnan(avg_hold) else "")

    # Max drawdown
    cum_pnl = np.cumsum([r.pnl_ntd for r in results])
    running_max = np.maximum.accumulate(cum_pnl)
    drawdowns = cum_pnl - running_max
    max_dd = float(drawdowns.min()) if len(drawdowns) > 0 else 0.0
    print(f"  Max drawdown: {max_dd:.0f} NTD")

    # Sharpe estimate (daily)
    daily_pnl = [r.pnl_ntd for r in results]
    if len(daily_pnl) > 1 and np.std(daily_pnl) > 0:
        sharpe_daily = np.mean(daily_pnl) / np.std(daily_pnl)
        sharpe_annual = sharpe_daily * np.sqrt(252)
        print(f"  Daily Sharpe: {sharpe_daily:.3f}")
        print(f"  Annualized Sharpe: {sharpe_annual:.3f}")

    # ── 8. Detrended IC Gate (Pre-registered) ────────────────────────────
    print(f"\n{'='*70}")
    print("DETRENDED IC GATE (Pre-registered thresholds)")
    print(f"{'='*70}")

    # Use 90s horizon as primary
    raw_ics_90 = [r.ic_raw.get("90s", 0) for r in results]
    det_ics_90 = [r.ic_detrended.get("90s", 0) for r in results]
    pooled_raw = np.mean(raw_ics_90)
    pooled_det = np.mean(det_ics_90)

    gate_results: dict[str, tuple[bool, str]] = {}

    # G1: Raw IC > 0.03
    g1_pass = abs(pooled_raw) > RAW_IC_MIN
    gate_results["G1_raw_ic"] = (
        g1_pass,
        f"Pooled raw IC = {pooled_raw:+.4f} (threshold: |IC| > {RAW_IC_MIN})"
    )

    # G2: Detrended IC > 0.02
    g2_pass = abs(pooled_det) > DETRENDED_IC_MIN
    gate_results["G2_detrended_ic"] = (
        g2_pass,
        f"Pooled detrended IC = {pooled_det:+.4f} (threshold: |IC| > {DETRENDED_IC_MIN})"
    )

    # G3: IC ratio > 0.50
    ic_ratio = abs(pooled_det / pooled_raw) if abs(pooled_raw) > 1e-6 else 0.0
    g3_pass = ic_ratio > IC_RATIO_MIN
    gate_results["G3_ic_ratio"] = (
        g3_pass,
        f"IC_detrended / IC_raw = {ic_ratio:.3f} (threshold: > {IC_RATIO_MIN})"
    )

    # G4: Trend contamination < 50%
    trend_contams = [r.trend_contamination for r in results if not np.isnan(r.trend_contamination)]
    mean_contam = np.mean(trend_contams) if trend_contams else 1.0
    g4_pass = mean_contam < TREND_CONTAMINATION_MAX
    gate_results["G4_trend_contamination"] = (
        g4_pass,
        f"Mean trend contamination = {mean_contam:.3f} (threshold: < {TREND_CONTAMINATION_MAX})"
    )

    # G5: Quintile monotonicity (at least one reversal in detrended)
    mono_det_count = sum(1 for r in results if r.quintile_monotonic_detrended)
    g5_pass = mono_det_count < len(results) * 0.5  # less than half of days show monotonic
    gate_results["G5_quintile_monotonicity"] = (
        g5_pass,
        f"Days with monotonic detrended quintiles: {mono_det_count}/{len(results)}"
    )

    # G6: AM/PM consistency > 60%
    am_pm_consistent_count = sum(1 for r in results if r.am_pm_consistent)
    am_pm_ratio = am_pm_consistent_count / len(results)
    g6_pass = am_pm_ratio > 0.60
    gate_results["G6_am_pm_consistency"] = (
        g6_pass,
        f"AM/PM sign consistency: {am_pm_consistent_count}/{len(results)} = {am_pm_ratio:.1%}"
    )

    # G7: Split-half validation (TXFC6 vs TXFD6 IC sign consistency)
    txfc6_ic = np.mean([r.ic_detrended.get("90s", 0) for r in txfc6_results]) if txfc6_results else 0
    txfd6_ic = np.mean([r.ic_detrended.get("90s", 0) for r in txfd6_results]) if txfd6_results else 0
    g7_pass = (np.sign(txfc6_ic) == np.sign(txfd6_ic)) and txfc6_ic != 0 and txfd6_ic != 0
    gate_results["G7_split_half"] = (
        g7_pass,
        f"TXFC6 det IC = {txfc6_ic:+.4f}, TXFD6 det IC = {txfd6_ic:+.4f} (same sign required)"
    )

    all_pass = True
    for gate_name, (passed, desc) in gate_results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {gate_name}: {desc}")
        if not passed:
            all_pass = False

    print(f"\n  OVERALL GATE: {'PASS' if all_pass else 'FAIL'}")

    # ── 9. Final Verdict ─────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("FINAL VERDICT")
    print(f"{'='*70}")

    # Check if APLR beats all baselines
    aplr_ic_mean = np.mean([r.ic_aplr for r in results if not np.isnan(r.ic_aplr)])
    active_ic_mean = np.mean([r.ic_macd_active for r in results if not np.isnan(r.ic_macd_active)])
    passive_ic_mean = np.mean([r.ic_macd_passive for r in results if not np.isnan(r.ic_macd_passive)])
    cvd_ema_ic_mean = np.mean([r.ic_cvd_ema for r in results if not np.isnan(r.ic_cvd_ema)])
    obi_ema_ic_mean = np.mean([r.ic_obi_ema for r in results if not np.isnan(r.ic_obi_ema)])

    beats_active = abs(aplr_ic_mean) > abs(active_ic_mean)
    beats_passive = abs(aplr_ic_mean) > abs(passive_ic_mean)
    beats_cvd_ema = abs(aplr_ic_mean) > abs(cvd_ema_ic_mean)
    beats_obi_ema = abs(aplr_ic_mean) > abs(obi_ema_ic_mean)

    print(f"  APLR IC = {aplr_ic_mean:+.4f}")
    print(f"  Beats macd_active?  {'YES' if beats_active else 'NO'} (active IC = {active_ic_mean:+.4f})")
    print(f"  Beats macd_passive? {'YES' if beats_passive else 'NO'} (passive IC = {passive_ic_mean:+.4f})")
    print(f"  Beats cvd_ema60?    {'YES' if beats_cvd_ema else 'NO'} (cvd_ema IC = {cvd_ema_ic_mean:+.4f})")
    print(f"  Beats obi_ema60?    {'YES' if beats_obi_ema else 'NO'} (obi_ema IC = {obi_ema_ic_mean:+.4f})")
    print(f"  Detrended IC gate:  {'PASS' if all_pass else 'FAIL'}")
    print(f"  P&L (25 days):      {total_pnl:.0f} NTD ({total_trades} trades)")

    if all_pass and beats_active and beats_passive and beats_cvd_ema and beats_obi_ema and total_pnl > 0:
        print(f"\n  >>> VERDICT: PROMISING — proceed to Stage 3 (walk-forward backtest)")
    elif abs(pooled_det) >= 0.015:
        print(f"\n  >>> VERDICT: MARGINAL — detrended IC near threshold, needs more data")
    else:
        print(f"\n  >>> VERDICT: KILL — APLR does not demonstrate sufficient edge")

    # ── Save raw data for reproducibility ────────────────────────────────
    output_data = {
        "n_days": n_days,
        "days": [],
        "pooled_ic_raw_90s": float(pooled_raw),
        "pooled_ic_detrended_90s": float(pooled_det),
        "total_pnl_ntd": float(total_pnl),
        "total_trades": total_trades,
        "gate_results": {k: {"pass": v[0], "detail": v[1]} for k, v in gate_results.items()},
        "all_gates_pass": all_pass,
    }
    for r in results:
        output_data["days"].append({
            "date": r.date,
            "contract": r.contract,
            "n_bars": r.n_bars,
            "n_ticks": r.n_ticks_total,
            "avg_spread_pts": r.avg_spread_pts,
            "green": r.green,
            "red": r.red,
            "grey": r.grey,
            "resonance_pct": r.resonance_pct,
            "ic_raw": r.ic_raw,
            "ic_detrended": r.ic_detrended,
            "ic_macd_active": r.ic_macd_active,
            "ic_macd_passive": r.ic_macd_passive,
            "ic_aplr": r.ic_aplr,
            "ic_cvd_ema": r.ic_cvd_ema,
            "ic_obi_ema": r.ic_obi_ema,
            "ic_resonance_only": r.ic_resonance_only,
            "ic_resonance_detrended": r.ic_resonance_detrended,
            "macd_correlation": r.macd_correlation,
            "trend_contamination": r.trend_contamination,
            "pnl_ntd": r.pnl_ntd,
            "n_trades": r.n_trades,
            "avg_hold_bars": r.avg_hold_bars,
            "avg_episode_len": r.avg_episode_len,
        })

    out_dir = Path("outputs/team_artifacts/alpha-research")
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "R33_stage2_data.json", "w") as f:
        json.dump(output_data, f, indent=2, default=lambda x: float(x) if isinstance(x, (np.floating, np.integer)) else str(x))

    print(f"\nRaw data saved to {out_dir / 'R33_stage2_data.json'}")


if __name__ == "__main__":
    main()
