"""
TMFD6 Adverse Selection Measurement — R18 Stage 2a (BLOCKER-E1)

Data: TMFD6_all_l1.npy — 7.75M L1 quote rows, ~20 trading days.
Volume field is always 0 (pure quote data), so we infer trades from
mid-price changes as a proxy for aggressive order flow.

Trade proxy method:
  - mid_price increases -> buy-initiated (buyer lifted the ask)
  - mid_price decreases -> sell-initiated (seller hit the bid)

Limitation: This captures price-moving events only. Non-price-moving trades
(passive fills) are invisible. Our sample is BIASED toward informed flow,
so adverse selection rates measured here are an UPPER BOUND.

Adverse selection from MM perspective:
  - Buy-initiated: MM sold. Adverse if mid goes UP further (informed buyer).
  - Sell-initiated: MM bought. Adverse if mid goes DOWN further (informed seller).

TAIFEX sessions (all times UTC+8):
  - Day session:   08:45 - 13:45
  - Night session:  15:00 - 05:00 (next day)
Forward lookups must not cross session boundaries.
"""

import numpy as np
from datetime import datetime, timezone, timedelta
import sys

# ── Config ──────────────────────────────────────────────────────────────
DATA_PATH = "research/data/raw/tmfd6/TMFD6_all_l1.npy"
HORIZONS_S = [1, 5, 30]
SPREAD_BUCKETS = [(5, 9), (10, 19), (20, 39), (40, float("inf"))]
TZ_OFFSET_S = 8 * 3600  # UTC+8

# Time-of-day bins (local time, minutes since midnight)
TOD_BINS = {
    "opening_0845_0915": (8 * 60 + 45, 9 * 60 + 15),
    "midday_0915_1200": (9 * 60 + 15, 12 * 60),
    "closing_1200_1345": (12 * 60, 13 * 60 + 45),
    "night_1500_2100": (15 * 60, 21 * 60),
    "night_2100_0500": (21 * 60, 29 * 60),  # 29*60 = 05:00 next day
}


def ts_to_local_s(ts_ns):
    """Convert nanosecond timestamps to local (UTC+8) seconds."""
    return ts_ns / 1e9 + TZ_OFFSET_S


def local_minute_of_day(local_s):
    """Minutes since midnight in local time."""
    return ((local_s % 86400) / 60).astype(np.int32)


def local_hour_of_day(local_s):
    """Hour in local time."""
    return ((local_s % 86400) / 3600).astype(np.int32)


def find_session_boundaries(ts_ns):
    """
    Find contiguous session blocks. A session break is defined as a gap
    of > 30 minutes between consecutive timestamps.
    """
    ts_s = ts_ns / 1e9
    gaps = np.diff(ts_s)
    break_idx = np.where(gaps > 1800)[0]  # 30 min gap

    sessions = []
    prev = 0
    for bi in break_idx:
        sessions.append((prev, bi + 1))
        prev = bi + 1
    sessions.append((prev, len(ts_ns)))

    return sessions


def compute_forward_mid_sessions(mid, ts_ns, trade_indices, horizon_s, sessions):
    """
    For each trade index, find mid_price at trade_time + horizon_s.
    Returns NaN if forward time crosses session boundary.
    """
    result = np.full(len(trade_indices), np.nan)
    horizon_ns = int(horizon_s * 1e9)

    for ss, se in sessions:
        # Which trades fall in this session?
        mask = (trade_indices >= ss) & (trade_indices < se)
        if not mask.any():
            continue

        session_trade_idx = trade_indices[mask]
        session_ts = ts_ns[ss:se]
        session_mid = mid[ss:se]

        # Target timestamps
        trade_ts = ts_ns[session_trade_idx]
        target_ts = trade_ts + horizon_ns

        # Must stay within session
        session_end_ts = session_ts[-1]

        # Binary search for target within session
        local_idx = session_trade_idx - ss
        future_pos = np.searchsorted(session_ts, target_ts)

        # Valid only if within session bounds
        valid = future_pos < len(session_ts)
        local_result = np.full(len(session_trade_idx), np.nan)
        if valid.any():
            clamped = np.clip(future_pos[valid], 0, len(session_ts) - 1)
            local_result[valid] = session_mid[clamped]

        result[mask] = local_result

    return result


def compute_1min_vol_sessions(mid, ts_ns, trade_indices, sessions):
    """
    For each trade, compute 1-minute realized volatility using subsampled
    mid-price returns in the 60s window following the trade.
    """
    result = np.full(len(trade_indices), np.nan)
    window_ns = int(60 * 1e9)

    for ss, se in sessions:
        mask = (trade_indices >= ss) & (trade_indices < se)
        if not mask.any():
            continue

        session_trade_idx = trade_indices[mask]
        session_ts = ts_ns[ss:se]
        session_mid = mid[ss:se]

        for j_local, ti in enumerate(session_trade_idx):
            sp = ti - ss
            end_ts = ts_ns[ti] + window_ns
            ep = np.searchsorted(session_ts, end_ts)
            ep = min(ep, len(session_ts))
            n_rows = ep - sp
            if n_rows < 10:
                continue
            step = max(1, n_rows // 20)
            mids = session_mid[sp:ep:step]
            if len(mids) < 3:
                continue
            rets = np.diff(mids) / mids[:-1]
            j_global = np.where(mask)[0][j_local]
            result[j_global] = np.std(rets) * np.sqrt(len(rets))

    return result


def main():
    print("=" * 70)
    print("TMFD6 ADVERSE SELECTION ANALYSIS — R18 Stage 2a")
    print("=" * 70)

    # ── Load ──
    print(f"\nLoading {DATA_PATH} ...")
    d = np.load(DATA_PATH, mmap_mode="r")
    print(f"  Rows: {len(d):,}")

    ts_ns = d["local_ts"]
    mid = d["mid_price"]
    bid = d["bid_px"]
    ask = d["ask_px"]
    spread_pts = ask - bid

    # ── Session boundaries ──
    print("\nFinding session boundaries (gap > 30min)...")
    sessions = find_session_boundaries(ts_ns)
    print(f"  Sessions found: {len(sessions)}")
    for i, (ss, se) in enumerate(sessions[:5]):
        t0 = datetime.fromtimestamp(ts_ns[ss] / 1e9, tz=timezone.utc)
        t1 = datetime.fromtimestamp(ts_ns[se - 1] / 1e9, tz=timezone.utc)
        dur_h = (ts_ns[se - 1] - ts_ns[ss]) / 1e9 / 3600
        print(f"    S{i}: {t0.strftime('%Y-%m-%d %H:%M')} UTC - "
              f"{t1.strftime('%H:%M')} UTC ({dur_h:.1f}h, {se-ss:,} rows)")
    if len(sessions) > 5:
        print(f"    ... and {len(sessions) - 5} more")

    # ── Trade proxies ──
    print("\nIdentifying trade proxies from mid-price changes...")
    mid_diff = np.diff(mid)
    buy_mask = mid_diff > 0
    sell_mask = mid_diff < 0

    # Index = row of new state (after change)
    buy_idx = np.where(buy_mask)[0] + 1
    sell_idx = np.where(sell_mask)[0] + 1

    buy_spread = spread_pts[buy_idx - 1]
    sell_spread = spread_pts[sell_idx - 1]

    all_idx = np.concatenate([buy_idx, sell_idx])
    all_dir = np.concatenate([np.ones(len(buy_idx), dtype=np.int8),
                               -np.ones(len(sell_idx), dtype=np.int8)])
    all_spread = np.concatenate([buy_spread, sell_spread])

    order = np.argsort(all_idx)
    all_idx = all_idx[order]
    all_dir = all_dir[order]
    all_spread = all_spread[order]

    print(f"  Total trade proxies: {len(all_idx):,}")
    print(f"  Buy-initiated: {(all_dir == 1).sum():,}")
    print(f"  Sell-initiated: {(all_dir == -1).sum():,}")

    # Wide spread filter
    wide_mask = all_spread >= 5
    w_idx = all_idx[wide_mask]
    w_dir = all_dir[wide_mask]
    w_spread = all_spread[wide_mask]
    w_mid = mid[w_idx]
    w_ts = ts_ns[w_idx]

    print(f"\n  Wide-spread (>=5 pts) trade proxies: {len(w_idx):,}")
    print(f"    Buy: {(w_dir == 1).sum():,}, Sell: {(w_dir == -1).sum():,}")

    # ── Forward mid-price at each horizon ──
    forward_mids = {}
    for h in HORIZONS_S:
        print(f"\nComputing +{h}s forward mid-price...")
        forward_mids[h] = compute_forward_mid_sessions(mid, ts_ns, w_idx, h, sessions)
        valid_n = (~np.isnan(forward_mids[h])).sum()
        print(f"  Valid: {valid_n:,} / {len(w_idx):,} ({valid_n/len(w_idx)*100:.1f}%)")

    # ── Adverse selection classification ──
    print("\n" + "=" * 70)
    print("ADVERSE SELECTION RESULTS")
    print("=" * 70)

    results = {}
    for h in HORIZONS_S:
        fwd = forward_mids[h]
        valid = ~np.isnan(fwd)
        delta = fwd - w_mid

        is_buy = w_dir == 1
        is_sell = w_dir == -1

        # Adverse from MM perspective
        adverse = np.zeros(len(w_idx), dtype=bool)
        adverse[is_buy & valid] = delta[is_buy & valid] > 0
        adverse[is_sell & valid] = delta[is_sell & valid] < 0

        magnitude = np.abs(delta)

        results[h] = {
            "valid": valid,
            "adverse": adverse,
            "delta": delta,
            "magnitude": magnitude,
        }

    # ── Overall rates ──
    print(f"\n{'':─<70}")
    print("Overall (spread >= 5 pts)")
    print(f"{'':─<70}")
    print(f"{'Horizon':>10}  {'N valid':>10}  {'Adverse%':>10}  {'Avg Adv Mag':>12}  {'Avg All Mag':>12}")
    for h in HORIZONS_S:
        r = results[h]
        v = r["valid"]
        a = r["adverse"] & v
        n_valid = v.sum()
        n_adv = a.sum()
        rate = n_adv / n_valid * 100 if n_valid > 0 else 0
        avg_adv_mag = r["magnitude"][a].mean() if a.sum() > 0 else 0
        avg_all_mag = r["magnitude"][v].mean() if n_valid > 0 else 0
        print(f"  +{h:>3}s    {n_valid:>10,}  {rate:>9.1f}%  {avg_adv_mag:>10.2f} pts  {avg_all_mag:>10.2f} pts")

    # ── By spread bucket ──
    print(f"\n{'':─<70}")
    print("By Spread Bucket")
    print(f"{'':─<70}")
    bucket_rates_5s = []
    for lo, hi in SPREAD_BUCKETS:
        hi_label = f"{hi:.0f}" if hi != float("inf") else "+"
        bucket_label = f"[{lo}-{hi_label}]"
        if hi != float("inf"):
            bucket_mask = (w_spread >= lo) & (w_spread <= hi)
        else:
            bucket_mask = w_spread >= lo

        print(f"\n  Spread {bucket_label} pts (N={bucket_mask.sum():,} trade proxies)")
        print(f"  {'Horizon':>10}  {'N valid':>10}  {'Adverse%':>10}  {'Avg Adv Mag':>12}  {'Avg All delta':>14}")
        for h in HORIZONS_S:
            r = results[h]
            v = r["valid"] & bucket_mask
            a = r["adverse"] & v
            n_valid = v.sum()
            n_adv = a.sum()
            rate = n_adv / n_valid * 100 if n_valid > 0 else 0
            avg_adv_mag = r["magnitude"][a].mean() if a.sum() > 0 else 0
            # Signed average delta (positive = adverse for buys, negative = adverse for sells)
            avg_delta = r["delta"][v].mean() if n_valid > 0 else 0
            print(f"    +{h:>3}s    {n_valid:>10,}  {rate:>9.1f}%  {avg_adv_mag:>10.2f} pts  {avg_delta:>+12.3f} pts")
            if h == 5:
                bucket_rates_5s.append((bucket_label, rate, n_valid))

    # ── By time of day ──
    print(f"\n{'':─<70}")
    print("By Time of Day (local UTC+8)")
    print(f"{'':─<70}")
    local_s = ts_to_local_s(w_ts)
    local_min = local_minute_of_day(local_s)

    for tod_name, (start_min, end_min) in TOD_BINS.items():
        if end_min > 24 * 60:
            # Wraps midnight: e.g. 21:00-05:00 = (min>=21*60) | (min<5*60)
            wrap_start = end_min - 24 * 60
            tod_mask = (local_min >= start_min) | (local_min < wrap_start)
        else:
            tod_mask = (local_min >= start_min) & (local_min < end_min)

        n_tod = tod_mask.sum()
        if n_tod == 0:
            continue

        print(f"\n  {tod_name} (N={n_tod:,} trade proxies)")
        print(f"  {'Horizon':>10}  {'N valid':>10}  {'Adverse%':>10}  {'Avg Adv Mag':>12}")
        for h in HORIZONS_S:
            r = results[h]
            v = r["valid"] & tod_mask
            a = r["adverse"] & v
            n_valid = v.sum()
            n_adv = a.sum()
            rate = n_adv / n_valid * 100 if n_valid > 0 else 0
            avg_adv_mag = r["magnitude"][a].mean() if a.sum() > 0 else 0
            print(f"    +{h:>3}s    {n_valid:>10,}  {rate:>9.1f}%  {avg_adv_mag:>10.2f} pts")

    # ── Day session only analysis ──
    print(f"\n{'':─<70}")
    print("Day Session Only (08:45-13:45 local)")
    print(f"{'':─<70}")
    day_mask = (local_min >= 8 * 60 + 45) & (local_min < 13 * 60 + 45)
    print(f"  Day session trade proxies: {day_mask.sum():,} / {len(w_idx):,}")

    print(f"  {'Horizon':>10}  {'N valid':>10}  {'Adverse%':>10}  {'Avg Adv Mag':>12}")
    for h in HORIZONS_S:
        r = results[h]
        v = r["valid"] & day_mask
        a = r["adverse"] & v
        n_valid = v.sum()
        n_adv = a.sum()
        rate = n_adv / n_valid * 100 if n_valid > 0 else 0
        avg_adv_mag = r["magnitude"][a].mean() if a.sum() > 0 else 0
        print(f"    +{h:>3}s    {n_valid:>10,}  {rate:>9.1f}%  {avg_adv_mag:>10.2f} pts")

    # ── Spread-Volatility Correlation ──
    print(f"\n{'':─<70}")
    print("Spread vs 1-min Realized Volatility Correlation")
    print(f"{'':─<70}")

    # Subsample for speed
    sample_step = 10
    s_idx = w_idx[::sample_step]
    s_spread = w_spread[::sample_step]

    print(f"Computing 1-min realized vol ({len(s_idx):,} sampled trades)...")
    vol_1m = compute_1min_vol_sessions(mid, ts_ns, s_idx, sessions)
    valid_vol = ~np.isnan(vol_1m)
    n_valid_vol = valid_vol.sum()

    if n_valid_vol > 100:
        corr = np.corrcoef(s_spread[valid_vol], vol_1m[valid_vol])[0, 1]
        print(f"  Sample size: {n_valid_vol:,}")
        print(f"  Pearson correlation (spread vs 1m vol): {corr:.4f}")
        if corr > 0.1:
            print("  WARNING: POSITIVE correlation — wider spread -> higher subsequent vol")
            print("  Interpretation: wide spread is partly information-driven")
        elif corr < -0.1:
            print("  Negative correlation — wide spread is liquidity-driven, not informational")
        else:
            print("  Weak correlation — spread width largely independent of subsequent volatility")

        print("\n  Spread bucket -> mean 1m realized vol:")
        for lo, hi in SPREAD_BUCKETS:
            hi_label = f"{hi:.0f}" if hi != float("inf") else "+"
            if hi != float("inf"):
                bmask = (s_spread >= lo) & (s_spread <= hi) & valid_vol
            else:
                bmask = (s_spread >= lo) & valid_vol
            if bmask.sum() > 0:
                print(f"    [{lo}-{hi_label}]: vol={vol_1m[bmask].mean():.6f} "
                      f"(n={bmask.sum():,})")

    # ── Favorable fill analysis ──
    print(f"\n{'':─<70}")
    print("Favorable Fill Analysis (MM profit potential)")
    print(f"{'':─<70}")
    print("If MM captures half-spread minus adverse movement:")
    for h in HORIZONS_S:
        r = results[h]
        v = r["valid"]
        half_spread = w_spread[v] / 2.0
        signed_delta = r["delta"][v].copy()
        dirs = w_dir[v]
        # From MM perspective: MM sold at ask (buy-initiated) -> P&L = -delta_mid
        # MM bought at bid (sell-initiated) -> P&L = +delta_mid
        mm_pnl_per_trade = np.where(dirs == 1, -signed_delta, signed_delta)
        # Add half-spread capture (MM earns half the spread by posting)
        mm_gross = half_spread + mm_pnl_per_trade
        # Subtract half RT cost (each leg = 2 pts)
        mm_net = mm_gross - 2.0  # one-leg cost

        print(f"\n  +{h}s horizon (n={v.sum():,}):")
        print(f"    Avg half-spread earned:  {half_spread.mean():+.2f} pts")
        print(f"    Avg adverse movement:    {mm_pnl_per_trade.mean():+.2f} pts")
        print(f"    Avg gross P&L/fill:      {mm_gross.mean():+.2f} pts")
        print(f"    Avg net P&L/fill:        {mm_net.mean():+.2f} pts (after 2pt one-leg cost)")
        print(f"    % fills with net > 0:    {(mm_net > 0).mean()*100:.1f}%")

    # ── Kill Gate Assessment ──
    print("\n" + "=" * 70)
    print("KILL GATE ASSESSMENT")
    print("=" * 70)

    r5 = results[5]
    v5 = r5["valid"]
    a5 = r5["adverse"] & v5
    overall_rate_5s = a5.sum() / v5.sum() * 100

    print(f"\n  Overall adverse rate at +5s (spread>=5): {overall_rate_5s:.1f}%")

    if overall_rate_5s > 60:
        print("  KILL: Adverse rate > 60% — wide-spread fills are predominantly informed.")
        verdict = "KILL"
    elif overall_rate_5s > 50:
        print("  WARNING: Adverse rate > 50% — marginal, needs further investigation.")
        verdict = "WARNING"
    else:
        print("  PASS: Adverse rate <= 50% — wide-spread fills are NOT predominantly adverse.")
        verdict = "PASS"

    # Monotonicity check on 5s rates
    rates_only = [r for _, r, n in bucket_rates_5s if n > 100]
    if len(rates_only) >= 3:
        increasing = all(rates_only[i] <= rates_only[i + 1]
                         for i in range(len(rates_only) - 1))
        if increasing:
            print("  WARNING: Adverse rate INCREASES with spread width — wider != more edge.")
        else:
            print("  OK: Adverse rate does NOT monotonically increase with spread width.")
            # Check if it DECREASES (good sign — wider spread = more edge)
            decreasing = all(rates_only[i] >= rates_only[i + 1]
                             for i in range(len(rates_only) - 1))
            if decreasing:
                print("  FAVORABLE: Adverse rate DECREASES with spread width — wider = more edge!")

    # Spread-vol correlation verdict
    if n_valid_vol > 100:
        if corr > 0.3:
            print(f"  WARNING: Strong positive spread-vol correlation ({corr:.3f}) — "
                  "wide spread is information-driven.")
        elif corr > 0.1:
            print(f"  CAUTION: Moderate positive spread-vol correlation ({corr:.3f}).")
        else:
            print(f"  OK: Spread-vol correlation is weak ({corr:.3f}).")

    print(f"\n  FINAL VERDICT: {verdict}")

    # ── Summary table ──
    print("\n" + "=" * 70)
    print("SUMMARY TABLE")
    print("=" * 70)
    print(f"\n{'Metric':<40} {'Value':>15}")
    print("-" * 57)
    print(f"{'Total L1 rows':<40} {len(d):>15,}")
    print(f"{'Total trade proxies':<40} {len(all_idx):>15,}")
    print(f"{'Wide-spread trade proxies (>=5pts)':<40} {len(w_idx):>15,}")
    for h in HORIZONS_S:
        r = results[h]
        v = r["valid"]
        a = r["adverse"] & v
        rate = a.sum() / v.sum() * 100
        mag = r["magnitude"][a].mean()
        print(f"{'Adverse rate +' + str(h) + 's':<40} {rate:>14.1f}%")
        print(f"{'Avg adverse magnitude +' + str(h) + 's':<40} {mag:>13.2f} pts")
    if n_valid_vol > 100:
        print(f"{'Spread-vol correlation':<40} {corr:>+14.4f}")
    print(f"{'Kill gate verdict':<40} {verdict:>15}")


if __name__ == "__main__":
    main()
