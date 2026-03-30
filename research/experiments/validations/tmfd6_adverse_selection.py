"""
R18 Stage 2a: TMFD6 Adverse Selection Rate Measurement
Uses exported TSV from ClickHouse (March 2026 data).
"""

import numpy as np
from datetime import datetime, timezone, timedelta

SCALE = 1_000_000  # prices stored as x1000000, divide to get points
TW_TZ = timezone(timedelta(hours=8))


def load_data(path='/tmp/tmfd6_march.tsv'):
    """Load TSV: type, exch_ts, price_scaled, volume, bid1, ask1, bid1_vol, ask1_vol"""
    print("Loading data...")
    types = []
    exch_ts = []
    price = []
    volume = []
    bid1 = []
    ask1 = []
    bid1_vol = []
    ask1_vol = []

    with open(path, 'r') as f:
        for line in f:
            parts = line.strip().split('\t')
            types.append(parts[0])
            exch_ts.append(int(parts[1]))
            price.append(int(parts[2]))
            volume.append(int(parts[3]))
            bid1.append(int(parts[4]))
            ask1.append(int(parts[5]))
            bid1_vol.append(int(parts[6]))
            ask1_vol.append(int(parts[7]))

    n = len(types)
    print(f"  Loaded {n:,} events")

    # Convert to arrays
    is_tick = np.array([1 if t == 'Tick' else 0 for t in types], dtype=np.int8)
    exch_ts = np.array(exch_ts, dtype=np.int64)
    price = np.array(price, dtype=np.int64)
    volume = np.array(volume, dtype=np.int64)
    bid1 = np.array(bid1, dtype=np.int64)
    ask1 = np.array(ask1, dtype=np.int64)
    bid1_vol = np.array(bid1_vol, dtype=np.int64)
    ask1_vol = np.array(ask1_vol, dtype=np.int64)

    n_ticks = is_tick.sum()
    n_ba = n - n_ticks
    print(f"  BidAsk: {n_ba:,}, Tick: {n_ticks:,}")

    return is_tick, exch_ts, price, volume, bid1, ask1, bid1_vol, ask1_vol


def build_mid_series(is_tick, exch_ts, bid1, ask1):
    """Build a forward-filled mid-price series from BidAsk events."""
    n = len(is_tick)
    # For BidAsk events, compute mid; for Tick events, carry forward
    mid = np.zeros(n, dtype=np.float64)
    last_mid = 0.0
    last_bid = 0
    last_ask = 0

    ba_mask = is_tick == 0
    ba_indices = np.where(ba_mask)[0]

    # Compute mid for all BidAsk events
    ba_bids = bid1[ba_mask]
    ba_asks = ask1[ba_mask]
    ba_mids = (ba_bids.astype(np.float64) + ba_asks.astype(np.float64)) / 2.0

    # Forward-fill: for each event, find the latest BidAsk before it
    # ba_ts are sorted, so use searchsorted
    ba_ts = exch_ts[ba_mask]

    # For every event, find the most recent BidAsk
    idx = np.searchsorted(ba_ts, exch_ts, side='right') - 1
    valid = idx >= 0
    mid[valid] = ba_mids[idx[valid]]
    mid[~valid] = 0

    return mid, ba_ts, ba_bids, ba_asks, ba_mids


def main():
    is_tick, exch_ts, price, volume, bid1, ask1, bid1_vol, ask1_vol = load_data()

    # Build mid-price series
    print("\nBuilding mid-price series...")
    mid, ba_ts, ba_bids, ba_asks, ba_mids = build_mid_series(is_tick, exch_ts, bid1, ask1)

    # Get tick events
    tick_mask = is_tick == 1
    tick_indices = np.where(tick_mask)[0]
    tick_ts = exch_ts[tick_mask]
    tick_px = price[tick_mask]
    tick_vol = volume[tick_mask]
    n_ticks = len(tick_ts)
    print(f"Processing {n_ticks:,} trades...")

    # For each tick, find prevailing bid/ask
    ba_idx_for_tick = np.searchsorted(ba_ts, tick_ts, side='right') - 1
    valid_ticks = ba_idx_for_tick >= 0

    # Get prevailing quotes at each tick
    tick_bid = np.zeros(n_ticks, dtype=np.int64)
    tick_ask = np.zeros(n_ticks, dtype=np.int64)
    tick_bid[valid_ticks] = ba_bids[ba_idx_for_tick[valid_ticks]]
    tick_ask[valid_ticks] = ba_asks[ba_idx_for_tick[valid_ticks]]

    # Spread at each tick (in points, prices are x1000000)
    tick_spread_pts = (tick_ask - tick_bid).astype(np.float64) / SCALE
    tick_mid_at_trade = (tick_bid.astype(np.float64) + tick_ask.astype(np.float64)) / 2.0

    # Classify trade side: buy if price >= ask, sell if price <= bid
    side = np.zeros(n_ticks, dtype=np.int8)  # 1=buy, -1=sell
    side[tick_px >= tick_ask] = 1
    side[tick_px <= tick_bid] = -1
    # For ambiguous (between bid and ask), use tick rule vs mid
    ambiguous = side == 0
    mid_at = tick_mid_at_trade[ambiguous]
    px_at = tick_px[ambiguous].astype(np.float64)
    side_amb = np.where(px_at > mid_at, 1, -1).astype(np.int8)
    side[ambiguous] = side_amb

    # For each tick, find mid-price at 1s, 5s, 30s horizons
    horizons = {'1s': 1_000_000_000, '5s': 5_000_000_000, '30s': 30_000_000_000}

    # Pre-compute mid at horizons using searchsorted on ba_ts
    mid_at_horizons = {}
    for label, h_ns in horizons.items():
        target_ts = tick_ts + h_ns
        h_idx = np.searchsorted(ba_ts, target_ts, side='right') - 1
        h_valid = h_idx >= 0
        h_mid = np.full(n_ticks, np.nan, dtype=np.float64)
        h_mid[h_valid] = ba_mids[h_idx[h_valid]]
        mid_at_horizons[label] = h_mid

    # Compute mid-price change (signed: positive = price went up)
    mid_chg = {}
    for label in horizons:
        mid_chg[label] = (mid_at_horizons[label] - tick_mid_at_trade) / SCALE  # in points

    # Adverse selection: mid moved in taker's direction
    # For buy (side=1): adverse if mid went UP (maker sold, price rose)
    # For sell (side=-1): adverse if mid went DOWN (maker bought, price fell)
    adverse = {}
    for label in horizons:
        chg = mid_chg[label]
        adv = np.zeros(n_ticks, dtype=np.int8)
        adv[(side == 1) & (chg > 0)] = 1
        adv[(side == -1) & (chg < 0)] = 1
        adverse[label] = adv

    # Maker-perspective signed change: positive = bad for maker
    maker_chg = {}
    for label in horizons:
        mc = mid_chg[label].copy()
        mc[side == -1] *= -1  # flip for sells
        maker_chg[label] = mc

    # Filter: valid ticks (have prevailing quote, not nan at horizons)
    valid = valid_ticks & np.isfinite(mid_chg['5s']) & (tick_spread_pts > 0)

    # Hour of day (Taiwan time)
    tick_hour_min = np.zeros(n_ticks, dtype=np.int32)
    for i in range(n_ticks):
        if not valid[i]:
            continue
        dt = datetime.fromtimestamp(tick_ts[i] / 1e9, tz=TW_TZ)
        tick_hour_min[i] = dt.hour * 100 + dt.minute

    print(f"Valid trades for analysis: {valid.sum():,}")

    # ===== ANALYSIS =====
    print("\n" + "=" * 90)
    print("TMFD6 ADVERSE SELECTION ANALYSIS — March 2026 (8 trading days)")
    print("=" * 90)

    spread_buckets = [
        ('1-3', 1, 3),
        ('4', 4, 4),
        ('5-6', 5, 6),
        ('7-10', 7, 10),
        ('11-20', 11, 20),
        ('20+', 20, 99999),
    ]

    tod_buckets = [
        ('open_0845-0915', 845, 915),
        ('morn_0915-1000', 915, 1000),
        ('mid_1000-1200', 1000, 1200),
        ('aftn_1200-1300', 1200, 1300),
        ('close_1300-1345', 1300, 1345),
    ]

    # --- Table 1: Adverse selection by spread bucket ---
    print("\n--- Table 1: Adverse Selection Rate by Spread Bucket ---")
    header = f"{'Bucket':<10} {'N':>10}"
    for label in horizons:
        header += f"  {'Adv_'+label:>10} {'AvgChg_'+label:>12}"
    print(header)
    print("-" * len(header))

    for bname, bmin, bmax in spread_buckets:
        mask = valid & (tick_spread_pts >= bmin) & (tick_spread_pts <= bmax)
        n = mask.sum()
        if n == 0:
            continue
        row = f"{bname:<10} {n:>10}"
        for label in horizons:
            adv_rate = adverse[label][mask].sum() / mask.sum()
            avg_mc = np.nanmean(maker_chg[label][mask])
            row += f"  {adv_rate:>9.1%} {avg_mc:>+11.3f}pt"
        print(row)

    # --- Overall spread >= 5 ---
    wide_mask = valid & (tick_spread_pts >= 5)
    n_wide = wide_mask.sum()
    print(f"\n{'>=5 ALL':<10} {n_wide:>10}", end='')
    for label in horizons:
        adv_rate = adverse[label][wide_mask].sum() / n_wide if n_wide > 0 else 0
        avg_mc = np.nanmean(maker_chg[label][wide_mask]) if n_wide > 0 else 0
        print(f"  {adv_rate:>9.1%} {avg_mc:>+11.3f}pt", end='')
    print()

    # --- Table 2: Adverse selection by time-of-day (spread >= 5 only) ---
    print("\n--- Table 2: Adverse Selection by Time-of-Day (spread >= 5) ---")
    header2 = f"{'TOD':<20} {'N':>8}"
    for label in horizons:
        header2 += f"  {'Adv_'+label:>10} {'AvgChg_'+label:>12}"
    print(header2)
    print("-" * len(header2))

    for tname, tmin, tmax in tod_buckets:
        mask = wide_mask & (tick_hour_min >= tmin) & (tick_hour_min < tmax)
        n = mask.sum()
        if n == 0:
            continue
        row = f"{tname:<20} {n:>8}"
        for label in horizons:
            adv_rate = adverse[label][mask].sum() / n if n > 0 else 0
            avg_mc = np.nanmean(maker_chg[label][mask]) if n > 0 else 0
            row += f"  {adv_rate:>9.1%} {avg_mc:>+11.3f}pt"
        print(row)

    # --- Table 3: Spread vs Volatility ---
    print("\n--- Table 3: Spread-Volatility Correlation ---")
    # 1-minute bucketing
    minute_keys = tick_ts[valid] // 60_000_000_000
    unique_mins = np.unique(minute_keys)
    spreads_1m = []
    vols_1m = []
    for mk in unique_mins:
        m_mask = valid & (tick_ts // 60_000_000_000 == mk)
        if m_mask.sum() < 2:
            continue
        avg_s = tick_spread_pts[m_mask].mean()
        mids = tick_mid_at_trade[m_mask] / SCALE
        rets = np.diff(mids)
        vol = np.std(rets) if len(rets) > 0 else 0
        spreads_1m.append(avg_s)
        vols_1m.append(vol)

    spreads_1m = np.array(spreads_1m)
    vols_1m = np.array(vols_1m)
    if len(spreads_1m) > 10:
        corr = np.corrcoef(spreads_1m, vols_1m)[0, 1]
        print(f"  1-min spread vs mid-price volatility correlation: {corr:.4f}")

        for bname, bmin, bmax in spread_buckets:
            m = (spreads_1m >= bmin) & (spreads_1m <= bmax)
            if m.sum() > 0:
                print(f"  Spread {bname:<6}: mean_vol={vols_1m[m].mean():.4f}pt  n_minutes={m.sum()}")

    # --- KILL GATE ---
    print("\n" + "=" * 90)
    print("KILL GATE EVALUATION")
    print("=" * 90)

    # Gate 1: Overall adverse rate at spread >= 5 (5s horizon)
    if n_wide > 0:
        overall_adv_5s = adverse['5s'][wide_mask].sum() / n_wide
        print(f"\n  [GATE 1] Adverse rate at spread >= 5 (5s): {overall_adv_5s:.1%}")
        if overall_adv_5s > 0.60:
            print(f"  >>> KILL: {overall_adv_5s:.1%} > 60% threshold <<<")
        else:
            print(f"  >>> PASS: {overall_adv_5s:.1%} <= 60% threshold <<<")

    # Gate 2: Does adverse rate increase with spread?
    print(f"\n  [GATE 2] Adverse rate monotonicity check (5s):")
    rates = []
    for bname, bmin, bmax in [('5-6', 5, 6), ('7-10', 7, 10), ('11-20', 11, 20), ('20+', 20, 99999)]:
        mask = valid & (tick_spread_pts >= bmin) & (tick_spread_pts <= bmax)
        n = mask.sum()
        if n > 0:
            r = adverse['5s'][mask].sum() / n
            rates.append((bname, r, n))
            print(f"    Spread {bname:<6}: adv_rate={r:.1%}  (n={n:,})")

    if len(rates) >= 2:
        increasing = all(rates[i][1] <= rates[i+1][1] for i in range(len(rates)-1))
        if increasing:
            print(f"  >>> WARN: Adverse rate INCREASES with spread width <<<")
        else:
            print(f"  >>> OK: Adverse rate does NOT monotonically increase <<<")

    # Additional: average maker P&L per trade at wide spreads
    print(f"\n  [INFO] Average maker adverse mid-change at wide spreads:")
    for label in horizons:
        if n_wide > 0:
            avg = np.nanmean(maker_chg[label][wide_mask])
            med = np.nanmedian(maker_chg[label][wide_mask])
            print(f"    {label}: mean={avg:+.3f}pt, median={med:+.3f}pt")

    # Effective maker edge = spread_capture / 2 - adverse_selection
    # If quoting at bid and ask, capture half spread per leg
    if n_wide > 0:
        avg_spread_wide = tick_spread_pts[wide_mask].mean()
        avg_adv_5s = np.nanmean(maker_chg['5s'][wide_mask])
        half_spread = avg_spread_wide / 2
        fee_per_leg = 2.0  # 20 NTD / 10 NTD per pt = 2 pts per side
        net_edge = half_spread - avg_adv_5s - fee_per_leg
        print(f"\n  [INFO] Rough maker edge estimate (per leg, spread >= 5):")
        print(f"    Avg half-spread capture: {half_spread:+.2f} pts")
        print(f"    Avg adverse selection (5s): {avg_adv_5s:+.3f} pts")
        print(f"    Fee per leg: {fee_per_leg:.1f} pts")
        print(f"    Net edge per leg: {net_edge:+.3f} pts")
        print(f"    Net edge per roundtrip: {2*net_edge:+.3f} pts ({2*net_edge*10:.1f} NTD)")


if __name__ == '__main__':
    main()
