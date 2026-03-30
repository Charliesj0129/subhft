"""
Direction C: Cross-Session Intraday Patterns on TMFD6 (Mini-TAIEX Futures)
Analyzes: C1 Opening Gap, C2 Time-of-Day, C3 First/Last Hour, C4 Day-of-Week
"""
import subprocess
import io
import numpy as np
import pandas as pd
from collections import defaultdict

# ── Constants ──
RT_COST_PTS = 4.0   # Round-trip cost in points
RT_COST_BPS = 1.33  # Round-trip cost in bps
SCALE = 10000.0

# ── Near-month contract mapping ──
# Based on tick counts: TMFB6 near-month Jan 27 - Feb 23, TMFC6 Feb 26 - Mar 18, TMFD6 Mar 19+
# Also include TMFD6 standalone dates (Feb 24, Feb 25 partial)

def run_ch(query: str) -> pd.DataFrame:
    """Run ClickHouse query and return DataFrame."""
    cmd = ['docker', 'exec', 'clickhouse', 'clickhouse-client',
           '--query', query, '--format', 'TabSeparatedWithNames']
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"CH ERROR: {result.stderr[:500]}")
        return pd.DataFrame()
    return pd.read_csv(io.StringIO(result.stdout), sep='\t')


def load_tick_data() -> pd.DataFrame:
    """Load all TMF tick data with near-month selection."""
    query = """
    SELECT
        symbol,
        exch_ts,
        price_scaled,
        volume,
        toDateTime64(exch_ts/1000000000, 3) + INTERVAL 8 HOUR as tw_time
    FROM hft.market_data
    WHERE symbol IN ('TMFB6','TMFC6','TMFD6')
      AND type = 'Tick'
      AND price_scaled > 0
    ORDER BY exch_ts
    """
    print("Loading tick data from ClickHouse...")
    df = run_ch(query)
    if df.empty:
        raise RuntimeError("No data returned")

    df['tw_time'] = pd.to_datetime(df['tw_time'])
    df['price'] = df['price_scaled'].astype(float) / SCALE
    df['tw_date'] = df['tw_time'].dt.date
    df['tw_hour'] = df['tw_time'].dt.hour
    df['tw_minute'] = df['tw_time'].dt.minute

    print(f"Loaded {len(df):,} ticks")
    return df


def select_near_month(df: pd.DataFrame) -> pd.DataFrame:
    """For each date, keep only the contract with the most ticks (= near-month)."""
    counts = df.groupby(['tw_date', 'symbol']).size().reset_index(name='n')
    idx = counts.groupby('tw_date')['n'].idxmax()
    best = counts.loc[idx][['tw_date', 'symbol']].rename(columns={'symbol': 'near_symbol'})

    df = df.merge(best, on='tw_date')
    df = df[df['symbol'] == df['near_symbol']].drop(columns=['near_symbol'])
    print(f"After near-month selection: {len(df):,} ticks across {df['tw_date'].nunique()} dates")
    return df


def classify_session(row):
    """Classify tick into day/night session based on Taiwan time."""
    h, m = row['tw_hour'], row['tw_minute']
    hm = h * 100 + m
    if 845 <= hm <= 1345:
        return 'day'
    elif hm >= 1500 or hm < 500:
        return 'night'
    else:
        return 'other'  # between sessions


def compute_session_boundaries(df: pd.DataFrame) -> pd.DataFrame:
    """Compute OHLC for each session on each date."""
    df['session'] = df.apply(classify_session, axis=1)
    df = df[df['session'].isin(['day', 'night'])]

    # For night sessions spanning midnight, assign to the date of the 15:00 start
    # Night session starting at 15:00 on date D belongs to "trading date D"
    # Day session on date D+1 belongs to "trading date D+1"

    results = []
    for (date, session), grp in df.groupby(['tw_date', 'session']):
        if len(grp) < 10:
            continue
        results.append({
            'tw_date': date,
            'session': session,
            'open': grp['price'].iloc[0],
            'close': grp['price'].iloc[-1],
            'high': grp['price'].max(),
            'low': grp['price'].min(),
            'mid_open': grp['price'].iloc[0],
            'mid_close': grp['price'].iloc[-1],
            'ticks': len(grp),
            'first_ts': grp['tw_time'].iloc[0],
            'last_ts': grp['tw_time'].iloc[-1],
        })

    return pd.DataFrame(results)


def analyze_c1_opening_gap(df: pd.DataFrame, sessions: pd.DataFrame):
    """C1: Opening Gap Signal - gap between night close and next day open."""
    print("\n" + "="*80)
    print("C1: OPENING GAP SIGNAL")
    print("="*80)

    # Get night session closes and day session opens
    night = sessions[sessions['session'] == 'night'].copy()
    day = sessions[sessions['session'] == 'day'].copy()

    # Sort by date
    night = night.sort_values('tw_date')
    day = day.sort_values('tw_date')

    # For each day session, find the preceding night session
    # Night session on date D ends early morning D+1, day session starts D+1 08:45
    # So night session's tw_date (based on 15:00 start) = day session tw_date - 1 (roughly)
    # Actually night ticks after midnight have tw_date = next day
    # Let's match: night sessions ending before 06:00 on date D match day sessions starting 08:45 on date D

    gaps = []
    for _, d in day.iterrows():
        day_date = d['tw_date']
        # Find night session closing on the same calendar date (early morning)
        # or the previous calendar date (if night session is all before midnight)
        matching_nights = night[
            (night['tw_date'] == day_date) |
            (night['tw_date'] == pd.Timestamp(day_date) - pd.Timedelta(days=1))
        ]
        # Pick the one closest before the day open
        matching_nights = matching_nights[matching_nights['last_ts'] < d['first_ts']]
        if len(matching_nights) == 0:
            continue
        n = matching_nights.iloc[-1]

        gap_pts = d['open'] - n['close']
        gap_bps = gap_pts / n['close'] * 10000

        # Forward returns from day session
        day_ticks = df[(df['tw_date'] == day_date) & (df['session'] == 'day')]
        if len(day_ticks) < 100:
            continue

        day_open_price = day_ticks['price'].iloc[0]

        # Returns at various horizons (30 min, 60 min, 120 min from open)
        open_ts = day_ticks['tw_time'].iloc[0]
        ret_30 = ret_60 = ret_120 = ret_full = np.nan

        for horizon_min, label in [(30, '30'), (60, '60'), (120, '120')]:
            target_ts = open_ts + pd.Timedelta(minutes=horizon_min)
            future_ticks = day_ticks[day_ticks['tw_time'] >= target_ts]
            if len(future_ticks) > 0:
                ret = (future_ticks['price'].iloc[0] - day_open_price)
                if label == '30': ret_30 = ret
                elif label == '60': ret_60 = ret
                elif label == '120': ret_120 = ret

        # Full session return
        ret_full = day_ticks['price'].iloc[-1] - day_open_price

        gaps.append({
            'date': day_date,
            'night_close': n['close'],
            'day_open': d['open'],
            'gap_pts': gap_pts,
            'gap_bps': gap_bps,
            'ret_30min': ret_30,
            'ret_60min': ret_60,
            'ret_120min': ret_120,
            'ret_full': ret_full,
        })

    gap_df = pd.DataFrame(gaps)
    if gap_df.empty:
        print("No gap data found!")
        return gap_df

    print(f"\nGap Statistics ({len(gap_df)} observations):")
    print(f"  Mean gap: {gap_df['gap_pts'].mean():.1f} pts ({gap_df['gap_bps'].mean():.2f} bps)")
    print(f"  Std gap:  {gap_df['gap_pts'].std():.1f} pts")
    print(f"  Min/Max:  {gap_df['gap_pts'].min():.0f} / {gap_df['gap_pts'].max():.0f} pts")

    # IC: correlation of gap direction/size with forward returns
    print("\nIC (gap_bps vs forward returns):")
    for col in ['ret_30min', 'ret_60min', 'ret_120min', 'ret_full']:
        valid = gap_df[['gap_bps', col]].dropna()
        if len(valid) < 5:
            print(f"  {col}: insufficient data")
            continue
        ic = valid['gap_bps'].corr(valid[col])
        # Also compute IC of gap direction (sign)
        gap_dir = np.sign(valid['gap_bps'])
        ic_dir = gap_dir.corr(valid[col])
        print(f"  {col}: IC={ic:+.4f} (size), IC={ic_dir:+.4f} (direction)")

    # Gap-fade vs gap-and-go
    print("\nGap-Fade (mean-reversion) vs Gap-and-Go (momentum):")
    for col in ['ret_30min', 'ret_60min', 'ret_120min', 'ret_full']:
        valid = gap_df[['gap_pts', col]].dropna()
        if len(valid) < 5:
            continue
        # Fade: short if gap > 0, long if gap < 0 → profit = -sign(gap) * ret
        # Go: long if gap > 0, short if gap < 0 → profit = sign(gap) * ret
        gap_sign = np.sign(valid['gap_pts'])
        fade_pnl = -gap_sign * valid[col]
        go_pnl = gap_sign * valid[col]
        print(f"  {col}: Fade avg={fade_pnl.mean():+.2f} pts, Go avg={go_pnl.mean():+.2f} pts")
        # Win rate
        fade_wr = (fade_pnl > 0).mean()
        go_wr = (go_pnl > 0).mean()
        print(f"          Fade WR={fade_wr:.1%}, Go WR={go_wr:.1%}")

    # Large gaps (> median)
    med = gap_df['gap_pts'].abs().median()
    large = gap_df[gap_df['gap_pts'].abs() > med]
    print(f"\nLarge gaps (|gap| > {med:.0f} pts, n={len(large)}):")
    for col in ['ret_30min', 'ret_60min', 'ret_full']:
        valid = large[['gap_pts', col]].dropna()
        if len(valid) < 3:
            continue
        gap_sign = np.sign(valid['gap_pts'])
        fade_pnl = -gap_sign * valid[col]
        go_pnl = gap_sign * valid[col]
        print(f"  {col}: Fade avg={fade_pnl.mean():+.2f} pts, Go avg={go_pnl.mean():+.2f} pts")

    return gap_df


def analyze_c2_time_of_day(df: pd.DataFrame):
    """C2: Time-of-Day Return Patterns in 30-min buckets."""
    print("\n" + "="*80)
    print("C2: TIME-OF-DAY RETURN PATTERNS")
    print("="*80)

    day_ticks = df[df['session'] == 'day'].copy()

    # Create 30-min buckets: 08:30, 09:00, 09:30, ..., 13:00
    day_ticks['bucket'] = day_ticks['tw_time'].dt.floor('30min')
    day_ticks['bucket_label'] = day_ticks['bucket'].dt.strftime('%H:%M')

    results = []
    for date in day_ticks['tw_date'].unique():
        date_ticks = day_ticks[day_ticks['tw_date'] == date]

        for bucket_label, grp in date_ticks.groupby('bucket_label'):
            if len(grp) < 5:
                continue
            ret_pts = grp['price'].iloc[-1] - grp['price'].iloc[0]
            open_px = grp['price'].iloc[0]
            ret_bps = ret_pts / open_px * 10000
            vol = grp['price'].diff().dropna().std()
            results.append({
                'date': date,
                'bucket': bucket_label,
                'ret_pts': ret_pts,
                'ret_bps': ret_bps,
                'volatility': vol,
                'ticks': len(grp),
            })

    res_df = pd.DataFrame(results)
    if res_df.empty:
        print("No data!")
        return res_df

    # Aggregate by bucket
    print(f"\n{'Bucket':<8} {'Mean Ret':>10} {'Mean bps':>10} {'Std pts':>10} {'Sharpe':>8} {'N days':>7} {'%Pos':>7} {'%>4pts':>7}")
    print("-" * 75)

    bucket_stats = []
    for bucket in sorted(res_df['bucket'].unique()):
        b = res_df[res_df['bucket'] == bucket]
        mean_ret = b['ret_pts'].mean()
        mean_bps = b['ret_bps'].mean()
        std_ret = b['ret_pts'].std()
        sharpe = mean_ret / std_ret if std_ret > 0 else 0
        n = len(b)
        pct_pos = (b['ret_pts'] > 0).mean()
        pct_above_cost = (b['ret_pts'].abs() > RT_COST_PTS).mean()

        bucket_stats.append({
            'bucket': bucket,
            'mean_ret': mean_ret,
            'mean_bps': mean_bps,
            'std_ret': std_ret,
            'sharpe': sharpe,
            'n': n,
            'pct_pos': pct_pos,
            'pct_above_cost': pct_above_cost,
        })

        print(f"{bucket:<8} {mean_ret:>+10.2f} {mean_bps:>+10.2f} {std_ret:>10.2f} {sharpe:>+8.3f} {n:>7} {pct_pos:>7.1%} {pct_above_cost:>7.1%}")

    return pd.DataFrame(bucket_stats)


def analyze_c3_first_last_hour(df: pd.DataFrame):
    """C3: First/Last Hour Dynamics."""
    print("\n" + "="*80)
    print("C3: FIRST/LAST HOUR DYNAMICS")
    print("="*80)

    day_ticks = df[df['session'] == 'day'].copy()

    results = []
    for date in day_ticks['tw_date'].unique():
        dt = day_ticks[day_ticks['tw_date'] == date].sort_values('tw_time')
        if len(dt) < 100:
            continue

        open_ts = dt['tw_time'].iloc[0]
        close_ts = dt['tw_time'].iloc[-1]
        session_dur = (close_ts - open_ts).total_seconds()
        if session_dur < 3600:  # need at least 1 hour
            continue

        open_px = dt['price'].iloc[0]
        close_px = dt['price'].iloc[-1]

        # First 30 min
        first30 = dt[dt['tw_time'] < open_ts + pd.Timedelta(minutes=30)]
        first30_ret = first30['price'].iloc[-1] - first30['price'].iloc[0] if len(first30) > 1 else np.nan

        # Morning trend (first 2 hours, 08:45-10:45)
        morning = dt[dt['tw_time'] < open_ts + pd.Timedelta(hours=2)]
        morning_ret = morning['price'].iloc[-1] - morning['price'].iloc[0] if len(morning) > 1 else np.nan

        # Last 30 min (13:00-13:30)
        last30_start = pd.Timestamp(f"{date} 13:00:00")
        last30 = dt[dt['tw_time'] >= last30_start]
        last30_ret = last30['price'].iloc[-1] - last30['price'].iloc[0] if len(last30) > 1 else np.nan

        # Last 45 min (12:45-13:30+)
        last45_start = pd.Timestamp(f"{date} 12:45:00")
        last45 = dt[dt['tw_time'] >= last45_start]
        last45_ret = last45['price'].iloc[-1] - last45['price'].iloc[0] if len(last45) > 1 else np.nan

        # Mid-day trend direction at 13:00
        mid_day = dt[dt['tw_time'] < last30_start]
        if len(mid_day) > 1:
            morning_trend_dir = np.sign(mid_day['price'].iloc[-1] - open_px)
        else:
            morning_trend_dir = 0

        results.append({
            'date': date,
            'first30_ret': first30_ret,
            'morning_ret': morning_ret,
            'last30_ret': last30_ret,
            'last45_ret': last45_ret,
            'morning_trend_dir': morning_trend_dir,
            'session_ret': close_px - open_px,
        })

    res_df = pd.DataFrame(results)
    if res_df.empty:
        print("No data!")
        return res_df

    print(f"\nFirst 30 min return: mean={res_df['first30_ret'].mean():+.2f} pts, std={res_df['first30_ret'].std():.2f}")
    print(f"Morning (2h) return: mean={res_df['morning_ret'].mean():+.2f} pts, std={res_df['morning_ret'].std():.2f}")
    print(f"Last 30 min return:  mean={res_df['last30_ret'].mean():+.2f} pts, std={res_df['last30_ret'].std():.2f}")
    print(f"Last 45 min return:  mean={res_df['last45_ret'].mean():+.2f} pts, std={res_df['last45_ret'].std():.2f}")

    # Correlation: morning trend vs last-hour return
    valid = res_df.dropna(subset=['morning_ret', 'last30_ret'])
    if len(valid) > 5:
        ic = valid['morning_ret'].corr(valid['last30_ret'])
        print(f"\nIC(morning_ret vs last30_ret) = {ic:+.4f}")

        # Test: enter at 13:00 WITH morning trend vs AGAINST
        trend_dir = np.sign(valid['morning_ret'])
        with_trend = trend_dir * valid['last30_ret']
        against_trend = -trend_dir * valid['last30_ret']
        print(f"\nLast 30 min WITH morning trend:    avg={with_trend.mean():+.2f} pts, WR={(with_trend > 0).mean():.1%}")
        print(f"Last 30 min AGAINST morning trend: avg={against_trend.mean():+.2f} pts, WR={(against_trend > 0).mean():.1%}")

    # Autocorrelation: first30 vs rest-of-day
    valid2 = res_df.dropna(subset=['first30_ret', 'session_ret'])
    if len(valid2) > 5:
        rest_ret = valid2['session_ret'] - valid2['first30_ret']
        ic2 = valid2['first30_ret'].corr(rest_ret)
        print(f"\nIC(first30 vs rest_of_day) = {ic2:+.4f}")

        # Momentum: continue first30 direction
        f30_dir = np.sign(valid2['first30_ret'])
        mom_pnl = f30_dir * rest_ret
        fade_pnl = -f30_dir * rest_ret
        print(f"Continue first30 direction for rest of day: avg={mom_pnl.mean():+.2f} pts, WR={(mom_pnl > 0).mean():.1%}")
        print(f"Fade first30 direction for rest of day:     avg={fade_pnl.mean():+.2f} pts, WR={(fade_pnl > 0).mean():.1%}")

    return res_df


def analyze_c4_day_of_week(df: pd.DataFrame):
    """C4: Day-of-Week Effects."""
    print("\n" + "="*80)
    print("C4: DAY-OF-WEEK EFFECTS")
    print("="*80)

    day_ticks = df[df['session'] == 'day'].copy()

    results = []
    for date in day_ticks['tw_date'].unique():
        dt = day_ticks[day_ticks['tw_date'] == date].sort_values('tw_time')
        if len(dt) < 50:
            continue

        ret = dt['price'].iloc[-1] - dt['price'].iloc[0]
        vol = dt['price'].diff().dropna().std()
        open_px = dt['price'].iloc[0]
        ret_bps = ret / open_px * 10000

        dow = pd.Timestamp(date).day_name()

        results.append({
            'date': date,
            'dow': dow,
            'ret_pts': ret,
            'ret_bps': ret_bps,
            'volatility': vol,
            'ticks': len(dt),
        })

    res_df = pd.DataFrame(results)
    if res_df.empty:
        print("No data!")
        return res_df

    print(f"\n{'Day':<12} {'Mean Ret':>10} {'Mean bps':>10} {'Std pts':>10} {'Sharpe':>8} {'N':>5} {'%Pos':>7}")
    print("-" * 65)

    for dow in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
        d = res_df[res_df['dow'] == dow]
        if len(d) == 0:
            continue
        mean_ret = d['ret_pts'].mean()
        mean_bps = d['ret_bps'].mean()
        std_ret = d['ret_pts'].std()
        sharpe = mean_ret / std_ret if std_ret > 0 else 0
        n = len(d)
        pct_pos = (d['ret_pts'] > 0).mean()
        print(f"{dow:<12} {mean_ret:>+10.2f} {mean_bps:>+10.2f} {std_ret:>10.2f} {sharpe:>+8.3f} {n:>5} {pct_pos:>7.1%}")

    # Night-to-day by DOW
    print("\nNote: Small sample sizes — interpret with caution.")

    return res_df


def main():
    # Load and prepare data
    df = load_tick_data()
    df = select_near_month(df)

    # Classify sessions
    df['session'] = df.apply(classify_session, axis=1)
    df = df[df['session'].isin(['day', 'night'])]

    # Session boundaries
    sessions = compute_session_boundaries(df)
    print(f"\nSessions computed: {len(sessions)} total")
    print(sessions.groupby('session').size())

    # Run analyses
    gap_df = analyze_c1_opening_gap(df, sessions)
    bucket_df = analyze_c2_time_of_day(df)
    fl_df = analyze_c3_first_last_hour(df)
    dow_df = analyze_c4_day_of_week(df)

    print("\n" + "="*80)
    print("SUMMARY: KILL GATE ASSESSMENT")
    print("="*80)
    print(f"RT cost threshold: {RT_COST_PTS} pts ({RT_COST_BPS} bps)")
    print("\nKill gate criteria: |mean return| > 4 bps per period AND consistency > 60% of days")

    # Check C2 buckets for kill gate
    if not bucket_df.empty:
        passing = bucket_df[
            (bucket_df['mean_bps'].abs() > 4.0) &
            (((bucket_df['mean_bps'] > 0) & (bucket_df['pct_pos'] > 0.6)) |
             ((bucket_df['mean_bps'] < 0) & (bucket_df['pct_pos'] < 0.4)))
        ]
        if len(passing) > 0:
            print("\nPASSING kill gate (C2 buckets):")
            for _, row in passing.iterrows():
                print(f"  {row['bucket']}: {row['mean_bps']:+.2f} bps, {row['pct_pos']:.1%} positive, n={row['n']}")
        else:
            print("\nNo C2 buckets pass kill gate.")

    print("\nDone.")


if __name__ == '__main__':
    main()
