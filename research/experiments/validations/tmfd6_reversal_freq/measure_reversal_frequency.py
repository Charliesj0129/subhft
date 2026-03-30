"""
BLOCKER-E4: Measure TMFD6 OBI Reversal Frequency

Computes Order Book Imbalance (OBI) prediction accuracy and reversal rates
across multiple thresholds, horizons, spread regimes, and time-of-day buckets.

Also checks trade direction classification feasibility (BLOCKER-E5).
"""

import numpy as np
import datetime
import sys
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parents[4] / "research" / "data" / "raw" / "tmfd6" / "TMFD6_all_l1.npy"

# Taiwan is UTC+8
UTC_OFFSET_NS = 8 * 3600 * 10**9

# Trading hours in local time (nanosecond offsets from midnight)
OPENING_START = 8 * 3600 + 45 * 60   # 08:45
OPENING_END   = 9 * 3600 + 15 * 60   # 09:15
MIDDAY_END    = 12 * 3600             # 12:00
CLOSING_END   = 13 * 3600 + 45 * 60  # 13:45

THRESHOLDS = [0.1, 0.2, 0.3, 0.5]
HORIZONS_S = [1, 5, 30]


def local_ts_to_date_and_secofday(local_ts_ns):
    """Convert local_ts (ns, UTC) to (date_ordinal, seconds-of-day in local time)."""
    local_ns = local_ts_ns + UTC_OFFSET_NS
    # seconds since epoch in local time
    local_s = local_ns / 1e9
    # date ordinal (days since epoch)
    day_s = np.floor(local_s / 86400).astype(np.int32)
    sec_of_day = local_s - day_s * 86400.0
    return day_s, sec_of_day


def compute_forward_mid(local_ts, mid_price, day_ids, horizon_ns):
    """For each row, find mid_price at local_ts + horizon_ns (same day only).

    Returns: forward_mid array (NaN if cross-day or out of range).
    Uses searchsorted for efficiency.
    """
    n = len(local_ts)
    target_ts = local_ts + horizon_ns
    forward_mid = np.full(n, np.nan, dtype=np.float64)

    # Process per-day to avoid cross-day lookups
    unique_days = np.unique(day_ids)
    for day in unique_days:
        mask = day_ids == day
        indices = np.where(mask)[0]
        day_ts = local_ts[indices]
        day_mid = mid_price[indices]
        day_target = target_ts[indices]

        # searchsorted: find index of closest ts >= target
        insert_idx = np.searchsorted(day_ts, day_target, side='left')

        # Clamp to valid range within day
        valid = insert_idx < len(day_ts)
        valid_src = indices[valid]
        valid_dst_local = insert_idx[valid]

        forward_mid[valid_src] = day_mid[valid_dst_local]

    return forward_mid


def run_analysis():
    print("=" * 70)
    print("BLOCKER-E4: TMFD6 OBI Reversal Frequency Analysis")
    print("=" * 70)

    # Load data
    print("\nLoading data...")
    data = np.load(str(DATA_PATH))
    n_total = len(data)
    print(f"Total rows: {n_total:,}")

    bid_qty = data['bid_qty']
    ask_qty = data['ask_qty']
    mid_price = data['mid_price']
    local_ts = data['local_ts']
    bid_px = data['bid_px']
    ask_px = data['ask_px']
    volume = data['volume']

    # Compute spread in points
    spread_pts = ask_px - bid_px

    # Compute OBI
    total_qty = bid_qty + ask_qty
    valid_qty = total_qty > 0
    obi = np.zeros(n_total, dtype=np.float64)
    obi[valid_qty] = (bid_qty[valid_qty] - ask_qty[valid_qty]) / total_qty[valid_qty]
    obi[~valid_qty] = np.nan

    n_valid = valid_qty.sum()
    print(f"Valid OBI rows (qty > 0): {n_valid:,} ({100*n_valid/n_total:.1f}%)")
    print(f"OBI stats: mean={np.nanmean(obi):.4f}, std={np.nanstd(obi):.4f}, "
          f"median={np.nanmedian(obi):.4f}")

    # Compute date and time-of-day
    day_ids, sec_of_day = local_ts_to_date_and_secofday(local_ts)
    unique_days = np.unique(day_ids)
    print(f"Trading days: {len(unique_days)}")

    # Time-of-day masks
    mask_opening = (sec_of_day >= OPENING_START) & (sec_of_day < OPENING_END)
    mask_midday  = (sec_of_day >= OPENING_END) & (sec_of_day < MIDDAY_END)
    mask_closing = (sec_of_day >= MIDDAY_END) & (sec_of_day < CLOSING_END)

    print(f"\nTime buckets: opening={mask_opening.sum():,}, midday={mask_midday.sum():,}, "
          f"closing={mask_closing.sum():,}")

    # Pre-compute forward mid prices for each horizon
    print("\nComputing forward mid prices...")
    forward_mids = {}
    for h_s in HORIZONS_S:
        h_ns = h_s * 10**9
        print(f"  Horizon {h_s}s...")
        forward_mids[h_s] = compute_forward_mid(local_ts, mid_price, day_ids, h_ns)
        n_avail = np.isfinite(forward_mids[h_s]).sum()
        print(f"    Available: {n_avail:,} ({100*n_avail/n_total:.1f}%)")

    # ---- Section 2 & 3: OBI prediction accuracy and reversal rate ----
    print("\n" + "=" * 70)
    print("SECTION 2-3: OBI Prediction Accuracy & Reversal Rate (ALL rows)")
    print("=" * 70)

    results_all = {}
    for thresh in THRESHOLDS:
        mask_pos = obi > thresh
        mask_neg = obi < -thresh
        n_signal = mask_pos.sum() + mask_neg.sum()
        print(f"\n--- Threshold |OBI| > {thresh} ---")
        print(f"  Signal rows: {n_signal:,} ({100*n_signal/n_valid:.1f}% of valid)")
        print(f"    Positive (bid-heavy): {mask_pos.sum():,}")
        print(f"    Negative (ask-heavy): {mask_neg.sum():,}")

        for h_s in HORIZONS_S:
            fwd = forward_mids[h_s]
            delta = fwd - mid_price

            # Positive OBI predicts price UP
            pos_valid = mask_pos & np.isfinite(fwd)
            pos_correct = (delta[pos_valid] > 0).sum()
            pos_wrong = (delta[pos_valid] < 0).sum()
            pos_zero = (delta[pos_valid] == 0).sum()
            pos_total = pos_valid.sum()

            # Negative OBI predicts price DOWN
            neg_valid = mask_neg & np.isfinite(fwd)
            neg_correct = (delta[neg_valid] < 0).sum()
            neg_wrong = (delta[neg_valid] > 0).sum()
            neg_zero = (delta[neg_valid] == 0).sum()
            neg_total = neg_valid.sum()

            total_signals = pos_total + neg_total
            total_correct = pos_correct + neg_correct
            total_wrong = pos_wrong + neg_wrong
            total_zero = pos_zero + neg_zero

            if total_signals > 0:
                # Exclude zero-move rows from accuracy calc
                decisive = total_correct + total_wrong
                if decisive > 0:
                    accuracy = 100 * total_correct / decisive
                    reversal = 100 - accuracy
                else:
                    accuracy = reversal = np.nan

                print(f"  Horizon {h_s:2d}s: acc={accuracy:5.1f}%, "
                      f"reversal={reversal:5.1f}%, "
                      f"zero_move={100*total_zero/total_signals:5.1f}%, "
                      f"n={total_signals:,} (decisive={decisive:,})")
                results_all[(thresh, h_s)] = {
                    'accuracy': accuracy, 'reversal': reversal,
                    'n_signals': total_signals, 'n_decisive': decisive,
                    'zero_pct': 100*total_zero/total_signals
                }
            else:
                print(f"  Horizon {h_s:2d}s: no valid signals")
                results_all[(thresh, h_s)] = None

    # ---- Section 4: Conditional on spread >= 5 ----
    print("\n" + "=" * 70)
    print("SECTION 4: OBI Prediction — SPREAD >= 5 pts only")
    print("=" * 70)

    mask_wide = spread_pts >= 5
    n_wide = mask_wide.sum()
    print(f"Rows with spread >= 5: {n_wide:,} ({100*n_wide/n_total:.1f}%)")

    results_wide = {}
    for thresh in THRESHOLDS:
        mask_pos = (obi > thresh) & mask_wide
        mask_neg = (obi < -thresh) & mask_wide
        n_signal = mask_pos.sum() + mask_neg.sum()
        print(f"\n--- Threshold |OBI| > {thresh}, spread >= 5 ---")
        print(f"  Signal rows: {n_signal:,}")

        for h_s in HORIZONS_S:
            fwd = forward_mids[h_s]
            delta = fwd - mid_price

            pos_valid = mask_pos & np.isfinite(fwd)
            pos_correct = (delta[pos_valid] > 0).sum()
            pos_wrong = (delta[pos_valid] < 0).sum()
            pos_zero = (delta[pos_valid] == 0).sum()

            neg_valid = mask_neg & np.isfinite(fwd)
            neg_correct = (delta[neg_valid] < 0).sum()
            neg_wrong = (delta[neg_valid] > 0).sum()
            neg_zero = (delta[neg_valid] == 0).sum()

            total_signals = pos_valid.sum() + neg_valid.sum()
            total_correct = pos_correct + neg_correct
            total_wrong = pos_wrong + neg_wrong
            total_zero = pos_zero + neg_zero

            if total_signals > 0:
                decisive = total_correct + total_wrong
                if decisive > 0:
                    accuracy = 100 * total_correct / decisive
                    reversal = 100 - accuracy
                else:
                    accuracy = reversal = np.nan
                print(f"  Horizon {h_s:2d}s: acc={accuracy:5.1f}%, "
                      f"reversal={reversal:5.1f}%, "
                      f"zero_move={100*total_zero/total_signals:5.1f}%, "
                      f"n={total_signals:,} (decisive={decisive:,})")
                results_wide[(thresh, h_s)] = {
                    'accuracy': accuracy, 'reversal': reversal,
                    'n_signals': total_signals, 'n_decisive': decisive,
                    'zero_pct': 100*total_zero/total_signals
                }
            else:
                print(f"  Horizon {h_s:2d}s: no valid signals")
                results_wide[(thresh, h_s)] = None

    # ---- Section 5: Reversal rate by time-of-day ----
    print("\n" + "=" * 70)
    print("SECTION 5: Reversal Rate by Time-of-Day")
    print("=" * 70)

    results_tod = {}
    for period_name, period_mask in [("opening", mask_opening), ("midday", mask_midday), ("closing", mask_closing)]:
        print(f"\n=== {period_name.upper()} ===")
        results_tod[period_name] = {}

        for thresh in THRESHOLDS:
            mask_pos = (obi > thresh) & period_mask
            mask_neg = (obi < -thresh) & period_mask
            n_signal = mask_pos.sum() + mask_neg.sum()

            if n_signal < 100:
                print(f"  Threshold {thresh}: too few signals ({n_signal})")
                continue

            row_parts = []
            for h_s in HORIZONS_S:
                fwd = forward_mids[h_s]
                delta = fwd - mid_price

                pos_valid = mask_pos & np.isfinite(fwd)
                neg_valid = mask_neg & np.isfinite(fwd)

                correct = (delta[pos_valid] > 0).sum() + (delta[neg_valid] < 0).sum()
                wrong = (delta[pos_valid] < 0).sum() + (delta[neg_valid] > 0).sum()
                decisive = correct + wrong

                if decisive > 0:
                    acc = 100 * correct / decisive
                    rev = 100 - acc
                    row_parts.append(f"{h_s}s: acc={acc:.1f}% rev={rev:.1f}%")
                    results_tod[period_name][(thresh, h_s)] = {
                        'accuracy': acc, 'reversal': rev, 'n_decisive': decisive
                    }
                else:
                    row_parts.append(f"{h_s}s: N/A")

            print(f"  |OBI|>{thresh} (n={n_signal:,}): {' | '.join(row_parts)}")

    # ---- Section 5b: Wide spread + time-of-day ----
    print("\n" + "=" * 70)
    print("SECTION 5b: Reversal Rate by Time-of-Day (SPREAD >= 5 only)")
    print("=" * 70)

    for period_name, period_mask in [("opening", mask_opening), ("midday", mask_midday), ("closing", mask_closing)]:
        print(f"\n=== {period_name.upper()} ===")
        period_wide = period_mask & mask_wide

        for thresh in THRESHOLDS:
            mask_pos = (obi > thresh) & period_wide
            mask_neg = (obi < -thresh) & period_wide
            n_signal = mask_pos.sum() + mask_neg.sum()

            if n_signal < 50:
                print(f"  |OBI|>{thresh}: too few signals ({n_signal})")
                continue

            row_parts = []
            for h_s in HORIZONS_S:
                fwd = forward_mids[h_s]
                delta = fwd - mid_price

                pos_valid = mask_pos & np.isfinite(fwd)
                neg_valid = mask_neg & np.isfinite(fwd)

                correct = (delta[pos_valid] > 0).sum() + (delta[neg_valid] < 0).sum()
                wrong = (delta[pos_valid] < 0).sum() + (delta[neg_valid] > 0).sum()
                decisive = correct + wrong

                if decisive > 0:
                    acc = 100 * correct / decisive
                    rev = 100 - acc
                    row_parts.append(f"{h_s}s: acc={acc:.1f}% rev={rev:.1f}%")
                else:
                    row_parts.append(f"{h_s}s: N/A")

            print(f"  |OBI|>{thresh} (n={n_signal:,}): {' | '.join(row_parts)}")

    # ---- Section 6: BLOCKER-E5 Trade direction classification ----
    print("\n" + "=" * 70)
    print("SECTION 6: BLOCKER-E5 — Trade Direction Classification Feasibility")
    print("=" * 70)

    trade_mask = volume > 0
    n_trades = trade_mask.sum()
    print(f"Total trade events (volume > 0): {n_trades:,}")

    trade_indices = np.where(trade_mask)[0]
    # Tick rule: compare mid_price with previous row's mid_price
    if len(trade_indices) > 0:
        trade_mid = mid_price[trade_indices]
        prev_mid = mid_price[trade_indices - 1]  # index-1 is previous row
        # Handle first row edge case
        prev_mid[trade_indices == 0] = np.nan

        mid_delta = trade_mid - prev_mid
        n_buy = (mid_delta > 0).sum()
        n_sell = (mid_delta < 0).sum()
        n_unchanged = (mid_delta == 0).sum()
        n_nan = np.isnan(mid_delta).sum()
        n_classifiable = n_buy + n_sell

        print(f"  Buy-initiated  (mid up):   {n_buy:>8,} ({100*n_buy/n_trades:5.1f}%)")
        print(f"  Sell-initiated (mid down):  {n_sell:>8,} ({100*n_sell/n_trades:5.1f}%)")
        print(f"  Unclassifiable (mid same):  {n_unchanged:>8,} ({100*n_unchanged/n_trades:5.1f}%)")
        print(f"  Invalid (NaN):              {n_nan:>8,}")
        print(f"  Classifiable total:         {n_classifiable:>8,} ({100*n_classifiable/n_trades:5.1f}%)")

        # Also check with look-back tick rule (use last different mid)
        print("\n  Extended tick rule (look back to last changed mid):")
        # For each trade, walk backward to find last different mid
        # Efficient: compute running "last different mid" for all rows
        last_diff_mid = np.full(n_total, np.nan)
        last_diff_mid[0] = np.nan
        current_last = np.nan
        for i in range(1, min(n_total, 100000)):  # sample first 100k for speed
            if mid_price[i] != mid_price[i-1]:
                current_last = mid_price[i-1]
            last_diff_mid[i] = current_last

        sample_trades = trade_indices[trade_indices < 100000]
        if len(sample_trades) > 0:
            ext_delta = mid_price[sample_trades] - last_diff_mid[sample_trades]
            ext_valid = np.isfinite(ext_delta)
            ext_buy = (ext_delta[ext_valid] > 0).sum()
            ext_sell = (ext_delta[ext_valid] < 0).sum()
            ext_zero = (ext_delta[ext_valid] == 0).sum()
            ext_total = ext_valid.sum()
            print(f"    Sample (first 100k rows): {len(sample_trades):,} trades")
            print(f"    Classifiable: {ext_buy + ext_sell:,}/{ext_total:,} "
                  f"({100*(ext_buy+ext_sell)/ext_total:.1f}%)")
            print(f"    Still unclassifiable: {ext_zero:,} ({100*ext_zero/ext_total:.1f}%)")

    # ---- Kill gate assessment ----
    print("\n" + "=" * 70)
    print("KILL GATE ASSESSMENT")
    print("=" * 70)

    # Check: reversal_rate < 10% at ALL thresholds during spread >= 5
    kill_low_reversal = True
    kill_random = True

    for thresh in THRESHOLDS:
        for h_s in HORIZONS_S:
            r = results_wide.get((thresh, h_s))
            if r is not None and not np.isnan(r['reversal']):
                if r['reversal'] >= 10:
                    kill_low_reversal = False
                if abs(r['reversal'] - 50) > 5:  # outside 45-55% range
                    kill_random = False

    if kill_low_reversal:
        print("KILL: Reversal rate < 10% at all thresholds (spread >= 5). Direction A killed.")
    elif kill_random:
        print("KILL: Reversal rate ~50% at all thresholds — OBI has no predictive power. Direction A killed.")
    else:
        print("PASS: OBI shows non-trivial, non-random reversal rates. Direction A may proceed.")
        # Print summary of best reversal rates
        print("\nBest reversal opportunities (spread >= 5):")
        best = []
        for thresh in THRESHOLDS:
            for h_s in HORIZONS_S:
                r = results_wide.get((thresh, h_s))
                if r is not None and not np.isnan(r['reversal']):
                    best.append((r['reversal'], thresh, h_s, r['n_decisive']))
        best.sort(reverse=True)
        for rev, thresh, h_s, n in best[:5]:
            print(f"  |OBI|>{thresh}, {h_s}s horizon: reversal={rev:.1f}%, n={n:,}")

    return results_all, results_wide, results_tod


if __name__ == "__main__":
    run_analysis()
