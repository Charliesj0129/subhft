"""
R54 Explore v2: Spread-triggered analysis with CORRECT front-month chain.

Front-month mapping:
- Jan 27 — Feb 6: TXFB6 (Feb expiry, CK export parquet, scale x1M)
- Feb 25: TXFC6 (Mar expiry, CK export parquet)
- Mar 19, 20, 24: TXFD6 front-month (L1 npy, raw points)

Key questions:
1. TMFD6 spread > 7 → front-month TXFD spread correlation
2. TXFD front-month realized range conditional on TMFD6 spread > 7
3. Episode duration and forward move characteristics
"""

import numpy as np
import pandas as pd
from pathlib import Path

TMFD6_DIR = Path("research/data/raw/tmfd6")
CK_DIR = Path("research/data/ck_export")
TXFD6_DIR = Path("research/data/raw/txfd6")

# Front-month chain: (date, symbol, source, path)
FRONT_CHAIN = [
    # TXFB6 (front month Jan-Feb)
    ("2026-01-27", "TXFB6", "parquet"),
    ("2026-01-28", "TXFB6", "parquet"),
    ("2026-01-29", "TXFB6", "parquet"),
    ("2026-01-30", "TXFB6", "parquet"),
    ("2026-02-03", "TXFB6", "parquet"),  # partial
    ("2026-02-04", "TXFB6", "parquet"),
    ("2026-02-05", "TXFB6", "parquet"),
    ("2026-02-06", "TXFB6", "parquet"),
    # TXFC6 (front month late Feb-Mar)
    ("2026-02-25", "TXFC6", "parquet"),  # partial
    # TXFD6 (front month late Mar+, use l1 npy)
    ("2026-03-19", "TXFD6", "npy"),
    ("2026-03-20", "TXFD6", "npy"),
    ("2026-03-24", "TXFD6", "npy"),
]

# TMFD6 dates that overlap with front-month chain
TMFD6_DATES = [fc[0] for fc in FRONT_CHAIN]

SCALE = 1_000_000  # CK parquet price scale


def load_tmfd6(date: str) -> np.ndarray:
    return np.load(str(TMFD6_DIR / f"TMFD6_{date}_l1.npy"), allow_pickle=True)


def load_front_bidask(symbol: str, date: str, source: str):
    """Load front-month bid/ask data. Returns (ts, bid1, ask1) arrays."""
    if source == "npy":
        d = np.load(str(TXFD6_DIR / f"{symbol}_{date}_l1.npy"), allow_pickle=True)
        return d["local_ts"], d["bid_px"], d["ask_px"], d["mid_price"]

    # parquet from CK export
    suffix = "_partial" if "partial" in str(list(CK_DIR.glob(f"{symbol}/{date}*"))[0]) else ""
    pq_path = CK_DIR / symbol / f"{date}{suffix}.parquet"
    if not pq_path.exists():
        pq_path = CK_DIR / symbol / f"{date}.parquet"
    df = pd.read_parquet(pq_path)

    # Filter BidAsk rows only (they have L5 book)
    ba = df[df["type"] == "BidAsk"].copy()
    if ba.empty:
        return None, None, None, None

    ts = ba["exch_ts"].values  # nanoseconds
    bid1 = np.array([arr[0] / SCALE if len(arr) > 0 else np.nan for arr in ba["bids_price"]])
    ask1 = np.array([arr[0] / SCALE if len(arr) > 0 else np.nan for arr in ba["asks_price"]])
    mid = (bid1 + ask1) / 2

    # Remove NaN rows
    valid = ~(np.isnan(bid1) | np.isnan(ask1))
    return ts[valid], bid1[valid], ask1[valid], mid[valid]


def spread_pts_raw(data):
    return data["ask_px"] - data["bid_px"]


def detect_episodes(spread, ts, threshold=7.0):
    above = spread >= threshold
    episodes = []
    in_episode = False
    start = 0
    for i in range(len(above)):
        if above[i] and not in_episode:
            in_episode = True
            start = i
        elif not above[i] and in_episode:
            in_episode = False
            dur = ts[i - 1] - ts[start]
            episodes.append((start, i, dur, spread[start:i].mean()))
    if in_episode:
        dur = ts[len(above) - 1] - ts[start]
        episodes.append((start, len(above), dur, spread[start:].mean()))
    return episodes


def main():
    print("=" * 80)
    print("R54 EXPLORE v2: Front-Month Chain Analysis")
    print("=" * 80)

    # =================================================================
    # Part 1: TMFD6 vs Front-Month spread correlation + conditional spread
    # =================================================================
    print("\n" + "=" * 80)
    print("PART 1: TMFD6 ↔ Front-Month Spread Correlation")
    print("=" * 80)

    for date, symbol, source in FRONT_CHAIN:
        # Load TMFD6
        try:
            tm = load_tmfd6(date)
        except FileNotFoundError:
            print(f"  {date}: TMFD6 not found, skip")
            continue

        # Load front-month
        ts_fm, bid_fm, ask_fm, mid_fm = load_front_bidask(symbol, date, source)
        if ts_fm is None or len(ts_fm) < 100:
            print(f"  {date} [{symbol}]: insufficient front-month data, skip")
            continue

        sp_fm = ask_fm - bid_fm
        sp_tm = spread_pts_raw(tm)
        ts_tm = tm["local_ts"]

        # Align: for each front-month tick, find nearest TMFD6 tick
        idx = np.searchsorted(ts_tm, ts_fm)
        idx = np.clip(idx, 0, len(ts_tm) - 1)
        dt = np.abs(ts_tm[idx] - ts_fm)
        mask_aligned = dt < 200_000_000  # 200ms

        if mask_aligned.sum() < 100:
            print(f"  {date} [{symbol}]: insufficient alignment, skip")
            continue

        sp_tm_aligned = sp_tm[idx[mask_aligned]]
        sp_fm_aligned = sp_fm[mask_aligned]

        corr = np.corrcoef(sp_tm_aligned, sp_fm_aligned)[0, 1]

        mask7 = sp_tm_aligned >= 7
        period = "Jan-Feb" if date < "2026-03-01" else "March"
        if mask7.sum() > 10:
            fm_when = np.median(sp_fm_aligned[mask7])
            fm_else = np.median(sp_fm_aligned[~mask7])
            print(f"  {date} [{symbol:>5s}] [{period:>7s}]: corr={corr:.3f}  "
                  f"Front spread when TMFD6>=7: {fm_when:.1f} (n={mask7.sum()})  "
                  f"otherwise: {fm_else:.1f}  "
                  f"(TMFD6 >=7: {mask7.sum()/len(mask7)*100:.1f}%)")
        else:
            print(f"  {date} [{symbol:>5s}] [{period:>7s}]: corr={corr:.3f}  "
                  f"TMFD6>=7: {mask7.sum()} events (too few)")

    # =================================================================
    # Part 2: Forward TXFD range after TMFD6 spread > 7 trigger
    # =================================================================
    print("\n" + "=" * 80)
    print("PART 2: Front-Month Forward Range after TMFD6 Spread >= 7")
    print("=" * 80)

    for threshold in [7, 10, 15]:
        print(f"\n  --- TMFD6 Spread Threshold: >= {threshold} ---")
        cond_results = {w: [] for w in ["5min", "30min", "60min"]}
        base_results = {w: [] for w in ["5min", "30min", "60min"]}
        windows = {"5min": 300e9, "30min": 1800e9, "60min": 3600e9}

        for date, symbol, source in FRONT_CHAIN:
            try:
                tm = load_tmfd6(date)
            except FileNotFoundError:
                continue

            ts_fm, bid_fm, ask_fm, mid_fm = load_front_bidask(symbol, date, source)
            if ts_fm is None or len(ts_fm) < 100:
                continue

            sp_tm = spread_pts_raw(tm)
            episodes = detect_episodes(sp_tm, tm["local_ts"], float(threshold))

            for start_idx, end_idx, dur_ns, mean_sp in episodes:
                trigger_ts = tm["local_ts"][start_idx]

                for wname, wns in windows.items():
                    mask_fwd = (ts_fm >= trigger_ts) & (ts_fm <= trigger_ts + wns)
                    if mask_fwd.sum() < 10:
                        continue
                    hi = mid_fm[mask_fwd].max()
                    lo = mid_fm[mask_fwd].min()
                    rng = hi - lo
                    cond_results[wname].append(rng)

            # Baseline: random triggers
            np.random.seed(42 + hash(date) % 10000)
            for _ in range(min(20, max(1, len(episodes)))):
                rand_ts = tm["local_ts"][np.random.randint(0, len(tm))]
                for wname, wns in windows.items():
                    mask_fwd = (ts_fm >= rand_ts) & (ts_fm <= rand_ts + wns)
                    if mask_fwd.sum() < 10:
                        continue
                    hi = mid_fm[mask_fwd].max()
                    lo = mid_fm[mask_fwd].min()
                    rng = hi - lo
                    base_results[wname].append(rng)

        for wname in ["5min", "30min", "60min"]:
            cond = cond_results[wname]
            base = base_results[wname]
            if cond and base:
                c_med, c_mean = np.median(cond), np.mean(cond)
                b_med, b_mean = np.median(base), np.mean(base)
                ratio = c_med / b_med if b_med > 0 else float("inf")
                print(f"    {wname}: COND median={c_med:6.1f}  mean={c_mean:6.1f}  "
                      f"P75={np.percentile(cond,75):6.1f}  P90={np.percentile(cond,90):6.1f}  (n={len(cond)})")
                print(f"    {wname}: BASE median={b_med:6.1f}  mean={b_mean:6.1f}  "
                      f"P75={np.percentile(base,75):6.1f}  P90={np.percentile(base,90):6.1f}  (n={len(base)})")
                print(f"    {wname}: Ratio: {ratio:.2f}x")
            elif cond:
                print(f"    {wname}: COND n={len(cond)} (no baseline)")
            else:
                print(f"    {wname}: no data")

    # =================================================================
    # Part 3: Episode characterization on front-month
    # =================================================================
    print("\n" + "=" * 80)
    print("PART 3: Episode Characterization (TMFD6 sp>=7 → front-month move)")
    print("=" * 80)

    for threshold in [7, 10]:
        ep_data = []
        for date, symbol, source in FRONT_CHAIN:
            try:
                tm = load_tmfd6(date)
            except FileNotFoundError:
                continue
            ts_fm, bid_fm, ask_fm, mid_fm = load_front_bidask(symbol, date, source)
            if ts_fm is None or len(ts_fm) < 100:
                continue

            sp_tm = spread_pts_raw(tm)
            episodes = detect_episodes(sp_tm, tm["local_ts"], float(threshold))

            for start_idx, end_idx, dur_ns, mean_sp in episodes:
                ep_start = tm["local_ts"][start_idx]
                ep_end = tm["local_ts"][min(end_idx, len(tm) - 1)]
                mask = (ts_fm >= ep_start) & (ts_fm <= ep_end)
                if mask.sum() < 3:
                    continue
                m = mid_fm[mask]
                rng = m.max() - m.min()
                direction = m[-1] - m[0]
                dur_s = dur_ns / 1e9
                sp_fm_during = (ask_fm[mask] - bid_fm[mask]).mean()
                ep_data.append({
                    "date": date, "symbol": symbol, "dur_s": dur_s,
                    "range": rng, "direction": direction,
                    "tmfd6_spread": mean_sp, "fm_spread": sp_fm_during,
                })

        if ep_data:
            ranges = [e["range"] for e in ep_data]
            dirs = [e["direction"] for e in ep_data]
            durs = [e["dur_s"] for e in ep_data]
            fm_sps = [e["fm_spread"] for e in ep_data]
            print(f"\n  Threshold >= {threshold}: {len(ep_data)} episodes")
            print(f"    FM Range:    med={np.median(ranges):6.1f}  mean={np.mean(ranges):6.1f}  "
                  f"P75={np.percentile(ranges,75):6.1f}  P90={np.percentile(ranges,90):6.1f} pts")
            print(f"    Duration:    med={np.median(durs):6.1f}  mean={np.mean(durs):6.1f}  "
                  f"P75={np.percentile(durs,75):6.1f}  P90={np.percentile(durs,90):6.1f} s")
            print(f"    |Direction|: med={np.median(np.abs(dirs)):6.1f}  mean={np.mean(np.abs(dirs)):6.1f} pts")
            print(f"    FM Spread:   med={np.median(fm_sps):6.1f}  mean={np.mean(fm_sps):6.1f} pts")
            pct_pos = sum(1 for d in dirs if d > 0) / len(dirs) * 100
            print(f"    Direction:   {pct_pos:.1f}% positive, {100-pct_pos:.1f}% negative")

            # Straddle breakeven
            print(f"\n    --- Straddle Breakeven (episodes with range >= X) ---")
            for cost in [10, 20, 40, 80, 120, 150, 200]:
                pct = sum(1 for r in ranges if r >= cost) / len(ranges) * 100
                count = sum(1 for r in ranges if r >= cost)
                print(f"      range >= {cost:3d} pts: {pct:5.1f}% ({count}/{len(ranges)})")

            # TXFD6 MR potential: episodes with range > 20 but |direction| < range/2
            mr_count = sum(1 for r, d in zip(ranges, dirs) if r > 20 and abs(d) < r / 2)
            print(f"\n    Mean-reversion potential (range>20 & |dir|<range/2): "
                  f"{mr_count}/{len(ranges)} ({mr_count/len(ranges)*100:.1f}%)")

            # By period
            for period_label, period_fn in [
                ("Jan-Feb", lambda d: d < "2026-03-01"),
                ("March", lambda d: d >= "2026-03-01"),
            ]:
                pe = [e for e in ep_data if period_fn(e["date"])]
                if pe:
                    pr = [e["range"] for e in pe]
                    print(f"\n    {period_label} ({len(pe)} episodes): "
                          f"range med={np.median(pr):.1f}  mean={np.mean(pr):.1f}  "
                          f"P90={np.percentile(pr,90):.1f}")
        else:
            print(f"\n  Threshold >= {threshold}: no episodes")

    # =================================================================
    # Part 4: March-specific deep dive
    # =================================================================
    print("\n" + "=" * 80)
    print("PART 4: March Deep Dive — Episode frequency & capacity")
    print("=" * 80)

    march_dates = ["2026-03-19", "2026-03-20", "2026-03-24", "2026-03-25", "2026-03-26"]
    for threshold in [5, 7, 10]:
        total_eps = 0
        total_time_pct = 0
        total_dur_s = 0
        n_days = 0
        for date in march_dates:
            try:
                tm = load_tmfd6(date)
            except FileNotFoundError:
                continue
            sp = spread_pts_raw(tm)
            eps = detect_episodes(sp, tm["local_ts"], float(threshold))
            pct = (sp >= threshold).sum() / len(sp) * 100
            total_eps += len(eps)
            total_time_pct += pct
            total_dur_s += sum(e[2] / 1e9 for e in eps)
            n_days += 1

        if n_days > 0:
            print(f"  Threshold >= {threshold}: "
                  f"avg {total_eps/n_days:.1f} episodes/day  "
                  f"avg {total_time_pct/n_days:.2f}% of time  "
                  f"avg total dur {total_dur_s/n_days:.1f}s/day  "
                  f"({n_days} days)")

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
