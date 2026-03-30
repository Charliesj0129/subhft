"""
Phase 2A Empirical Validation: Toxic Flow Detection (Candidate C1)
==================================================================

Validates whether OFI-based toxicity proxies can identify adverse selection
windows in TXFD6 Mini-TAIEX futures data.

Validation checks:
  V1: Toxicity proxy AUC (target > 0.60)
  V2: OFI-threshold detector precision/recall
  V3: Post-fill adverse selection at 1s/5s/10s
  V4: Economic impact calculation
  V5: Feature engine version confirmation

Data: L1 BBO (13 days, ~6.3M rows) + L2 with trades (4 days)
"""

import json
import os
import sys
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_DIR = Path("research/data/raw/txfd6")
OUT_DIR = Path("outputs/team_artifacts/alpha-research")

# TXFD6 1 point = 10 NTD (Mini-TAIEX)
POINT_VALUE_NTD = 10
# RT cost in bps
RT_COST_BPS = 2.18
# OpportunisticMM activation fraction
MM_ACTIVATION_RATE = 0.021
# Wide spread threshold (bps) at which OpportunisticMM activates
WIDE_SPREAD_BPS = 2.5
# Price scale in data
PRICE_SCALE = 1_000_000.0

# OFI window size in rows (not time — rows are ~115ms median apart)
OFI_WINDOW = 20  # ~2.3s worth of ticks
# Forward return horizon in rows
FWD_HORIZONS_ROWS = {
    "1s": 9,     # ~1s at 115ms/tick
    "5s": 43,    # ~5s
    "10s": 87,   # ~10s
}
# Adverse move threshold: 1 tick = 1 index point
# In data units (price already scaled), 1 point = 1.0
ADVERSE_TICK = 1.0

# Trade event code from L2 data
TRADE_EV = 0xC0000002
BID_L1_EV = 0xE0000001
ASK_L1_EV = 0xD0000001


def load_l1_all():
    """Load concatenated L1 BBO data."""
    path = DATA_DIR / "TXFD6_all_l1.npy"
    if not path.exists():
        print(f"ERROR: {path} not found")
        sys.exit(1)
    return np.load(str(path), mmap_mode="r")


def load_l2_days():
    """Load all available L2 data (with trades)."""
    l2_files = sorted(DATA_DIR.glob("TXFD6_*_l2.hftbt.npz"))
    datasets = []
    for f in l2_files:
        d = np.load(str(f))["data"]
        datasets.append(d)
        date = f.name.split("_")[1]
        n_trades = np.sum(d["ev"] == TRADE_EV)
        print(f"  L2 {date}: {len(d):,} events, {n_trades:,} trades")
    if not datasets:
        print("WARNING: No L2 data found")
        return None
    return np.concatenate(datasets)


def compute_ofi_series(data):
    """Compute OFI (Order Flow Imbalance) from L1 BBO changes.

    OFI = delta(bid_qty * I(bid_px unchanged or up))
        - delta(ask_qty * I(ask_px unchanged or down))

    Returns array of same length as data, with OFI values.
    """
    n = len(data)
    ofi = np.zeros(n, dtype=np.float64)

    bid_px = data["bid_px"]
    ask_px = data["ask_px"]
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]

    # Vectorized OFI computation
    # Bid side contribution
    bid_px_up = bid_px[1:] > bid_px[:-1]
    bid_px_same = bid_px[1:] == bid_px[:-1]
    bid_px_down = bid_px[1:] < bid_px[:-1]

    bid_contrib = np.where(
        bid_px_up, bid_qty[1:],
        np.where(bid_px_same, bid_qty[1:] - bid_qty[:-1],
                 -bid_qty[:-1])
    )

    # Ask side contribution
    ask_px_down = ask_px[1:] < ask_px[:-1]
    ask_px_same = ask_px[1:] == ask_px[:-1]
    ask_px_up = ask_px[1:] > ask_px[:-1]

    ask_contrib = np.where(
        ask_px_down, -ask_qty[1:],
        np.where(ask_px_same, -(ask_qty[1:] - ask_qty[:-1]),
                 ask_qty[:-1])
    )

    ofi[1:] = bid_contrib + ask_contrib
    return ofi


def compute_rolling_ofi(ofi, window):
    """Compute rolling sum of OFI over window."""
    cs = np.cumsum(ofi)
    rolling = np.zeros_like(ofi)
    rolling[window:] = cs[window:] - cs[:-window]
    rolling[:window] = cs[:window]
    return rolling


def compute_forward_returns(data, horizon_rows):
    """Compute forward mid-price returns (in index points) at given horizon."""
    mid = data["mid_price"]
    n = len(mid)
    fwd = np.full(n, np.nan)
    if horizon_rows < n:
        fwd[:n - horizon_rows] = mid[horizon_rows:] - mid[:n - horizon_rows]
    return fwd


def run_v1_auc(data):
    """V1: Toxicity Proxy AUC.

    Define toxic window: rolling OFI magnitude in top quintile.
    Label: |forward return| > 1 tick at ~1s horizon.
    Compute AUC of |rolling_ofi| predicting adverse moves.
    """
    print("\n=== V1: Toxicity Proxy AUC ===")

    ofi = compute_ofi_series(data)
    rolling_ofi = compute_rolling_ofi(ofi, OFI_WINDOW)
    fwd_1s = compute_forward_returns(data, FWD_HORIZONS_ROWS["1s"])

    # Only use rows where forward return is available
    valid = ~np.isnan(fwd_1s)
    abs_rofi = np.abs(rolling_ofi[valid])
    abs_fwd = np.abs(fwd_1s[valid])

    # Binary label: adverse move > 1 tick
    label = (abs_fwd > ADVERSE_TICK).astype(np.int32)
    pos_rate = label.mean()
    print(f"  Adverse move rate (|ret| > 1 tick at 1s): {pos_rate:.4f} ({pos_rate*100:.2f}%)")

    # Compute AUC using trapezoidal rule (no sklearn dependency)
    auc = _compute_auc(abs_rofi, label)
    print(f"  AUC (|rolling_OFI| -> adverse move): {auc:.4f}")
    print(f"  PASS threshold: AUC > 0.60")
    print(f"  Result: {'PASS' if auc > 0.60 else 'FAIL'}")

    # Also try signed OFI predicting signed return direction
    signed_label = (fwd_1s[valid] > ADVERSE_TICK).astype(np.int32)
    auc_signed = _compute_auc(rolling_ofi[valid], signed_label)
    print(f"  AUC (signed rolling_OFI -> price UP >1 tick): {auc_signed:.4f}")

    # Try multiple thresholds for OFI magnitude
    thresholds = [50, 60, 70, 80, 90, 95]
    print("\n  Threshold sweep (OFI percentile -> adverse move prediction):")
    for pct in thresholds:
        thr = np.percentile(abs_rofi, pct)
        pred = abs_rofi > thr
        if pred.sum() == 0:
            continue
        precision = label[pred].mean()
        recall = label[pred].sum() / max(label.sum(), 1)
        print(f"    P{pct}: threshold={thr:.1f}, flagged={pred.mean()*100:.1f}%, "
              f"precision={precision*100:.1f}%, recall={recall*100:.1f}%")

    return {
        "auc_unsigned": float(auc),
        "auc_signed": float(auc_signed),
        "adverse_move_rate": float(pos_rate),
        "n_samples": int(valid.sum()),
        "pass": auc > 0.60,
    }


def run_v2_threshold_detector(data):
    """V2: Simple OFI-threshold toxic window detector.

    Flag windows where |rolling_OFI| > threshold as toxic.
    Measure precision and recall against adverse price moves.
    """
    print("\n=== V2: OFI-Threshold Toxic Detector ===")

    ofi = compute_ofi_series(data)
    rolling_ofi = compute_rolling_ofi(ofi, OFI_WINDOW)
    fwd_1s = compute_forward_returns(data, FWD_HORIZONS_ROWS["1s"])

    valid = ~np.isnan(fwd_1s)
    abs_rofi = np.abs(rolling_ofi[valid])
    abs_fwd = np.abs(fwd_1s[valid])
    label = abs_fwd > ADVERSE_TICK

    # Also condition on wide spread (OpportunisticMM territory)
    spread = data["spread_bps"][valid]
    wide_mask = spread > WIDE_SPREAD_BPS

    results = {}
    for regime, mask_label in [("all_spreads", np.ones(valid.sum(), dtype=bool)),
                                ("wide_spread_only", wide_mask)]:
        print(f"\n  --- Regime: {regime} ---")
        if mask_label.sum() == 0:
            print(f"  No rows in this regime")
            continue

        m_rofi = abs_rofi[mask_label]
        m_label = label[mask_label]
        base_rate = m_label.mean()
        print(f"  Rows: {mask_label.sum():,}, base adverse rate: {base_rate*100:.2f}%")

        # Sweep OFI thresholds
        best = {"precision": 0, "recall": 0, "threshold": 0, "f1": 0}
        sweep = []
        for pct in range(50, 100, 5):
            thr = np.percentile(m_rofi, pct)
            pred = m_rofi > thr
            if pred.sum() == 0:
                continue
            tp = (pred & m_label).sum()
            precision = tp / pred.sum() if pred.sum() > 0 else 0
            recall = tp / m_label.sum() if m_label.sum() > 0 else 0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
            sweep.append({
                "percentile": pct,
                "threshold": float(thr),
                "flagged_pct": float(pred.mean() * 100),
                "precision": float(precision * 100),
                "recall": float(recall * 100),
                "f1": float(f1 * 100),
            })
            if f1 > best["f1"]:
                best = {"precision": precision, "recall": recall,
                        "threshold": thr, "f1": f1, "percentile": pct}

        for s in sweep:
            print(f"    P{s['percentile']}: thr={s['threshold']:.1f}, "
                  f"flagged={s['flagged_pct']:.1f}%, "
                  f"prec={s['precision']:.1f}%, rec={s['recall']:.1f}%, "
                  f"F1={s['f1']:.1f}%")

        print(f"\n  Best F1: P{best.get('percentile','?')}, "
              f"precision={best['precision']*100:.1f}%, recall={best['recall']*100:.1f}%")
        print(f"  PASS threshold: Precision > 55%, Recall > 30%")
        passed = best["precision"] > 0.55 and best["recall"] > 0.30
        print(f"  Result: {'PASS' if passed else 'FAIL'}")

        results[regime] = {
            "best_precision": float(best["precision"]),
            "best_recall": float(best["recall"]),
            "best_f1": float(best["f1"]),
            "best_percentile": best.get("percentile"),
            "base_adverse_rate": float(base_rate),
            "n_rows": int(mask_label.sum()),
            "sweep": sweep,
            "pass": passed,
        }

    return results


def run_v3_adverse_selection(l2_data):
    """V3: Post-fill adverse selection measurement.

    From L2 trade data, compute mid-price change after each trade
    at 1s, 5s, 10s horizons.
    """
    print("\n=== V3: Post-Fill Adverse Selection ===")

    if l2_data is None:
        print("  SKIPPED: No L2 data available")
        return {"skipped": True}

    # Extract trades and L1 quotes
    trades = l2_data[l2_data["ev"] == TRADE_EV]
    bids = l2_data[l2_data["ev"] == BID_L1_EV]
    asks = l2_data[l2_data["ev"] == ASK_L1_EV]

    print(f"  Total trades: {len(trades):,}")
    print(f"  Total bid updates: {len(bids):,}")
    print(f"  Total ask updates: {len(asks):,}")

    # Build mid-price series from bid/ask L1
    # Interleave bid and ask by timestamp to get BBO snapshots
    # Simpler: use the L1 data directly for this
    # Actually, let's merge bid/ask by timestamp
    all_quotes = np.concatenate([bids, asks])
    all_quotes.sort(order="local_ts")

    # Build running BBO
    n_q = len(all_quotes)
    running_bid = np.zeros(n_q)
    running_ask = np.zeros(n_q)
    cur_bid, cur_ask = 0.0, 0.0

    # Vectorized approach: track latest bid and ask
    is_bid = all_quotes["ev"] == BID_L1_EV
    is_ask = all_quotes["ev"] == ASK_L1_EV

    # Forward-fill bid and ask prices
    bid_vals = np.where(is_bid, all_quotes["px"], np.nan)
    ask_vals = np.where(is_ask, all_quotes["px"], np.nan)

    # Forward fill
    _ffill(bid_vals)
    _ffill(ask_vals)

    mid_prices = (bid_vals + ask_vals) / 2.0
    quote_ts = all_quotes["local_ts"]

    # For each trade, find mid-price at trade time and at +1s, +5s, +10s
    trade_ts = trades["local_ts"]
    trade_px = trades["px"]

    # Classify trade side: buy if trade_px >= mid at time, sell otherwise
    # Find mid-price at each trade time via searchsorted
    trade_idx = np.searchsorted(quote_ts, trade_ts, side="right") - 1
    trade_idx = np.clip(trade_idx, 0, len(mid_prices) - 1)
    trade_mid = mid_prices[trade_idx]

    is_buy = trade_px >= trade_mid
    trade_sign = np.where(is_buy, 1.0, -1.0)

    horizons_ns = {
        "1s": 1_000_000_000,
        "5s": 5_000_000_000,
        "10s": 10_000_000_000,
    }

    results = {}
    for label, ns in horizons_ns.items():
        future_ts = trade_ts + ns
        future_idx = np.searchsorted(quote_ts, future_ts, side="right") - 1
        future_idx = np.clip(future_idx, 0, len(mid_prices) - 1)
        future_mid = mid_prices[future_idx]

        # Adverse selection = signed return in direction of trade
        signed_ret = trade_sign * (future_mid - trade_mid)

        # Fraction of trades where price moved against us (AS > 0 for counterparty)
        adverse_frac = (signed_ret > 0).mean()
        mean_as = signed_ret.mean()
        mean_as_bps = mean_as / trade_mid.mean() * 10000

        print(f"  {label}: mean AS = {mean_as:.3f} pts ({mean_as_bps:.3f} bps), "
              f"adverse fraction = {adverse_frac*100:.1f}%")

        results[label] = {
            "mean_adverse_selection_pts": float(mean_as),
            "mean_adverse_selection_bps": float(mean_as_bps),
            "adverse_fraction": float(adverse_frac),
            "n_trades": int(len(trades)),
        }

    # V3 bonus: OFI-conditioned AS
    # Compute rolling OFI on quote data, then check AS in high-OFI vs low-OFI windows
    print("\n  --- OFI-conditioned adverse selection (1s horizon) ---")

    # Build OFI from L1 quote changes
    ofi_q = np.zeros(n_q)
    # bid contribution
    bid_up = np.zeros(n_q, dtype=bool)
    bid_same = np.zeros(n_q, dtype=bool)
    bid_up[1:] = bid_vals[1:] > bid_vals[:-1]
    bid_same[1:] = bid_vals[1:] == bid_vals[:-1]

    bq = all_quotes["qty"]
    bid_c = np.zeros(n_q)
    bid_c[1:] = np.where(bid_up[1:], bq[1:],
                          np.where(bid_same[1:], bq[1:] - bq[:-1], -bq[:-1]))
    # Only count bid-side events
    bid_c[~is_bid] = 0

    ask_down = np.zeros(n_q, dtype=bool)
    ask_same = np.zeros(n_q, dtype=bool)
    ask_down[1:] = ask_vals[1:] < ask_vals[:-1]
    ask_same[1:] = ask_vals[1:] == ask_vals[:-1]
    ask_c = np.zeros(n_q)
    ask_c[1:] = np.where(ask_down[1:], -bq[1:],
                          np.where(ask_same[1:], -(bq[1:] - bq[:-1]), bq[:-1]))
    ask_c[~is_ask] = 0

    ofi_q = bid_c + ask_c
    rofi_q = np.cumsum(ofi_q)
    window = 200  # ~200 events
    rofi_rolling = np.zeros(n_q)
    rofi_rolling[window:] = rofi_q[window:] - rofi_q[:-window]

    # Get rolling OFI at each trade time
    trade_rofi = rofi_rolling[trade_idx]
    abs_trade_rofi = np.abs(trade_rofi)

    # Split into high/low OFI
    median_rofi = np.median(abs_trade_rofi)
    high_ofi = abs_trade_rofi > np.percentile(abs_trade_rofi, 75)
    low_ofi = abs_trade_rofi < np.percentile(abs_trade_rofi, 25)

    ns_1s = horizons_ns["1s"]
    future_ts_1s = trade_ts + ns_1s
    future_idx_1s = np.searchsorted(quote_ts, future_ts_1s, side="right") - 1
    future_idx_1s = np.clip(future_idx_1s, 0, len(mid_prices) - 1)
    future_mid_1s = mid_prices[future_idx_1s]
    signed_ret_1s = trade_sign * (future_mid_1s - trade_mid)

    as_high = signed_ret_1s[high_ofi].mean()
    as_low = signed_ret_1s[low_ofi].mean()
    as_high_bps = as_high / trade_mid[high_ofi].mean() * 10000
    as_low_bps = as_low / trade_mid[low_ofi].mean() * 10000

    print(f"  High-OFI trades (Q4): AS = {as_high:.3f} pts ({as_high_bps:.3f} bps)")
    print(f"  Low-OFI trades (Q1):  AS = {as_low:.3f} pts ({as_low_bps:.3f} bps)")
    print(f"  AS difference (addressable by flow classification): {(as_high_bps - as_low_bps):.3f} bps")

    results["ofi_conditioned"] = {
        "high_ofi_as_bps": float(as_high_bps),
        "low_ofi_as_bps": float(as_low_bps),
        "addressable_as_bps": float(as_high_bps - as_low_bps),
    }

    return results


def run_v4_economic_impact(v1_results, v2_results, v3_results):
    """V4: Economic impact calculation.

    If OpportunisticMM activates 2.1% of time, and we improve adverse
    selection by X%, what is the daily NTD impact?
    """
    print("\n=== V4: Economic Impact Calculation ===")

    # TXFD6 daily volume: ~150K trades from L2 data (4 days)
    # OpportunisticMM activation: 2.1% of time
    # Assume ~50 trades per activation window, activation ~100 times/day

    # From V3, get addressable AS
    addressable_bps = 0.0
    if v3_results and not v3_results.get("skipped"):
        ofi_cond = v3_results.get("ofi_conditioned", {})
        addressable_bps = ofi_cond.get("addressable_as_bps", 0)

    # Baseline data
    # Average daily trade count from L2: 152K/day * 4 days
    daily_trades = 152000
    mm_activation = MM_ACTIVATION_RATE
    mm_trades_per_day = daily_trades * mm_activation
    # Average TXFD6 mid-price ~ 33000 (index points)
    avg_mid = 33000.0

    # If we can avoid trading in top-quartile toxic windows:
    # We skip 25% of opportunities but avoid worst AS
    skip_fraction = 0.25
    remaining_trades = mm_trades_per_day * (1 - skip_fraction)

    # Improvement per trade
    improvement_per_trade_pts = addressable_bps / 10000 * avg_mid
    improvement_per_trade_ntd = improvement_per_trade_pts * POINT_VALUE_NTD

    # Daily improvement
    daily_improvement_ntd = remaining_trades * improvement_per_trade_ntd

    # But we also LOSE the spread capture from skipped trades
    # Spread capture per trade: ~2.5 bps (wide spread regime)
    spread_capture_pts = WIDE_SPREAD_BPS / 10000 * avg_mid
    spread_capture_ntd = spread_capture_pts * POINT_VALUE_NTD
    # Not all skipped trades would have been profitable, but ~50% are
    lost_spread_ntd = mm_trades_per_day * skip_fraction * 0.5 * spread_capture_ntd

    net_daily_ntd = daily_improvement_ntd - lost_spread_ntd

    print(f"  Daily MM trades (2.1% activation): {mm_trades_per_day:.0f}")
    print(f"  Addressable AS difference: {addressable_bps:.3f} bps")
    print(f"  Improvement per trade: {improvement_per_trade_pts:.4f} pts = {improvement_per_trade_ntd:.2f} NTD")
    print(f"  Daily AS improvement (skip Q4 toxic): {daily_improvement_ntd:.0f} NTD")
    print(f"  Lost spread revenue from skipped trades: {lost_spread_ntd:.0f} NTD")
    print(f"  Net daily impact: {net_daily_ntd:.0f} NTD")
    print(f"  Net monthly impact (~22 trading days): {net_daily_ntd * 22:.0f} NTD")
    print(f"  Economically meaningful (>100 NTD/day)? {'YES' if net_daily_ntd > 100 else 'NO'}")

    return {
        "daily_mm_trades": float(mm_trades_per_day),
        "addressable_as_bps": float(addressable_bps),
        "improvement_per_trade_ntd": float(improvement_per_trade_ntd),
        "daily_improvement_ntd": float(daily_improvement_ntd),
        "lost_spread_ntd": float(lost_spread_ntd),
        "net_daily_ntd": float(net_daily_ntd),
        "net_monthly_ntd": float(net_daily_ntd * 22),
        "economically_meaningful": net_daily_ntd > 100,
    }


def run_v5_version_check():
    """V5: Confirm strategy depends on lob_shared_v1 (16 features), not v2."""
    print("\n=== V5: Feature Engine Version Check ===")

    # Check OpportunisticMM strategy config
    opp_mm_path = Path("src/hft_platform/strategies/opportunistic_mm.py")
    if opp_mm_path.exists():
        content = opp_mm_path.read_text()
        has_v2 = "lob_shared_v2" in content or "v2" in content.lower()
        has_v1 = "lob_shared_v1" in content or "feature_engine" in content.lower()
        print(f"  OpportunisticMM references v2: {has_v2}")
        print(f"  OpportunisticMM references v1/feature_engine: {has_v1}")
        if has_v2:
            print("  WARNING: Strategy references v2 — should be v1 (16 features)")
        result = "v1_confirmed" if not has_v2 else "v2_detected"
    else:
        print(f"  OpportunisticMM file not found at {opp_mm_path}")
        result = "file_not_found"

    # Check feature engine config
    fe_path = Path("src/hft_platform/feature/engine.py")
    if fe_path.exists():
        content = fe_path.read_text()
        # Count registered features
        n_features = content.count("register_feature") if "register_feature" in content else "unknown"
        print(f"  FeatureEngine registered features: {n_features}")

    print(f"  Result: {result}")
    return {"version": result}


def _ffill(arr):
    """Forward-fill NaN values in-place."""
    mask = np.isnan(arr)
    idx = np.where(~mask, np.arange(len(arr)), 0)
    np.maximum.accumulate(idx, out=idx)
    arr[:] = arr[idx]


def _compute_auc(scores, labels):
    """Compute AUC-ROC without sklearn.

    Uses the Mann-Whitney U statistic formulation:
    AUC = P(score_positive > score_negative)
    """
    pos = scores[labels == 1]
    neg = scores[labels == 0]

    if len(pos) == 0 or len(neg) == 0:
        return 0.5

    # Subsample for efficiency if too many samples
    max_samples = 500_000
    if len(pos) > max_samples:
        rng = np.random.RandomState(42)
        pos = rng.choice(pos, max_samples, replace=False)
    if len(neg) > max_samples:
        rng = np.random.RandomState(42)
        neg = rng.choice(neg, max_samples, replace=False)

    # Sort-based AUC computation
    all_scores = np.concatenate([pos, neg])
    all_labels = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))])

    order = np.argsort(all_scores)
    all_labels_sorted = all_labels[order]

    # Rank-based formula
    ranks = np.arange(1, len(all_scores) + 1)
    pos_rank_sum = ranks[all_labels_sorted == 1].sum()
    n_pos = len(pos)
    n_neg = len(neg)

    auc = (pos_rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    return float(auc)


def main():
    print("=" * 70)
    print("Phase 2A Empirical Validation: Toxic Flow Detection (C1)")
    print("=" * 70)

    # Load data
    print("\nLoading L1 BBO data (all days)...")
    l1_data = load_l1_all()
    print(f"  Loaded {len(l1_data):,} L1 rows")

    print("\nLoading L2 trade data...")
    l2_data = load_l2_days()

    # Run validations
    v1 = run_v1_auc(l1_data)
    v2 = run_v2_threshold_detector(l1_data)
    v3 = run_v3_adverse_selection(l2_data)
    v4 = run_v4_economic_impact(v1, v2, v3)
    v5 = run_v5_version_check()

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    v1_pass = v1["pass"]
    # Check V2 pass in wide_spread regime (relevant for OpportunisticMM)
    v2_key = "wide_spread_only" if "wide_spread_only" in v2 else "all_spreads"
    v2_pass = v2.get(v2_key, {}).get("pass", False)
    v4_viable = v4.get("economically_meaningful", False)

    print(f"  V1 AUC:        {v1['auc_unsigned']:.4f}  {'PASS' if v1_pass else 'FAIL'} (threshold: 0.60)")
    print(f"  V2 Detector:   prec={v2.get(v2_key,{}).get('best_precision',0)*100:.1f}%, "
          f"rec={v2.get(v2_key,{}).get('best_recall',0)*100:.1f}%  "
          f"{'PASS' if v2_pass else 'FAIL'}")
    print(f"  V3 AS:         " + ("computed" if not v3.get("skipped") else "SKIPPED"))
    print(f"  V4 Economics:  net {v4['net_daily_ntd']:.0f} NTD/day  "
          f"{'VIABLE' if v4_viable else 'NOT VIABLE'}")
    print(f"  V5 Version:    {v5['version']}")

    overall = v1_pass and v2_pass
    print(f"\n  OVERALL: {'PROCEED with C1' if overall else 'KILL C1 — insufficient signal'}")

    # Save results
    all_results = {
        "validation": "C1_toxic_flow_detection",
        "date": "2026-03-26",
        "v1_auc": v1,
        "v2_threshold_detector": v2,
        "v3_adverse_selection": v3,
        "v4_economic_impact": v4,
        "v5_version_check": v5,
        "overall_pass": overall,
        "recommendation": "PROCEED" if overall else "KILL",
    }

    os.makedirs(str(OUT_DIR), exist_ok=True)
    json_path = OUT_DIR / "round16_stage2a_c1_data.json"
    with open(str(json_path), "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n  Results saved to {json_path}")

    return all_results


if __name__ == "__main__":
    results = main()
