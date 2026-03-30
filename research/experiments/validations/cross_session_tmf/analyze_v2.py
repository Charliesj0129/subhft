"""
Direction C: Cross-Session Intraday Patterns on TMFD6 (Mini-TAIEX Futures)
v2: Aggregate in ClickHouse to avoid memory limits, load per-contract.
"""
import subprocess
import io
import numpy as np
import pandas as pd

RT_COST_PTS = 4.0
RT_COST_BPS = 1.33
# ClickHouse stores price_scaled = float_price * 1,000,000 (see recorder/mapper.py)
SCALE = 1_000_000.0

def run_ch(query: str) -> pd.DataFrame:
    cmd = ['docker', 'exec', 'clickhouse', 'clickhouse-client',
           '--query', query, '--format', 'TabSeparatedWithNames',
           '--max_memory_usage', '2000000000']
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"CH ERROR: {result.stderr[:500]}")
        return pd.DataFrame()
    if not result.stdout.strip():
        return pd.DataFrame()
    return pd.read_csv(io.StringIO(result.stdout), sep='\t')


def load_ticks_by_contract(symbol: str) -> pd.DataFrame:
    """Load ticks for one contract."""
    query = f"""
    SELECT
        exch_ts,
        price_scaled,
        volume
    FROM hft.market_data
    WHERE symbol = '{symbol}'
      AND type = 'Tick'
      AND price_scaled > 0
    ORDER BY exch_ts
    """
    print(f"  Loading {symbol}...")
    df = run_ch(query)
    if df.empty:
        return df
    df['symbol'] = symbol
    df['tw_time'] = pd.to_datetime(df['exch_ts'].astype(float) / 1e9, unit='s') + pd.Timedelta(hours=8)
    df['price'] = df['price_scaled'].astype(float) / SCALE
    df['tw_date'] = df['tw_time'].dt.date
    return df


def load_all_ticks() -> pd.DataFrame:
    """Load all TMF ticks contract by contract, then select near-month."""
    frames = []
    for sym in ['TMFB6', 'TMFC6', 'TMFD6']:
        d = load_ticks_by_contract(sym)
        if not d.empty:
            frames.append(d)

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values('exch_ts').reset_index(drop=True)

    # Near-month selection: per date, keep contract with most ticks
    counts = df.groupby(['tw_date', 'symbol']).size().reset_index(name='n')
    idx = counts.groupby('tw_date')['n'].idxmax()
    best = counts.loc[idx][['tw_date', 'symbol']].rename(columns={'symbol': 'near_sym'})
    df = df.merge(best, on='tw_date')
    df = df[df['symbol'] == df['near_sym']].drop(columns=['near_sym'])

    print(f"Total after near-month: {len(df):,} ticks, {df['tw_date'].nunique()} dates")
    return df


def classify_session(h, m):
    hm = h * 100 + m
    if 845 <= hm <= 1345:
        return 'day'
    elif hm >= 1500 or hm < 500:
        return 'night'
    return 'other'


def add_session(df: pd.DataFrame) -> pd.DataFrame:
    df['session'] = [classify_session(h, m) for h, m in zip(df['tw_time'].dt.hour, df['tw_time'].dt.minute)]
    return df[df['session'].isin(['day', 'night'])]


# ─── C1: Opening Gap ───

def analyze_c1(df):
    print("\n" + "="*80)
    print("C1: OPENING GAP SIGNAL")
    print("="*80)

    day = df[df['session'] == 'day']
    night = df[df['session'] == 'night']

    # Get session OHLC
    def session_ohlc(grp_df, session_name):
        results = []
        for date, g in grp_df.groupby('tw_date'):
            g = g.sort_values('exch_ts')
            if len(g) < 10:
                continue
            results.append({
                'tw_date': date,
                'open': g['price'].iloc[0],
                'close': g['price'].iloc[-1],
                'first_ts': g['tw_time'].iloc[0],
                'last_ts': g['tw_time'].iloc[-1],
            })
        return pd.DataFrame(results)

    day_sessions = session_ohlc(day, 'day')
    night_sessions = session_ohlc(night, 'night')

    gaps = []
    for _, d in day_sessions.iterrows():
        # Night session closing on same calendar date (early morning before day open)
        ns_match = night_sessions[night_sessions['last_ts'] < d['first_ts']]
        if len(ns_match) == 0:
            continue
        n = ns_match.iloc[-1]  # most recent night close

        gap_pts = d['open'] - n['close']
        gap_bps = gap_pts / n['close'] * 10000

        # Forward returns
        day_ticks = day[(day['tw_date'] == d['tw_date'])].sort_values('exch_ts')
        if len(day_ticks) < 50:
            continue
        open_px = day_ticks['price'].iloc[0]
        open_ts = day_ticks['tw_time'].iloc[0]

        rets = {}
        for mins in [30, 60, 120]:
            target = open_ts + pd.Timedelta(minutes=mins)
            fut = day_ticks[day_ticks['tw_time'] >= target]
            if len(fut) > 0:
                rets[f'ret_{mins}'] = fut['price'].iloc[0] - open_px
            else:
                rets[f'ret_{mins}'] = np.nan

        rets['ret_full'] = day_ticks['price'].iloc[-1] - open_px

        gaps.append({
            'date': d['tw_date'],
            'gap_pts': gap_pts,
            'gap_bps': gap_bps,
            **rets,
        })

    gdf = pd.DataFrame(gaps)
    if gdf.empty:
        print("No gap data!")
        return gdf

    print(f"\n{len(gdf)} gap observations")
    print(f"Mean gap: {gdf['gap_pts'].mean():+.1f} pts ({gdf['gap_bps'].mean():+.2f} bps)")
    print(f"Std gap:  {gdf['gap_pts'].std():.1f} pts, Range: [{gdf['gap_pts'].min():.0f}, {gdf['gap_pts'].max():.0f}]")

    print(f"\n{'Horizon':<12} {'IC(size)':>10} {'IC(dir)':>10} {'Fade avg':>10} {'Go avg':>10} {'Fade WR':>10} {'Go WR':>10}")
    print("-" * 72)

    for col in ['ret_30', 'ret_60', 'ret_120', 'ret_full']:
        v = gdf[['gap_bps', 'gap_pts', col]].dropna()
        if len(v) < 5:
            continue
        ic_size = v['gap_bps'].corr(v[col])
        ic_dir = np.sign(v['gap_pts']).corr(v[col])
        gs = np.sign(v['gap_pts'])
        fade = (-gs * v[col])
        go = (gs * v[col])
        label = col.replace('ret_', '') + ('min' if col != 'ret_full' else '')
        print(f"{label:<12} {ic_size:>+10.4f} {ic_dir:>+10.4f} {fade.mean():>+10.2f} {go.mean():>+10.2f} {(fade>0).mean():>10.1%} {(go>0).mean():>10.1%}")

    # Large gaps
    med = gdf['gap_pts'].abs().median()
    large = gdf[gdf['gap_pts'].abs() > med]
    print(f"\nLarge gaps (|gap| > {med:.0f} pts, n={len(large)}):")
    for col in ['ret_30', 'ret_60', 'ret_full']:
        v = large[['gap_pts', col]].dropna()
        if len(v) < 3:
            continue
        gs = np.sign(v['gap_pts'])
        fade = (-gs * v[col]).mean()
        go = (gs * v[col]).mean()
        label = col.replace('ret_', '')
        print(f"  {label}: Fade {fade:+.2f} pts, Go {go:+.2f} pts")

    # Gap size quintile analysis
    if len(gdf) >= 10:
        gdf['gap_q'] = pd.qcut(gdf['gap_bps'], 5, labels=['Q1(big_down)', 'Q2', 'Q3(neutral)', 'Q4', 'Q5(big_up)'], duplicates='drop')
        print("\nGap quintile → full session return:")
        for q in gdf['gap_q'].unique():
            sub = gdf[gdf['gap_q'] == q]['ret_full'].dropna()
            if len(sub) > 0:
                print(f"  {q}: mean={sub.mean():+.2f} pts, n={len(sub)}")

    return gdf


# ─── C2: Time-of-Day Returns ───

def analyze_c2(df):
    print("\n" + "="*80)
    print("C2: TIME-OF-DAY RETURN PATTERNS (30-min buckets)")
    print("="*80)

    day = df[df['session'] == 'day'].copy()
    day['bucket'] = day['tw_time'].dt.floor('30min').dt.strftime('%H:%M')

    results = []
    for (date, bucket), grp in day.groupby(['tw_date', 'bucket']):
        grp = grp.sort_values('exch_ts')
        if len(grp) < 5:
            continue
        ret = grp['price'].iloc[-1] - grp['price'].iloc[0]
        ret_bps = ret / grp['price'].iloc[0] * 10000
        results.append({
            'date': date,
            'bucket': bucket,
            'ret_pts': ret,
            'ret_bps': ret_bps,
            'ticks': len(grp),
        })

    rdf = pd.DataFrame(results)
    if rdf.empty:
        print("No data!")
        return rdf

    print(f"\n{'Bucket':<8} {'Mean pts':>10} {'Mean bps':>10} {'Std pts':>10} {'Sharpe':>8} {'N':>5} {'%Pos':>7} {'%>4pts':>7}")
    print("-" * 72)

    stats = []
    for bucket in sorted(rdf['bucket'].unique()):
        b = rdf[rdf['bucket'] == bucket]
        mr = b['ret_pts'].mean()
        mb = b['ret_bps'].mean()
        sr = b['ret_pts'].std()
        sh = mr / sr if sr > 0 else 0
        n = len(b)
        pp = (b['ret_pts'] > 0).mean()
        pa = (b['ret_pts'].abs() > RT_COST_PTS).mean()
        stats.append({'bucket': bucket, 'mean_bps': mb, 'mean_pts': mr, 'std': sr,
                       'sharpe': sh, 'n': n, 'pct_pos': pp, 'pct_above_cost': pa})
        print(f"{bucket:<8} {mr:>+10.2f} {mb:>+10.2f} {sr:>10.2f} {sh:>+8.3f} {n:>5} {pp:>7.1%} {pa:>7.1%}")

    # Cumulative intraday return
    print("\nCumulative intraday pattern (opening = 0):")
    cum = 0
    for s in sorted(stats, key=lambda x: x['bucket']):
        cum += s['mean_pts']
        print(f"  {s['bucket']}: cumulative {cum:+.1f} pts")

    return pd.DataFrame(stats)


# ─── C3: First/Last Hour ───

def analyze_c3(df):
    print("\n" + "="*80)
    print("C3: FIRST/LAST HOUR DYNAMICS")
    print("="*80)

    day = df[df['session'] == 'day'].copy()

    results = []
    for date in day['tw_date'].unique():
        dt = day[day['tw_date'] == date].sort_values('exch_ts')
        if len(dt) < 100:
            continue

        open_ts = dt['tw_time'].iloc[0]
        open_px = dt['price'].iloc[0]
        close_px = dt['price'].iloc[-1]

        # First 30 min
        f30 = dt[dt['tw_time'] < open_ts + pd.Timedelta(minutes=30)]
        f30_ret = f30['price'].iloc[-1] - f30['price'].iloc[0] if len(f30) > 1 else np.nan

        # Morning (2h from open)
        morn = dt[dt['tw_time'] < open_ts + pd.Timedelta(hours=2)]
        morn_ret = morn['price'].iloc[-1] - morn['price'].iloc[0] if len(morn) > 1 else np.nan

        # Price at 13:00
        t1300 = dt[dt['tw_time'] >= pd.Timestamp(f"{date} 13:00:00")]
        px_1300 = t1300['price'].iloc[0] if len(t1300) > 0 else np.nan
        morning_to_1300 = px_1300 - open_px if not np.isnan(px_1300) else np.nan

        # Last 30 min (from 13:00)
        last30_ret = close_px - px_1300 if not np.isnan(px_1300) else np.nan

        # Last 45 min (from 12:45)
        t1245 = dt[dt['tw_time'] >= pd.Timestamp(f"{date} 12:45:00")]
        last45_ret = t1245['price'].iloc[-1] - t1245['price'].iloc[0] if len(t1245) > 1 else np.nan

        results.append({
            'date': date,
            'first30_ret': f30_ret,
            'morning_ret': morn_ret,
            'morning_to_1300': morning_to_1300,
            'last30_ret': last30_ret,
            'last45_ret': last45_ret,
            'session_ret': close_px - open_px,
        })

    rdf = pd.DataFrame(results)
    if rdf.empty:
        print("No data!")
        return rdf

    print(f"\n{len(rdf)} day sessions analyzed")
    for col in ['first30_ret', 'morning_ret', 'last30_ret', 'last45_ret', 'session_ret']:
        v = rdf[col].dropna()
        print(f"  {col:<20}: mean={v.mean():+.2f}, std={v.std():.2f}, %pos={(v>0).mean():.1%}, n={len(v)}")

    # Morning trend → last 30 min
    v = rdf[['morning_to_1300', 'last30_ret']].dropna()
    if len(v) >= 5:
        ic = v['morning_to_1300'].corr(v['last30_ret'])
        print(f"\nIC(morning_to_1300 vs last30_ret) = {ic:+.4f}")
        trend_dir = np.sign(v['morning_to_1300'])
        with_t = (trend_dir * v['last30_ret'])
        against_t = (-trend_dir * v['last30_ret'])
        print(f"With morning trend last 30 min:    avg={with_t.mean():+.2f} pts, WR={(with_t>0).mean():.1%}")
        print(f"Against morning trend last 30 min: avg={against_t.mean():+.2f} pts, WR={(against_t>0).mean():.1%}")

    # First 30 → rest of day
    v2 = rdf[['first30_ret', 'session_ret']].dropna()
    if len(v2) >= 5:
        rest = v2['session_ret'] - v2['first30_ret']
        ic2 = v2['first30_ret'].corr(rest)
        print(f"\nIC(first30 vs rest_of_day) = {ic2:+.4f}")
        f30d = np.sign(v2['first30_ret'])
        mom = (f30d * rest)
        fad = (-f30d * rest)
        print(f"Continue first30 for rest of day: avg={mom.mean():+.2f} pts, WR={(mom>0).mean():.1%}")
        print(f"Fade first30 for rest of day:     avg={fad.mean():+.2f} pts, WR={(fad>0).mean():.1%}")

    # Large morning moves
    v3 = rdf[['morning_to_1300', 'last30_ret']].dropna()
    if len(v3) >= 10:
        med = v3['morning_to_1300'].abs().median()
        large = v3[v3['morning_to_1300'].abs() > med]
        print(f"\nLarge morning moves (|move| > {med:.0f} pts, n={len(large)}):")
        td = np.sign(large['morning_to_1300'])
        w = (td * large['last30_ret'])
        a = (-td * large['last30_ret'])
        print(f"  With trend:    avg={w.mean():+.2f} pts, WR={(w>0).mean():.1%}")
        print(f"  Against trend: avg={a.mean():+.2f} pts, WR={(a>0).mean():.1%}")

    return rdf


# ─── C4: Day-of-Week ───

def analyze_c4(df):
    print("\n" + "="*80)
    print("C4: DAY-OF-WEEK EFFECTS")
    print("="*80)

    day = df[df['session'] == 'day'].copy()

    results = []
    for date in day['tw_date'].unique():
        dt = day[day['tw_date'] == date].sort_values('exch_ts')
        if len(dt) < 50:
            continue
        ret = dt['price'].iloc[-1] - dt['price'].iloc[0]
        ret_bps = ret / dt['price'].iloc[0] * 10000
        vol = dt['price'].diff().dropna().std()
        dow = pd.Timestamp(date).day_name()
        dow_num = pd.Timestamp(date).dayofweek
        results.append({
            'date': date, 'dow': dow, 'dow_num': dow_num,
            'ret_pts': ret, 'ret_bps': ret_bps, 'volatility': vol, 'ticks': len(dt),
        })

    rdf = pd.DataFrame(results)
    if rdf.empty:
        print("No data!")
        return rdf

    print(f"\n{len(rdf)} day sessions")
    print(f"\n{'Day':<12} {'Mean pts':>10} {'Mean bps':>10} {'Std pts':>10} {'Sharpe':>8} {'N':>5} {'%Pos':>7} {'Avg Vol':>8}")
    print("-" * 75)

    for dow in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
        d = rdf[rdf['dow'] == dow]
        if len(d) == 0:
            continue
        mr = d['ret_pts'].mean()
        mb = d['ret_bps'].mean()
        sr = d['ret_pts'].std()
        sh = mr / sr if sr > 0 else 0
        n = len(d)
        pp = (d['ret_pts'] > 0).mean()
        av = d['volatility'].mean()
        print(f"{dow:<12} {mr:>+10.2f} {mb:>+10.2f} {sr:>10.2f} {sh:>+8.3f} {n:>5} {pp:>7.1%} {av:>8.2f}")

    # Night session by DOW
    night = df[df['session'] == 'night'].copy()
    night_results = []
    for date in night['tw_date'].unique():
        dt = night[night['tw_date'] == date].sort_values('exch_ts')
        if len(dt) < 50:
            continue
        ret = dt['price'].iloc[-1] - dt['price'].iloc[0]
        dow = pd.Timestamp(date).day_name()
        night_results.append({'date': date, 'dow': dow, 'ret_pts': ret})

    ndf = pd.DataFrame(night_results)
    if not ndf.empty:
        print("\nNight session by day-of-week:")
        print(f"{'Day':<12} {'Mean pts':>10} {'Std':>10} {'N':>5} {'%Pos':>7}")
        print("-" * 50)
        for dow in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
            d = ndf[ndf['dow'] == dow]
            if len(d) == 0:
                continue
            print(f"{dow:<12} {d['ret_pts'].mean():>+10.2f} {d['ret_pts'].std():>10.2f} {len(d):>5} {(d['ret_pts']>0).mean():>7.1%}")

    return rdf


# ─── C5: Overnight holding signal ───

def analyze_overnight(df):
    """Test: holding overnight from day close to next day open."""
    print("\n" + "="*80)
    print("C5: OVERNIGHT HOLDING SIGNAL")
    print("="*80)

    day = df[df['session'] == 'day']
    sessions = []
    for date in sorted(day['tw_date'].unique()):
        dt = day[day['tw_date'] == date].sort_values('exch_ts')
        if len(dt) < 50:
            continue
        sessions.append({
            'date': date,
            'open': dt['price'].iloc[0],
            'close': dt['price'].iloc[-1],
            'ret': dt['price'].iloc[-1] - dt['price'].iloc[0],
        })

    sdf = pd.DataFrame(sessions)
    if len(sdf) < 3:
        print("Insufficient data")
        return

    # Overnight return: close[i] → open[i+1]
    sdf['next_open'] = sdf['open'].shift(-1)
    sdf['overnight_ret'] = sdf['next_open'] - sdf['close']
    sdf['overnight_bps'] = sdf['overnight_ret'] / sdf['close'] * 10000

    v = sdf['overnight_ret'].dropna()
    print(f"\n{len(v)} overnight periods")
    print(f"Mean overnight return: {v.mean():+.2f} pts ({sdf['overnight_bps'].dropna().mean():+.2f} bps)")
    print(f"Std: {v.std():.2f} pts")
    print(f"%Positive: {(v > 0).mean():.1%}")

    # Day return → overnight predictability
    v2 = sdf[['ret', 'overnight_ret']].dropna()
    if len(v2) >= 5:
        ic = v2['ret'].corr(v2['overnight_ret'])
        print(f"\nIC(day_ret → overnight_ret) = {ic:+.4f}")
        # Momentum vs reversal
        d_dir = np.sign(v2['ret'])
        mom = (d_dir * v2['overnight_ret'])
        fad = (-d_dir * v2['overnight_ret'])
        print(f"Overnight WITH day trend:    avg={mom.mean():+.2f} pts, WR={(mom>0).mean():.1%}")
        print(f"Overnight AGAINST day trend: avg={fad.mean():+.2f} pts, WR={(fad>0).mean():.1%}")

    # Serial autocorrelation: day[i] → day[i+1]
    sdf['next_ret'] = sdf['ret'].shift(-1)
    v3 = sdf[['ret', 'next_ret']].dropna()
    if len(v3) >= 5:
        ic3 = v3['ret'].corr(v3['next_ret'])
        print(f"\nIC(day[i] → day[i+1]) = {ic3:+.4f}")


def main():
    print("Loading ticks per contract...")
    df = load_all_ticks()
    df = add_session(df)
    print(f"Sessions classified: {df['session'].value_counts().to_dict()}")

    analyze_c1(df)
    analyze_c2(df)
    analyze_c3(df)
    analyze_c4(df)
    analyze_overnight(df)

    print("\n" + "="*80)
    print("KILL GATE SUMMARY")
    print("="*80)
    print(f"Threshold: |mean ret| > {RT_COST_PTS} pts ({RT_COST_BPS} bps) AND consistency > 60%")
    print("See analysis above for results. Report will be written separately.")


if __name__ == '__main__':
    main()
