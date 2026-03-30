"""
C2 Fill-Probability-Aware Contrarian Maker — Empirical Validation
=================================================================
Validates reversal magnitude, fill rates, tick resolution, and economic
viability using TXFD6 L1 historical data.

V1: Reversal magnitude after extreme imbalance
V2: Wide-spread vs narrow-spread reversal rate
V3: Tick resolution analysis (tick density per time window)
V4: Fill rate at contrarian touch
V5: Queue position investigation (code search — done externally)
V6: Adverse fill prediction (logistic regression AUC)
V7: Economic model validation
"""

import json
import sys
from pathlib import Path

import numpy as np

# ── Constants ──────────────────────────────────────────────────────────────
TICK_SIZE = 1            # 1 index point
POINT_VALUE_NTD = 10     # 1 point = 10 NTD for Mini-TAIEX
RT_COST_BPS = 2.18       # round-trip cost in bps
INDEX_LEVEL = 21000      # approximate index level for bps conversion
# RT cost in ticks: 2.18 bps * 33000 / 10000 / 1 point ≈ 0.72 ticks at 33000
# But we use actual mid_price for accurate calc

DATA_PATH = Path("research/data/raw/txfd6/TXFD6_all_l1.npy")

# Imbalance thresholds
EXTREME_PCT = 10   # top/bottom 10%

# Forward windows in ticks
FWD_WINDOWS = [10, 20, 40, 80]

# Spread threshold for wide vs narrow (bps)
SPREAD_THRESHOLD_BPS = 2.5


def load_data():
    """Load and filter L1 data to valid trading hours (reasonable spreads)."""
    data = np.load(DATA_PATH, allow_pickle=True)
    print(f"Loaded {len(data):,} rows")

    # Filter to valid quotes (ask > bid) with reasonable spread (< 100 bps = active trading)
    valid = (data["ask_px"] > data["bid_px"]) & (data["spread_bps"] < 100)
    data = data[valid]
    print(f"After filtering spread < 100 bps: {len(data):,} rows ({valid.sum() / len(valid) * 100:.1f}%)")
    return data


def compute_imbalance(data):
    """L1 bid/ask quantity imbalance: (bid_qty - ask_qty) / (bid_qty + ask_qty)."""
    total = data["bid_qty"] + data["ask_qty"]
    # Avoid division by zero
    safe = total > 0
    imb = np.zeros(len(data))
    imb[safe] = (data["bid_qty"][safe] - data["ask_qty"][safe]) / total[safe]
    return imb


def v1_reversal_magnitude(data, imbalance):
    """V1: Measure mean price reversal within N ticks after extreme imbalance."""
    print("\n" + "=" * 70)
    print("V1: REVERSAL MAGNITUDE AFTER EXTREME IMBALANCE")
    print("=" * 70)

    lo_thresh = np.percentile(imbalance, EXTREME_PCT)
    hi_thresh = np.percentile(imbalance, 100 - EXTREME_PCT)
    print(f"Extreme imbalance thresholds: lo={lo_thresh:.3f}, hi={hi_thresh:.3f}")

    mid = data["mid_price"]
    n = len(mid)

    # Extreme bearish imbalance (bid << ask) → expect price to drop then revert up
    bear_idx = np.where(imbalance <= lo_thresh)[0]
    # Extreme bullish imbalance (bid >> ask) → expect price to rise then revert down
    bull_idx = np.where(imbalance >= hi_thresh)[0]

    print(f"Extreme bearish events: {len(bear_idx):,}")
    print(f"Extreme bullish events: {len(bull_idx):,}")

    results = {}
    for label, idx, sign in [
        ("bearish_extreme", bear_idx, 1),   # price down, reversal = up = positive
        ("bullish_extreme", bull_idx, -1),   # price up, reversal = down = negative (we measure magnitude)
    ]:
        reversals_by_window = {}
        for w in FWD_WINDOWS:
            valid_idx = idx[idx + w < n]
            if len(valid_idx) == 0:
                reversals_by_window[w] = {"mean": 0, "median": 0, "std": 0, "n": 0}
                continue
            # Price change = mid[t+w] - mid[t], in points (tick units)
            fwd_change = mid[valid_idx + w] - mid[valid_idx]
            # For bearish: imbalance is very negative (sellers dominate), so price should be
            # depressed; reversal = price going UP = positive fwd_change
            # For bullish: opposite
            reversal = sign * fwd_change  # positive = reversal direction

            reversals_by_window[w] = {
                "mean_ticks": float(np.mean(reversal)),
                "median_ticks": float(np.median(reversal)),
                "std_ticks": float(np.std(reversal)),
                "pct_positive": float((reversal > 0).mean() * 100),
                "n": int(len(valid_idx)),
            }
            print(f"  {label} +{w} ticks: mean_reversal={reversal.mean():.3f} pts, "
                  f"median={np.median(reversal):.3f}, pct_positive={((reversal > 0).mean() * 100):.1f}%, n={len(valid_idx)}")
        results[label] = reversals_by_window

    # Combined: absolute reversal regardless of direction
    all_extreme_idx = np.concatenate([bear_idx, bull_idx])
    combined = {}
    for w in FWD_WINDOWS:
        valid_bear = bear_idx[bear_idx + w < n]
        valid_bull = bull_idx[bull_idx + w < n]
        bear_rev = mid[valid_bear + w] - mid[valid_bear]           # expect positive
        bull_rev = -(mid[valid_bull + w] - mid[valid_bull])        # expect positive
        all_rev = np.concatenate([bear_rev, bull_rev])
        combined[w] = {
            "mean_reversal_ticks": float(np.mean(all_rev)),
            "median_reversal_ticks": float(np.median(all_rev)),
            "pct_correct_direction": float((all_rev > 0).mean() * 100),
            "n": int(len(all_rev)),
        }
        print(f"  COMBINED +{w} ticks: mean={np.mean(all_rev):.3f}, "
              f"median={np.median(all_rev):.3f}, pct_correct={((all_rev > 0).mean() * 100):.1f}%")
    results["combined"] = combined

    # PASS/FAIL
    rev_40 = combined[40]["mean_reversal_ticks"]
    if rev_40 >= 2.0:
        verdict = "PASS"
    elif rev_40 >= 1.5:
        verdict = "MARGINAL"
    else:
        verdict = "FAIL"
    print(f"\n  V1 VERDICT: {verdict} (mean reversal @40 ticks = {rev_40:.3f}, threshold >= 2.0)")
    results["verdict"] = verdict
    results["mean_reversal_40"] = rev_40
    return results


def v2_spread_conditioned_reversal(data, imbalance):
    """V2: Reversal rate during wide vs narrow spread."""
    print("\n" + "=" * 70)
    print("V2: WIDE-SPREAD VS NARROW-SPREAD REVERSAL RATE")
    print("=" * 70)

    mid = data["mid_price"]
    spread_bps = data["spread_bps"]
    n = len(mid)

    # Detect 1-tick price drops: mid[t+1] < mid[t]
    price_drop_idx = np.where(np.diff(mid) < 0)[0]
    # Only keep those where we can look 10 ticks forward
    price_drop_idx = price_drop_idx[price_drop_idx + 11 < n]

    # Reversal = price recovers within 10 ticks (mid[t+k] >= mid[t] for some k in 1..10)
    results = {}
    for label, condition in [
        ("wide_spread", spread_bps[price_drop_idx] > SPREAD_THRESHOLD_BPS),
        ("narrow_spread", spread_bps[price_drop_idx] <= SPREAD_THRESHOLD_BPS),
        ("all", np.ones(len(price_drop_idx), dtype=bool)),
    ]:
        idx = price_drop_idx[condition]
        if len(idx) == 0:
            results[label] = {"reversal_rate": 0, "n": 0}
            continue
        pre_drop_mid = mid[idx]
        # Check if price returns to pre-drop level within 10 ticks
        recovered = np.zeros(len(idx), dtype=bool)
        for offset in range(1, 11):
            recovered |= (mid[idx + offset] >= pre_drop_mid)

        rate = recovered.mean() * 100
        results[label] = {
            "reversal_rate_pct": float(rate),
            "n": int(len(idx)),
        }
        print(f"  {label}: reversal rate = {rate:.1f}% (n={len(idx):,})")

    wide_rate = results.get("wide_spread", {}).get("reversal_rate_pct", 0)
    verdict = "PASS" if wide_rate >= 30 else "FAIL"
    print(f"\n  V2 VERDICT: {verdict} (wide-spread reversal = {wide_rate:.1f}%, threshold >= 30%)")
    results["verdict"] = verdict
    return results


def v3_tick_resolution(data):
    """V3: Distribution of ticks per time window."""
    print("\n" + "=" * 70)
    print("V3: TICK RESOLUTION ANALYSIS")
    print("=" * 70)

    ts = data["local_ts"]
    # Use per-day analysis to avoid cross-day gaps
    # Convert ns timestamps to seconds for binning
    windows_ms = [100, 5000, 30000]  # 100ms, 5s, 30s
    results = {}

    for w_ms in windows_ms:
        w_ns = w_ms * 1_000_000
        # Count ticks in rolling windows (sample every 1000th point for efficiency)
        sample_idx = np.arange(0, len(ts) - 1, max(1, len(ts) // 50000))
        counts = []
        for i in sample_idx:
            end_ts = ts[i] + w_ns
            # Binary search for end
            j = np.searchsorted(ts, end_ts, side="right")
            counts.append(j - i)
        counts = np.array(counts)
        results[f"{w_ms}ms"] = {
            "mean": float(np.mean(counts)),
            "median": float(np.median(counts)),
            "p10": float(np.percentile(counts, 10)),
            "p90": float(np.percentile(counts, 90)),
            "pct_zero": float((counts == 0).mean() * 100),
        }
        print(f"  {w_ms}ms window: mean={np.mean(counts):.1f}, median={np.median(counts):.0f}, "
              f"p10={np.percentile(counts, 10):.0f}, p90={np.percentile(counts, 90):.0f}, "
              f"pct_zero={((counts == 0).mean() * 100):.1f}%")

    # Albers needs high tick density. If median ticks in 100ms < 1, features are infeasible
    med_100ms = results["100ms"]["median"]
    verdict = "PASS" if med_100ms >= 2 else "FAIL"
    print(f"\n  V3 VERDICT: {verdict} (median ticks in 100ms = {med_100ms:.1f}, need >= 2 for Albers features)")
    results["verdict"] = verdict
    return results


def v4_fill_rate_contrarian(data, imbalance):
    """V4: Fill rate if we place contrarian limit order during extreme imbalance."""
    print("\n" + "=" * 70)
    print("V4: FILL RATE AT CONTRARIAN TOUCH")
    print("=" * 70)

    mid = data["mid_price"]
    bid = data["bid_px"]
    ask = data["ask_px"]
    n = len(mid)

    lo_thresh = np.percentile(imbalance, EXTREME_PCT)
    hi_thresh = np.percentile(imbalance, 100 - EXTREME_PCT)

    results = {}

    # Extreme bearish imbalance → contrarian BUY at best bid
    # Fill = ask drops to our bid price (someone sells to us)
    bear_idx = np.where(imbalance <= lo_thresh)[0]
    # Extreme bullish imbalance → contrarian SELL at best ask
    # Fill = bid rises to our ask price (someone buys from us)
    bull_idx = np.where(imbalance >= hi_thresh)[0]

    for label, idx, our_price_field, touch_field in [
        ("contrarian_buy", bear_idx, "bid_px", "ask_px"),  # we bid; fill if market ask touches our bid? No.
        # Actually: we place limit buy at best bid. We fill if someone sells at our price.
        # Proxy: the traded price (mid or ask) drops to our bid level.
        # Better proxy: ask_px at future tick <= our bid_px (market crosses to us)
        ("contrarian_sell", bull_idx, "ask_px", "bid_px"),
    ]:
        fill_rates = {}
        for w in [10, 20, 40]:
            valid_idx = idx[idx + w < n]
            if len(valid_idx) == 0:
                fill_rates[w] = 0.0
                continue

            if label == "contrarian_buy":
                our_price = bid[valid_idx]  # we bid at current best bid
                # Fill proxy: at any future tick within window, ask_px <= our_price
                # (someone willing to sell at or below our bid)
                # Or more conservatively: the trade happens at our price = mid touches our bid
                filled = np.zeros(len(valid_idx), dtype=bool)
                for offset in range(1, w + 1):
                    # Price drops enough that ask reaches our bid (crossed spread)
                    filled |= (ask[valid_idx + offset] <= our_price)
                    # Or even: mid drops to our bid level
                    # filled |= (mid[valid_idx + offset] <= our_price)
            else:
                our_price = ask[valid_idx]  # we ask at current best ask
                filled = np.zeros(len(valid_idx), dtype=bool)
                for offset in range(1, w + 1):
                    filled |= (bid[valid_idx + offset] >= our_price)

            rate = filled.mean() * 100
            fill_rates[w] = float(rate)
            print(f"  {label} fill within {w} ticks: {rate:.1f}% (n={len(valid_idx):,})")
        results[label] = fill_rates

    # Also compute a softer fill proxy: price touches our level (mid reaches bid/ask)
    print("\n  Softer fill proxy (mid crosses our price):")
    for label, idx, our_price_field in [
        ("contrarian_buy_soft", bear_idx, "bid_px"),
        ("contrarian_sell_soft", bull_idx, "ask_px"),
    ]:
        fill_rates = {}
        for w in [10, 20, 40]:
            valid_idx = idx[idx + w < n]
            if len(valid_idx) == 0:
                fill_rates[w] = 0.0
                continue
            if "buy" in label:
                our_price = data[our_price_field][valid_idx]
                filled = np.zeros(len(valid_idx), dtype=bool)
                for offset in range(1, w + 1):
                    filled |= (mid[valid_idx + offset] <= our_price)
            else:
                our_price = data[our_price_field][valid_idx]
                filled = np.zeros(len(valid_idx), dtype=bool)
                for offset in range(1, w + 1):
                    filled |= (mid[valid_idx + offset] >= our_price)
            rate = filled.mean() * 100
            fill_rates[w] = float(rate)
            print(f"  {label} fill within {w} ticks: {rate:.1f}% (n={len(valid_idx):,})")
        results[label] = fill_rates

    # Combined strict fill rate at 10 ticks
    buy_10 = results.get("contrarian_buy", {}).get(10, 0)
    sell_10 = results.get("contrarian_sell", {}).get(10, 0)
    avg_10 = (buy_10 + sell_10) / 2
    verdict = "PASS" if avg_10 >= 20 else "FAIL"
    print(f"\n  V4 VERDICT: {verdict} (avg strict fill @10 ticks = {avg_10:.1f}%, threshold >= 20%)")
    results["verdict"] = verdict
    results["avg_fill_rate_10"] = avg_10
    return results


def v6_adverse_fill_prediction(data, imbalance):
    """V6: Can we predict adverse fills ex-ante using LOB features?"""
    print("\n" + "=" * 70)
    print("V6: EX-ANTE ADVERSE FILL PREDICTION (AUC)")
    print("=" * 70)

    mid = data["mid_price"]
    spread_bps = data["spread_bps"]
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]
    n = len(mid)

    # Forward 40-tick return (proxy for 5s at 125ms median interval)
    fwd_window = 40
    valid_idx = np.arange(0, n - fwd_window)
    fwd_return = mid[valid_idx + fwd_window] - mid[valid_idx]

    # For hypothetical BUY fills: adverse = price drops after fill (worst 20%)
    # For simplicity, use absolute return magnitude; worst 20% = largest adverse move
    # Label: 1 if in worst 20% (most negative return for buys)
    worst_20_thresh = np.percentile(fwd_return, 20)
    labels = (fwd_return <= worst_20_thresh).astype(int)

    # Features at submission time
    features = np.column_stack([
        imbalance[valid_idx],
        spread_bps[valid_idx],
        bid_qty[valid_idx],
        ask_qty[valid_idx],
        bid_qty[valid_idx] / np.maximum(ask_qty[valid_idx], 1),
    ])

    # Simple logistic regression (manual via sigmoid + gradient descent for no sklearn dependency)
    # Actually, let's try sklearn if available, else manual
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import train_test_split

        # Subsample for speed
        n_sample = min(500_000, len(valid_idx))
        rng = np.random.RandomState(42)
        sample_idx = rng.choice(len(valid_idx), n_sample, replace=False)

        X = features[sample_idx]
        y = labels[sample_idx]

        # Normalize features
        X_mean = X.mean(axis=0)
        X_std = X.std(axis=0) + 1e-10
        X_norm = (X - X_mean) / X_std

        X_train, X_test, y_train, y_test = train_test_split(X_norm, y, test_size=0.3, random_state=42)

        clf = LogisticRegression(max_iter=200, random_state=42)
        clf.fit(X_train, y_train)
        y_prob = clf.predict_proba(X_test)[:, 1]
        auc = roc_auc_score(y_test, y_prob)

        print(f"  Logistic regression AUC: {auc:.4f}")
        print(f"  Feature importances (coef): {dict(zip(['imbalance', 'spread_bps', 'bid_qty', 'ask_qty', 'bid_ask_ratio'], clf.coef_[0].tolist()))}")

        verdict = "PASS" if auc > 0.55 else "FAIL"
        print(f"\n  V6 VERDICT: {verdict} (AUC = {auc:.4f}, threshold > 0.55)")
        return {"auc": float(auc), "verdict": verdict, "n_train": len(X_train), "n_test": len(X_test)}

    except ImportError:
        # Manual AUC via simple threshold on imbalance
        print("  sklearn not available, using manual AUC on imbalance feature only")
        n_sample = min(500_000, len(valid_idx))
        rng = np.random.RandomState(42)
        sample_idx = rng.choice(len(valid_idx), n_sample, replace=False)
        scores = -imbalance[valid_idx[sample_idx]]  # negative imbalance → bearish → adverse for buys
        y = labels[sample_idx]

        # Sort by score descending, compute AUC
        sorted_order = np.argsort(-scores)
        y_sorted = y[sorted_order]
        n_pos = y.sum()
        n_neg = len(y) - n_pos
        tp_cumsum = np.cumsum(y_sorted)
        fp_cumsum = np.arange(1, len(y) + 1) - tp_cumsum
        tpr = tp_cumsum / n_pos
        fpr = fp_cumsum / n_neg
        auc = float(np.trapz(tpr, fpr))

        print(f"  Manual AUC (imbalance only): {auc:.4f}")
        verdict = "PASS" if auc > 0.55 else "FAIL"
        print(f"\n  V6 VERDICT: {verdict} (AUC = {auc:.4f}, threshold > 0.55)")
        return {"auc": auc, "verdict": verdict, "method": "manual_imbalance_only", "n": n_sample}


def v7_economic_model(v1_results, v4_results, data):
    """V7: Economic viability given measured reversal and fill rates."""
    print("\n" + "=" * 70)
    print("V7: ECONOMIC MODEL VALIDATION")
    print("=" * 70)

    mid = data["mid_price"]
    avg_mid = float(np.mean(mid))
    avg_spread = float(np.mean(data["ask_px"] - data["bid_px"]))

    # RT cost in points
    rt_cost_points = RT_COST_BPS * avg_mid / 10000
    rt_cost_ntd = rt_cost_points * POINT_VALUE_NTD

    # Revenue per trade: spread capture (half spread) + reversal profit - costs
    # Best case: we capture the spread (buy at bid, sell at ask)
    spread_capture_points = avg_spread / 2  # half spread on entry
    reversal_profit = v1_results.get("combined", {}).get(40, {}).get("mean_reversal_ticks", 0)

    # But we don't always fill: fill rate
    avg_fill = v4_results.get("avg_fill_rate_10", 0) / 100

    # Aggressive exit slippage: 1 tick
    exit_slippage = 1.0

    # Queue position disadvantage: we join at back, so adverse selection is worse
    # Estimate: queue position adds ~0.5 tick adverse selection
    queue_adverse = 0.5

    # Expected profit per attempted trade
    gross_per_fill = spread_capture_points + reversal_profit - exit_slippage - queue_adverse
    net_per_fill = gross_per_fill - rt_cost_points
    net_ntd_per_fill = net_per_fill * POINT_VALUE_NTD

    # Expected value per signal (accounting for fill probability)
    ev_per_signal = net_ntd_per_fill * avg_fill

    # Trade frequency: extreme imbalance events per day
    # From data: ~6.3M rows over 13 days ≈ 486k per day
    # 20% are extreme → ~97k signals per day
    # But overlapping signals → unique events maybe 10-20k per day
    # Conservative: 5k unique signals per day
    daily_signals = 5000
    daily_ev_ntd = ev_per_signal * daily_signals

    print(f"  Average mid price: {avg_mid:.0f}")
    print(f"  Average spread: {avg_spread:.1f} points")
    print(f"  RT cost: {rt_cost_points:.2f} points ({rt_cost_ntd:.1f} NTD)")
    print(f"  Spread capture (half): {spread_capture_points:.2f} points")
    print(f"  Mean reversal @40 ticks: {reversal_profit:.3f} points")
    print(f"  Exit slippage: {exit_slippage:.1f} points")
    print(f"  Queue adverse selection: {queue_adverse:.1f} points")
    print(f"  Gross per fill: {gross_per_fill:.3f} points")
    print(f"  Net per fill: {net_per_fill:.3f} points ({net_ntd_per_fill:.1f} NTD)")
    print(f"  Fill rate (strict @10): {avg_fill * 100:.1f}%")
    print(f"  EV per signal: {ev_per_signal:.2f} NTD")
    print(f"  Estimated daily signals: {daily_signals}")
    print(f"  Estimated daily EV: {daily_ev_ntd:.0f} NTD")

    verdict = "PASS" if net_per_fill > 0 and daily_ev_ntd > 500 else "FAIL"
    print(f"\n  V7 VERDICT: {verdict} (net/fill={net_per_fill:.3f} pts, daily EV={daily_ev_ntd:.0f} NTD)")

    return {
        "avg_mid": avg_mid,
        "avg_spread_points": avg_spread,
        "rt_cost_points": rt_cost_points,
        "rt_cost_ntd": rt_cost_ntd,
        "spread_capture_points": spread_capture_points,
        "reversal_profit_points": reversal_profit,
        "exit_slippage": exit_slippage,
        "queue_adverse": queue_adverse,
        "gross_per_fill": gross_per_fill,
        "net_per_fill": net_per_fill,
        "net_ntd_per_fill": net_ntd_per_fill,
        "fill_rate": avg_fill,
        "ev_per_signal_ntd": ev_per_signal,
        "daily_ev_ntd": daily_ev_ntd,
        "verdict": verdict,
    }


def main():
    print("C2 Fill-Probability-Aware Contrarian Maker — Empirical Validation")
    print("=" * 70)

    data = load_data()
    imbalance = compute_imbalance(data)

    v1 = v1_reversal_magnitude(data, imbalance)
    v2 = v2_spread_conditioned_reversal(data, imbalance)
    v3 = v3_tick_resolution(data)
    v4 = v4_fill_rate_contrarian(data, imbalance)
    v6 = v6_adverse_fill_prediction(data, imbalance)
    v7 = v7_economic_model(v1, v4, data)

    # V5 result (from codebase search)
    v5 = {
        "queue_position_available": False,
        "notes": "No queue position data in Shioaji SDK. Searched feed_adapter/shioaji/ for "
                 "queue_pos, position_in, rank, priority, ahead — no matches. "
                 "Queue position estimation must rely on visible depth only.",
        "verdict": "FAIL",
    }

    # Overall verdict
    all_results = {"v1": v1, "v2": v2, "v3": v3, "v4": v4, "v5": v5, "v6": v6, "v7": v7}

    verdicts = {k: v.get("verdict", "N/A") for k, v in all_results.items()}
    pass_count = sum(1 for v in verdicts.values() if v == "PASS")
    fail_count = sum(1 for v in verdicts.values() if v == "FAIL")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for k, v in verdicts.items():
        print(f"  {k}: {v}")
    print(f"\n  PASS: {pass_count}, FAIL: {fail_count}")

    # Kill decision
    kill_reasons = []
    if v1.get("mean_reversal_40", 0) < 1.5:
        kill_reasons.append(f"V1: reversal @40 = {v1.get('mean_reversal_40', 0):.3f} < 1.5 ticks")
    if v4.get("avg_fill_rate_10", 0) < 20:
        kill_reasons.append(f"V4: fill rate @10 = {v4.get('avg_fill_rate_10', 0):.1f}% < 20%")
    if v7.get("verdict") == "FAIL":
        kill_reasons.append(f"V7: economically unviable (daily EV = {v7.get('daily_ev_ntd', 0):.0f} NTD)")

    if kill_reasons:
        overall = "KILL C2"
        print(f"\n  OVERALL: >>> KILL C2 <<<")
        print(f"  Kill reasons:")
        for r in kill_reasons:
            print(f"    - {r}")
    else:
        overall = "PROCEED"
        print(f"\n  OVERALL: PROCEED TO NEXT STAGE")

    all_results["overall"] = overall
    all_results["kill_reasons"] = kill_reasons

    # Save JSON
    out_path = Path("outputs/team_artifacts/alpha-research/round16_stage2a_c2_validation.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n  Results saved to {out_path}")

    return all_results


if __name__ == "__main__":
    results = main()
