"""
R23 Stage 2: Signed-Flow Candidate Diagnostic

Replays L1 BBO data through TradeClassifier to compute three candidate signals:
  A1: Confidence-weighted signed OFI (tick-rule correction term)
  A2: Cancel-volume OFI (volume-weighted L1 cancel imbalance)
  C:  Trade-signed toxicity score (EMA of signed imbalance / total volume)

Measures: detrended IC (5 horizons), correlations, R-squared, post-fill adverse movement.

Data: .npy L1 files from research/data/raw/{symbol}/
      Format: (bid_px, ask_px, bid_qty, ask_qty, mid_price, spread_bps, volume, local_ts)
      Volume = 0 throughout (BBO-only). Trades inferred from mid-price changes.

Usage:
  python diagnostic.py [--symbol TXFD6|TMFD6] [--data-dir PATH] [--out-dir PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

# ---------------------------------------------------------------------------
# Project imports (run from repo root or with PYTHONPATH set)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
from hft_platform.trade_classifier import TradeClassifier, BUY, SELL, UNKNOWN

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HORIZONS_S = [10, 30, 60, 120, 300]
TZ_OFFSET_S = 8 * 3600  # UTC+8 for TAIFEX
SESSION_GAP_S = 1800  # 30 min gap = session boundary
EMA_ALPHA_OFI = 2.0 / (8 + 1)  # EMA(8) for OFI, matches FE v2
EMA_ALPHA_TOX = 2.0 / (50 + 1)  # EMA(50) for toxicity (~6s at 8 ticks/s)
DETREND_WINDOW_S = 300  # 5-min local trend removal (R18 gate)


# ---------------------------------------------------------------------------
# Session utilities (reused from measure_adverse_selection.py pattern)
# ---------------------------------------------------------------------------
def find_sessions(ts_ns: NDArray[np.int64]) -> list[tuple[int, int]]:
    """Split data into contiguous sessions (gap > 30 min = break)."""
    ts_s = ts_ns / 1e9
    gaps = np.diff(ts_s)
    breaks = np.where(gaps > SESSION_GAP_S)[0]
    sessions: list[tuple[int, int]] = []
    prev = 0
    for b in breaks:
        sessions.append((prev, b + 1))
        prev = b + 1
    sessions.append((prev, len(ts_ns)))
    return sessions


def filter_trading_hours(ts_ns: NDArray[np.int64]) -> NDArray[np.bool_]:
    """Keep only day session 08:45-13:45 local (UTC+8)."""
    local_h = (((ts_ns / 1e9) + TZ_OFFSET_S) % 86400) / 3600
    return (local_h >= 8.75) & (local_h <= 13.75)


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------
def compute_ofi_l1_raw(
    bid: NDArray, ask: NDArray, bid_qty: NDArray, ask_qty: NDArray,
) -> NDArray[np.float64]:
    """Standard unsigned OFI L1 (same logic as FeatureEngine._compute_ofi_l1_raw)."""
    n = len(bid)
    ofi = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        # Bid flow
        if bid[i] > bid[i - 1]:
            b_flow = bid_qty[i]
        elif bid[i] == bid[i - 1]:
            b_flow = bid_qty[i] - bid_qty[i - 1]
        else:
            b_flow = -bid_qty[i - 1]
        # Ask flow
        if ask[i] > ask[i - 1]:
            a_flow = -ask_qty[i - 1]
        elif ask[i] == ask[i - 1]:
            a_flow = ask_qty[i] - ask_qty[i - 1]
        else:
            a_flow = ask_qty[i]
        ofi[i] = b_flow - a_flow
    return ofi


def compute_ofi_ema(ofi_raw: NDArray[np.float64], alpha: float) -> NDArray[np.float64]:
    """EMA of raw OFI (matches FE v2 ofi_l1_ema8)."""
    n = len(ofi_raw)
    ema = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        ema[i] = (1.0 - alpha) * ema[i - 1] + alpha * ofi_raw[i]
    return ema


def identify_trades(mid: NDArray[np.float64]) -> NDArray[np.int64]:
    """Identify trade-proxy rows: indices where mid_price changed."""
    diff = np.diff(mid)
    return np.where(diff != 0)[0] + 1  # index of NEW state after change


def classify_trades_offline(
    trade_idx: NDArray[np.int64],
    mid: NDArray, bid: NDArray, ask: NDArray,
) -> tuple[NDArray[np.int8], NDArray[np.int16]]:
    """Classify each inferred trade using TradeClassifier (EMO algorithm).

    For inferred trades from BBO data, the 'trade price' is estimated as:
    - If mid went UP: buyer lifted the ask -> trade price = ask of previous row
    - If mid went DOWN: seller hit the bid -> trade price = bid of previous row
    """
    tc = TradeClassifier(enabled=True)
    directions = np.zeros(len(trade_idx), dtype=np.int8)
    confidences = np.zeros(len(trade_idx), dtype=np.int16)

    for j, ti in enumerate(trade_idx):
        # Update BBO state from the row BEFORE the trade
        prev = ti - 1
        b = int(bid[prev])
        a = int(ask[prev])
        if b > 0 and a > 0:
            tc.update_quotes("SYM", b, a)

        # Estimate trade price from direction of mid change
        mid_prev = mid[prev]
        mid_cur = mid[ti]
        if mid_cur > mid_prev:
            trade_price = int(ask[prev])  # buyer lifted the ask
        else:
            trade_price = int(bid[prev])  # seller hit the bid

        d, c = tc.classify("SYM", trade_price)
        directions[j] = d
        confidences[j] = c

    return directions, confidences


def compute_cancel_volume_ofi(
    bid_qty: NDArray, ask_qty: NDArray,
    bid: NDArray, ask: NDArray,
    trade_mask: NDArray[np.bool_],
) -> NDArray[np.float64]:
    """A2: Cancel-volume OFI.

    For each L1 depth decrease that does NOT coincide with a trade (mid unchanged),
    compute directional cancel volume: bid_cancel - ask_cancel.
    Non-cancel rows = 0.
    """
    n = len(bid_qty)
    cancel_ofi = np.zeros(n, dtype=np.float64)

    for i in range(1, n):
        if trade_mask[i]:
            continue  # skip trade rows — depth change is fill, not cancel

        # Bid-side cancel: depth decreased while price unchanged
        b_cancel = 0.0
        if bid[i] == bid[i - 1] and bid_qty[i] < bid_qty[i - 1]:
            b_cancel = bid_qty[i - 1] - bid_qty[i]  # positive = volume cancelled

        # Ask-side cancel: depth decreased while price unchanged
        a_cancel = 0.0
        if ask[i] == ask[i - 1] and ask_qty[i] < ask_qty[i - 1]:
            a_cancel = ask_qty[i - 1] - ask_qty[i]

        # Cancel OFI: more ask cancels = bullish (liquidity withdrawal on sell side)
        cancel_ofi[i] = a_cancel - b_cancel

    return cancel_ofi


def compute_confidence_weighted_ofi(
    ofi_raw: NDArray[np.float64],
    trade_idx: NDArray[np.int64],
    directions: NDArray[np.int8],
    confidences: NDArray[np.int16],
) -> NDArray[np.float64]:
    """A1: Confidence-weighted signed OFI.

    At trade rows: OFI * (confidence / 1000).
    At non-trade rows: OFI unchanged (weight = 1.0).
    The correction term is (1 - conf/1000) * ofi at trade rows.
    """
    cw_ofi = ofi_raw.copy()
    trade_set = set(trade_idx.tolist())
    idx_to_j = {int(ti): j for j, ti in enumerate(trade_idx)}

    for ti_int in trade_set:
        j = idx_to_j[ti_int]
        conf = confidences[j]
        weight = conf / 1000.0 if conf > 0 else 0.5
        cw_ofi[ti_int] = ofi_raw[ti_int] * weight

    return cw_ofi


def compute_toxicity_score(
    trade_idx: NDArray[np.int64],
    directions: NDArray[np.int8],
    n_rows: int,
    alpha: float,
) -> NDArray[np.float64]:
    """C: Toxicity score — EMA of signed trade direction.

    At each trade row, update EMA with direction (+1/-1).
    Non-trade rows carry forward.
    Score in [-1000, +1000] (scaled x1000 for int compatibility).
    """
    tox = np.zeros(n_rows, dtype=np.float64)
    ema_val = 0.0
    trade_set = set(trade_idx.tolist())
    idx_to_j = {int(ti): j for j, ti in enumerate(trade_idx)}

    for i in range(n_rows):
        if i in trade_set:
            j = idx_to_j[i]
            d = int(directions[j])
            if d != UNKNOWN:
                ema_val = (1.0 - alpha) * ema_val + alpha * float(d)
        tox[i] = ema_val * 1000.0  # scale x1000

    return tox


# ---------------------------------------------------------------------------
# Measurement: forward returns, detrended IC, R², correlations
# ---------------------------------------------------------------------------
def compute_forward_returns(
    mid: NDArray, ts_ns: NDArray, sessions: list[tuple[int, int]],
    horizon_s: float,
) -> NDArray[np.float64]:
    """For each row, compute mid-price return at +horizon_s. NaN if crosses session."""
    fwd = np.full(len(mid), np.nan, dtype=np.float64)
    horizon_ns = int(horizon_s * 1e9)

    for ss, se in sessions:
        seg_ts = ts_ns[ss:se]
        seg_mid = mid[ss:se]
        if len(seg_ts) < 2:
            continue
        target_ts = seg_ts + horizon_ns
        future_pos = np.searchsorted(seg_ts, target_ts)
        valid = future_pos < len(seg_ts)
        for local_i in np.where(valid)[0]:
            fi = future_pos[local_i]
            if fi < len(seg_mid) and seg_mid[local_i] != 0:
                fwd[ss + local_i] = (seg_mid[fi] - seg_mid[local_i]) / seg_mid[local_i]

    return fwd


def detrend_signal(signal: NDArray, ts_ns: NDArray, window_s: float) -> NDArray[np.float64]:
    """Remove 5-min local trend from signal (R18 mandatory gate).

    Uses expanding mean within each window_s block.
    """
    detrended = signal.copy().astype(np.float64)
    window_ns = int(window_s * 1e9)

    # Block-based detrending: subtract mean of each window_s block
    block_start_ts = ts_ns[0]
    block_start_idx = 0
    for i in range(len(ts_ns)):
        if ts_ns[i] - block_start_ts >= window_ns:
            block_mean = np.nanmean(detrended[block_start_idx:i])
            detrended[block_start_idx:i] -= block_mean
            block_start_ts = ts_ns[i]
            block_start_idx = i
    # Last block
    if block_start_idx < len(detrended):
        block_mean = np.nanmean(detrended[block_start_idx:])
        detrended[block_start_idx:] -= block_mean

    return detrended


def rank_ic(signal: NDArray, returns: NDArray) -> float:
    """Spearman rank IC between signal and returns, ignoring NaN."""
    valid = ~np.isnan(signal) & ~np.isnan(returns) & (signal != 0)
    if valid.sum() < 30:
        return np.nan
    from scipy.stats import spearmanr
    rho, _ = spearmanr(signal[valid], returns[valid])
    return float(rho)


def r_squared(signal: NDArray, returns: NDArray) -> float:
    """Linear R² between signal and returns, ignoring NaN."""
    valid = ~np.isnan(signal) & ~np.isnan(returns) & (signal != 0)
    if valid.sum() < 30:
        return np.nan
    ss_res = np.sum((returns[valid] - np.polyval(np.polyfit(signal[valid], returns[valid], 1), signal[valid])) ** 2)
    ss_tot = np.sum((returns[valid] - np.mean(returns[valid])) ** 2)
    if ss_tot == 0:
        return np.nan
    return float(1.0 - ss_res / ss_tot)


def pearson_corr(a: NDArray, b: NDArray) -> float:
    """Pearson correlation, ignoring NaN/zero."""
    valid = ~np.isnan(a) & ~np.isnan(b) & (a != 0) & (b != 0)
    if valid.sum() < 30:
        return np.nan
    return float(np.corrcoef(a[valid], b[valid])[0, 1])


# ---------------------------------------------------------------------------
# Post-fill adverse movement (Candidate C diagnostic)
# ---------------------------------------------------------------------------
def compute_adverse_movement_by_toxicity(
    trade_idx: NDArray[np.int64],
    directions: NDArray[np.int8],
    toxicity: NDArray[np.float64],
    mid: NDArray, ts_ns: NDArray,
    sessions: list[tuple[int, int]],
) -> dict:
    """Bucket trades by toxicity quintile, measure adverse movement at +5/10/30/60s."""
    horizons = [5, 10, 30, 60]
    tox_at_trade = toxicity[trade_idx]

    # Only trades with known direction
    known = directions != UNKNOWN
    if known.sum() < 50:
        return {"error": "insufficient known-direction trades"}

    t_idx = trade_idx[known]
    t_dir = directions[known]
    t_tox = tox_at_trade[known]

    # Quintiles
    q_edges = np.percentile(t_tox, [20, 40, 60, 80])
    q_labels = np.digitize(t_tox, q_edges)  # 0-4

    results: dict = {}
    for h_s in horizons:
        fwd_mid = np.full(len(t_idx), np.nan)
        h_ns = int(h_s * 1e9)
        for ss, se in sessions:
            mask = (t_idx >= ss) & (t_idx < se)
            if not mask.any():
                continue
            seg_ts = ts_ns[ss:se]
            seg_mid = mid[ss:se]
            for j in np.where(mask)[0]:
                ti = t_idx[j]
                target = ts_ns[ti] + h_ns
                fi = np.searchsorted(seg_ts, target)
                if fi < len(seg_mid):
                    fwd_mid[j] = seg_mid[fi]  # fi is already relative to segment

        # Adverse movement: for buys, price going down is adverse. For sells, up.
        current_mid = mid[t_idx]
        delta = fwd_mid - current_mid
        adverse = np.where(t_dir == BUY, -delta, delta)  # positive = adverse

        quintile_medians = []
        for q in range(5):
            qmask = (q_labels == q) & ~np.isnan(adverse)
            if qmask.sum() > 0:
                quintile_medians.append(float(np.median(adverse[qmask])))
            else:
                quintile_medians.append(np.nan)

        results[f"+{h_s}s"] = {
            "quintile_medians": quintile_medians,
            "q5_minus_q1": quintile_medians[4] - quintile_medians[0]
            if not (np.isnan(quintile_medians[4]) or np.isnan(quintile_medians[0]))
            else np.nan,
            "n_valid": int((~np.isnan(adverse)).sum()),
        }

    return results


# ---------------------------------------------------------------------------
# Cancel/fill contamination check (A2 diagnostic)
# ---------------------------------------------------------------------------
def check_cancel_fill_contamination(
    bid: NDArray, ask: NDArray,
    bid_qty: NDArray, ask_qty: NDArray,
    trade_mask: NDArray[np.bool_],
) -> dict:
    """Report fraction of L1 depth decreases that coincide with a trade."""
    n = len(bid)
    depth_decrease_count = 0
    coincides_with_trade = 0

    for i in range(1, n):
        bid_decreased = (bid[i] == bid[i - 1]) and (bid_qty[i] < bid_qty[i - 1])
        ask_decreased = (ask[i] == ask[i - 1]) and (ask_qty[i] < ask_qty[i - 1])

        if bid_decreased or ask_decreased:
            depth_decrease_count += 1
            if trade_mask[i]:
                coincides_with_trade += 1

    frac = coincides_with_trade / depth_decrease_count if depth_decrease_count > 0 else 0.0
    return {
        "depth_decrease_count": depth_decrease_count,
        "coincides_with_trade": coincides_with_trade,
        "fill_fraction": frac,
        "flagged": frac > 0.50,
    }


# ---------------------------------------------------------------------------
# Main diagnostic pipeline
# ---------------------------------------------------------------------------
def load_all_days(data_dir: Path, symbol: str) -> tuple[NDArray, ...]:
    """Load and concatenate all per-day .npy files for a symbol."""
    files = sorted(data_dir.glob(f"{symbol}_*_l1.npy"))
    if not files:
        raise FileNotFoundError(f"No .npy files for {symbol} in {data_dir}")

    arrays = []
    for f in files:
        d = np.load(str(f))
        arrays.append(d)
        print(f"  Loaded {f.name}: {len(d):,} rows")

    combined = np.concatenate(arrays)
    print(f"  Total: {len(combined):,} rows across {len(files)} files")
    return (
        combined["bid_px"].astype(np.float64),
        combined["ask_px"].astype(np.float64),
        combined["bid_qty"].astype(np.float64),
        combined["ask_qty"].astype(np.float64),
        combined["mid_price"].astype(np.float64),
        combined["local_ts"].astype(np.int64),
    )


def run_diagnostic(symbol: str, data_dir: Path, out_dir: Path) -> dict:
    """Run full diagnostic for one symbol."""
    print(f"\n{'=' * 70}")
    print(f"R23 STAGE 2 DIAGNOSTIC — {symbol}")
    print(f"{'=' * 70}")

    # --- Load data ---
    print(f"\nLoading {symbol} data from {data_dir}...")
    bid, ask, bid_qty, ask_qty, mid, ts_ns = load_all_days(data_dir, symbol)

    # --- Filter trading hours ---
    day_mask = filter_trading_hours(ts_ns)
    print(f"\nDay session rows: {day_mask.sum():,} / {len(bid):,}")

    bid = bid[day_mask]
    ask = ask[day_mask]
    bid_qty = bid_qty[day_mask]
    ask_qty = ask_qty[day_mask]
    mid = mid[day_mask]
    ts_ns = ts_ns[day_mask]
    n = len(bid)

    # --- Sessions ---
    sessions = find_sessions(ts_ns)
    print(f"Sessions: {len(sessions)}")

    # --- Spread statistics ---
    spread = ask - bid
    print(f"\nSpread: min={spread.min():.0f} med={np.median(spread):.0f} "
          f"p95={np.percentile(spread, 95):.0f} max={spread.max():.0f}")

    # --- Identify trades (mid-price changes) ---
    trade_idx = identify_trades(mid)
    n_trades = len(trade_idx)
    print(f"\nInferred trades (mid changes): {n_trades:,}")
    if n_trades < 100:
        print("  WARNING: Very few trades. Results may be unreliable.")

    # Trade mask (for cancel/fill separation)
    trade_mask = np.zeros(n, dtype=bool)
    trade_mask[trade_idx] = True

    # --- Classify trades ---
    print("Classifying trades via TradeClassifier (EMO)...")
    directions, confidences = classify_trades_offline(trade_idx, mid, bid, ask)

    n_buy = (directions == BUY).sum()
    n_sell = (directions == SELL).sum()
    n_unknown = (directions == UNKNOWN).sum()
    n_at_quote = (confidences == 1000).sum()
    n_inside = (confidences == 800).sum()
    n_tick_rule = (confidences == 500).sum()

    print(f"  BUY: {n_buy:,}  SELL: {n_sell:,}  UNKNOWN: {n_unknown:,}")
    print(f"  At-quote: {n_at_quote:,}  Inside: {n_inside:,}  Tick-rule: {n_tick_rule:,}")

    tick_rule_frac = n_tick_rule / max(1, n_trades)
    print(f"  Tick-rule fallback rate: {tick_rule_frac * 100:.1f}%")

    # --- Compute base signals ---
    print("\nComputing base OFI signals...")
    ofi_raw = compute_ofi_l1_raw(bid, ask, bid_qty, ask_qty)
    ofi_ema = compute_ofi_ema(ofi_raw, EMA_ALPHA_OFI)

    print("Computing A1: Confidence-weighted OFI...")
    cw_ofi_raw = compute_confidence_weighted_ofi(ofi_raw, trade_idx, directions, confidences)
    cw_ofi_ema = compute_ofi_ema(cw_ofi_raw, EMA_ALPHA_OFI)

    print("Computing A2: Cancel-volume OFI...")
    cancel_ofi_raw = compute_cancel_volume_ofi(bid_qty, ask_qty, bid, ask, trade_mask)
    cancel_ofi_ema = compute_ofi_ema(cancel_ofi_raw, EMA_ALPHA_OFI)

    print("Computing C: Toxicity score...")
    toxicity = compute_toxicity_score(trade_idx, directions, n, EMA_ALPHA_TOX)

    # --- Detrend all signals ---
    print("Detrending signals (5-min window)...")
    ofi_ema_dt = detrend_signal(ofi_ema, ts_ns, DETREND_WINDOW_S)
    cw_ofi_ema_dt = detrend_signal(cw_ofi_ema, ts_ns, DETREND_WINDOW_S)
    cancel_ofi_ema_dt = detrend_signal(cancel_ofi_ema, ts_ns, DETREND_WINDOW_S)
    toxicity_dt = detrend_signal(toxicity, ts_ns, DETREND_WINDOW_S)

    # --- Forward returns ---
    print("Computing forward returns at 5 horizons...")
    fwd_returns: dict[int, NDArray] = {}
    for h in HORIZONS_S:
        fwd_returns[h] = compute_forward_returns(mid, ts_ns, sessions, h)
        valid_n = (~np.isnan(fwd_returns[h])).sum()
        print(f"  +{h}s: {valid_n:,} valid")

    # --- IC measurements ---
    print("\n" + "-" * 70)
    print("DETRENDED IC (Spearman rank correlation with forward returns)")
    print("-" * 70)

    signals = {
        "ofi_ema8 (baseline)": ofi_ema_dt,
        "A1: conf_weighted_ofi_ema8": cw_ofi_ema_dt,
        "A2: cancel_volume_ofi_ema8": cancel_ofi_ema_dt,
        "C: toxicity_score": toxicity_dt,
    }

    ic_table: dict[str, dict[int, float]] = {}
    for name, sig in signals.items():
        ic_table[name] = {}
        row = f"  {name:<35}"
        for h in HORIZONS_S:
            ic = rank_ic(sig, fwd_returns[h])
            ic_table[name][h] = ic
            row += f"  {ic:+.4f}" if not np.isnan(ic) else "     NaN"
        print(row)

    # Header
    header = f"  {'Signal':<35}"
    for h in HORIZONS_S:
        header += f"  {'+' + str(h) + 's':>7}"
    print(header)
    for name in signals:
        row = f"  {name:<35}"
        for h in HORIZONS_S:
            ic = ic_table[name][h]
            row += f"  {ic:+.4f}" if not np.isnan(ic) else "     NaN"
        print(row)

    # --- Monotonic IC decay check ---
    print("\n  Monotonic IC check (trend contamination flag):")
    for name in signals:
        ics = [ic_table[name][h] for h in HORIZONS_S if not np.isnan(ic_table[name][h])]
        if len(ics) >= 3:
            increasing = all(ics[i] <= ics[i + 1] for i in range(len(ics) - 1))
            if increasing:
                print(f"    {name}: FLAGGED — IC increases monotonically (trend contamination)")
            else:
                print(f"    {name}: OK — non-monotonic")
        else:
            print(f"    {name}: SKIP — insufficient valid horizons")

    # --- R² measurements ---
    print("\n" + "-" * 70)
    print("R-SQUARED (linear fit)")
    print("-" * 70)

    r2_table: dict[str, dict[int, float]] = {}
    for name, sig in signals.items():
        r2_table[name] = {}
        row = f"  {name:<35}"
        for h in HORIZONS_S:
            r2 = r_squared(sig, fwd_returns[h])
            r2_table[name][h] = r2
            row += f"  {r2:.6f}" if not np.isnan(r2) else "      NaN"
        print(row)

    # --- Correlation kill gates ---
    print("\n" + "-" * 70)
    print("CORRELATION KILL GATES")
    print("-" * 70)

    corr_a1_ofi = pearson_corr(cw_ofi_ema, ofi_ema)
    corr_a2_ofi = pearson_corr(cancel_ofi_ema, ofi_ema)
    corr_c_spread = pearson_corr(toxicity, spread)

    print(f"  A1 corr(conf_weighted_ofi, ofi_ema8):  {corr_a1_ofi:+.4f}  "
          f"{'KILL (>0.85)' if corr_a1_ofi > 0.85 else 'PASS'}")
    print(f"  A2 corr(cancel_volume_ofi, ofi_ema8):  {corr_a2_ofi:+.4f}  "
          f"{'KILL (>0.60)' if corr_a2_ofi > 0.60 else 'PASS'}")
    print(f"  C  corr(toxicity_score, spread):        {corr_c_spread:+.4f}  "
          f"{'KILL (>0.70)' if corr_c_spread > 0.70 else 'PASS'}")

    # --- Cancel/fill contamination (A2) ---
    print("\n" + "-" * 70)
    print("A2 CANCEL/FILL CONTAMINATION CHECK")
    print("-" * 70)

    contam = check_cancel_fill_contamination(bid, ask, bid_qty, ask_qty, trade_mask)
    print(f"  L1 depth decreases: {contam['depth_decrease_count']:,}")
    print(f"  Coincide with trade: {contam['coincides_with_trade']:,}")
    print(f"  Fill fraction: {contam['fill_fraction']:.3f}")
    print(f"  Flagged (>50%): {contam['flagged']}")

    # --- Post-fill adverse movement (Candidate C) ---
    print("\n" + "-" * 70)
    print("CANDIDATE C: POST-FILL ADVERSE MOVEMENT BY TOXICITY QUINTILE")
    print("-" * 70)

    adverse_results = compute_adverse_movement_by_toxicity(
        trade_idx, directions, toxicity, mid, ts_ns, sessions,
    )
    if "error" in adverse_results:
        print(f"  {adverse_results['error']}")
    else:
        print(f"  {'Horizon':<10} {'Q1 (low tox)':>12} {'Q2':>8} {'Q3':>8} {'Q4':>8} "
              f"{'Q5 (high tox)':>14} {'Q5-Q1':>8} {'Status':>10}")
        all_pass = True
        for h_key, h_data in adverse_results.items():
            qm = h_data["quintile_medians"]
            diff = h_data["q5_minus_q1"]
            status = "PASS" if (not np.isnan(diff) and abs(diff) >= 1.0) else "FAIL"
            if status == "FAIL":
                all_pass = False
            row = f"  {h_key:<10}"
            for v in qm:
                row += f"  {v:+8.2f}" if not np.isnan(v) else "       NaN"
            row += f"  {diff:+8.2f}" if not np.isnan(diff) else "       NaN"
            row += f"  {status:>10}"
            print(row)
        print(f"\n  Overall: {'PASS — Q5-Q1 >= 1 tick at some horizon' if all_pass else 'CHECK — see individual horizons'}")

    # --- Kill gate summary ---
    print("\n" + "=" * 70)
    print("KILL GATE SUMMARY")
    print("=" * 70)

    # A1 gates
    a1_ic_pass = any(
        not np.isnan(ic_table["A1: conf_weighted_ofi_ema8"][h])
        and ic_table["A1: conf_weighted_ofi_ema8"][h] >= 0.015
        for h in HORIZONS_S
    )
    a1_corr_pass = not np.isnan(corr_a1_ofi) and corr_a1_ofi <= 0.85
    a1_verdict = "PASS" if (a1_ic_pass and a1_corr_pass) else "KILL"
    print(f"  A1 (conf-weighted OFI): IC >= 0.015? {'Y' if a1_ic_pass else 'N'}  "
          f"corr <= 0.85? {'Y' if a1_corr_pass else 'N'}  -> {a1_verdict}")

    # A2 gates
    a2_ic_pass = any(
        not np.isnan(ic_table["A2: cancel_volume_ofi_ema8"][h])
        and ic_table["A2: cancel_volume_ofi_ema8"][h] >= 0.015
        for h in HORIZONS_S
    )
    a2_corr_pass = not np.isnan(corr_a2_ofi) and corr_a2_ofi <= 0.60
    a2_contam_pass = not contam["flagged"]
    a2_verdict = "PASS" if (a2_ic_pass and a2_corr_pass and a2_contam_pass) else "KILL"
    print(f"  A2 (cancel-volume OFI): IC >= 0.015? {'Y' if a2_ic_pass else 'N'}  "
          f"corr <= 0.60? {'Y' if a2_corr_pass else 'N'}  "
          f"contam <= 50%? {'Y' if a2_contam_pass else 'N'}  -> {a2_verdict}")

    # C gates
    c_ic_pass = any(
        not np.isnan(ic_table["C: toxicity_score"][h])
        and ic_table["C: toxicity_score"][h] >= 0.015
        for h in HORIZONS_S
    )
    c_corr_pass = not np.isnan(corr_c_spread) and corr_c_spread <= 0.70
    c_adverse_pass = False
    if "error" not in adverse_results:
        c_adverse_pass = any(
            not np.isnan(h_data["q5_minus_q1"]) and abs(h_data["q5_minus_q1"]) >= 1.0
            for h_data in adverse_results.values()
        )
    c_verdict = "PASS" if (c_ic_pass or c_adverse_pass) and c_corr_pass else "KILL"
    print(f"  C  (toxicity score):    IC >= 0.015? {'Y' if c_ic_pass else 'N'}  "
          f"corr <= 0.70? {'Y' if c_corr_pass else 'N'}  "
          f"adverse Q5-Q1 >= 1? {'Y' if c_adverse_pass else 'N'}  -> {c_verdict}")

    # --- Save results ---
    result = {
        "symbol": symbol,
        "n_rows": int(n),
        "n_trades": int(n_trades),
        "tick_rule_fallback_rate": float(tick_rule_frac),
        "classification": {
            "buy": int(n_buy), "sell": int(n_sell), "unknown": int(n_unknown),
            "at_quote": int(n_at_quote), "inside": int(n_inside), "tick_rule": int(n_tick_rule),
        },
        "spread": {
            "min": float(spread.min()), "median": float(np.median(spread)),
            "p95": float(np.percentile(spread, 95)),
        },
        "ic_table": {name: {str(h): float(v) if not np.isnan(v) else None for h, v in ics.items()}
                     for name, ics in ic_table.items()},
        "r2_table": {name: {str(h): float(v) if not np.isnan(v) else None for h, v in r2s.items()}
                     for name, r2s in r2_table.items()},
        "correlations": {
            "a1_vs_ofi": float(corr_a1_ofi) if not np.isnan(corr_a1_ofi) else None,
            "a2_vs_ofi": float(corr_a2_ofi) if not np.isnan(corr_a2_ofi) else None,
            "c_vs_spread": float(corr_c_spread) if not np.isnan(corr_c_spread) else None,
        },
        "cancel_fill_contamination": contam,
        "adverse_movement": adverse_results,
        "verdicts": {"A1": a1_verdict, "A2": a2_verdict, "C": c_verdict},
    }

    out_file = out_dir / f"results_{symbol}.json"
    with open(out_file, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\nResults saved to {out_file}")

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="R23 Stage 2 Signed-Flow Diagnostic")
    parser.add_argument("--symbol", default="TXFD6", help="Primary symbol (default: TXFD6)")
    parser.add_argument("--data-dir", default=None, help="Data directory override")
    parser.add_argument("--out-dir", default=None, help="Output directory override")
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[4]
    data_dir = Path(args.data_dir) if args.data_dir else base / "research" / "data" / "raw" / args.symbol.lower()
    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not data_dir.exists():
        print(f"ERROR: Data directory not found: {data_dir}")
        print("Available symbols:")
        raw_dir = base / "research" / "data" / "raw"
        if raw_dir.exists():
            for d in sorted(raw_dir.iterdir()):
                if d.is_dir():
                    n_files = len(list(d.glob("*_l1.npy")))
                    print(f"  {d.name}: {n_files} files")
        sys.exit(1)

    run_diagnostic(args.symbol, data_dir, out_dir)


if __name__ == "__main__":
    main()
