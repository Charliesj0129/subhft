"""
R54 Explore: Spread-triggered options / volatility analysis.

Key questions:
1. TMFD6-TXFD6 spread correlation (concurrent, time-aligned)
2. TXFD6 realized range conditional on TMFD6 spread > 7
3. March spread > 7 frequency (episodes per day)
4. Episode duration and characteristics
5. TXFD6 forward realized vol after TMFD6 spread > 7 trigger
"""

import numpy as np
from pathlib import Path
from collections import defaultdict

TMFD6_DIR = Path("research/data/raw/tmfd6")
TXFD6_DIR = Path("research/data/raw/txfd6")

# Overlapping dates
OVERLAP_DATES = [
    "2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30",
    "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06",
    "2026-03-19", "2026-03-20", "2026-03-24",
]

# All TMFD6 dates (for spread frequency analysis)
ALL_TMFD6_DATES = [
    "2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30",
    "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06",
    "2026-02-10", "2026-02-11", "2026-02-23", "2026-02-24", "2026-02-25",
    "2026-03-19", "2026-03-20", "2026-03-24", "2026-03-25", "2026-03-26",
]


def load_l1(directory: Path, symbol: str, date: str) -> np.ndarray:
    path = directory / f"{symbol}_{date}_l1.npy"
    return np.load(str(path), allow_pickle=True)


def spread_pts(data: np.ndarray) -> np.ndarray:
    """Compute spread in raw index points."""
    return data["ask_px"] - data["bid_px"]


def align_by_time(tmfd6: np.ndarray, txfd6: np.ndarray, window_ns: int = 100_000_000):
    """
    Align TMFD6 and TXFD6 by nearest timestamp within window (100ms default).
    Returns aligned arrays (same length).
    """
    # Use merge-join approach on sorted timestamps
    t_tm = tmfd6["local_ts"]
    t_tx = txfd6["local_ts"]

    # For each TXFD6 tick, find nearest TMFD6 tick
    idx_tm = np.searchsorted(t_tm, t_tx)
    idx_tm = np.clip(idx_tm, 0, len(t_tm) - 1)

    # Check if nearest is within window
    dt = np.abs(t_tm[idx_tm] - t_tx)
    mask = dt < window_ns

    return tmfd6[idx_tm[mask]], txfd6[mask]


def detect_episodes(spread: np.ndarray, ts: np.ndarray, threshold: float = 7.0):
    """
    Detect contiguous episodes where spread >= threshold.
    Returns list of (start_idx, end_idx, duration_ns, mean_spread).
    """
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
            mean_sp = spread[start:i].mean()
            episodes.append((start, i, dur, mean_sp))

    if in_episode:
        dur = ts[len(above) - 1] - ts[start]
        mean_sp = spread[start:].mean()
        episodes.append((start, len(above), dur, mean_sp))

    return episodes


def main():
    print("=" * 80)
    print("R54 EXPLORE: Spread-Triggered Options / Volatility Analysis")
    print("=" * 80)

    # ===================================================================
    # Part 1: TMFD6 spread > 7 frequency by period
    # ===================================================================
    print("\n" + "=" * 80)
    print("PART 1: TMFD6 Spread >= 7 Frequency by Date")
    print("=" * 80)

    daily_stats = {}
    for date in ALL_TMFD6_DATES:
        tm = load_l1(TMFD6_DIR, "TMFD6", date)
        sp = spread_pts(tm)
        n = len(sp)
        pct_ge5 = (sp >= 5).sum() / n * 100
        pct_ge7 = (sp >= 7).sum() / n * 100
        pct_ge10 = (sp >= 10).sum() / n * 100
        median_sp = np.median(sp)

        episodes_7 = detect_episodes(sp, tm["local_ts"], 7.0)
        episodes_10 = detect_episodes(sp, tm["local_ts"], 10.0)

        daily_stats[date] = {
            "n": n, "median": median_sp,
            "pct_ge5": pct_ge5, "pct_ge7": pct_ge7, "pct_ge10": pct_ge10,
            "episodes_7": len(episodes_7), "episodes_10": len(episodes_10),
        }

        # Episode duration stats
        if episodes_7:
            durations_s = [e[2] / 1e9 for e in episodes_7]
            mean_dur = np.mean(durations_s)
            max_dur = np.max(durations_s)
        else:
            mean_dur = max_dur = 0

        period = "Jan-Feb" if date < "2026-03-01" else "March"
        print(f"  {date} [{period:>7s}]: median={median_sp:5.1f}  "
              f">=5: {pct_ge5:5.1f}%  >=7: {pct_ge7:5.1f}%  >=10: {pct_ge10:5.1f}%  "
              f"episodes(>=7): {len(episodes_7):3d}  "
              f"avg_dur: {mean_dur:6.1f}s  max_dur: {max_dur:7.1f}s")

    # Summary by period
    jan_feb = {k: v for k, v in daily_stats.items() if k < "2026-03-01"}
    march = {k: v for k, v in daily_stats.items() if k >= "2026-03-01"}

    print("\n--- Summary ---")
    for label, group in [("Jan-Feb", jan_feb), ("March", march)]:
        if not group:
            continue
        avg_pct7 = np.mean([v["pct_ge7"] for v in group.values()])
        avg_ep7 = np.mean([v["episodes_7"] for v in group.values()])
        avg_med = np.mean([v["median"] for v in group.values()])
        print(f"  {label:>7s}: avg_median_spread={avg_med:.1f} pts  "
              f"avg >=7 pct={avg_pct7:.1f}%  avg episodes/day={avg_ep7:.1f}")

    # ===================================================================
    # Part 2: TMFD6-TXFD6 spread correlation
    # ===================================================================
    print("\n" + "=" * 80)
    print("PART 2: TMFD6-TXFD6 Spread Correlation (time-aligned)")
    print("=" * 80)

    all_corr = []
    for date in OVERLAP_DATES:
        tm = load_l1(TMFD6_DIR, "TMFD6", date)
        tx = load_l1(TXFD6_DIR, "TXFD6", date)
        tm_a, tx_a = align_by_time(tm, tx)

        if len(tm_a) < 100:
            print(f"  {date}: insufficient aligned ticks ({len(tm_a)}), skipping")
            continue

        sp_tm = spread_pts(tm_a)
        sp_tx = spread_pts(tx_a)
        corr = np.corrcoef(sp_tm, sp_tx)[0, 1]
        all_corr.append(corr)

        # Also check: when TMFD6 spread > 7, what is TXFD6 spread?
        mask7 = sp_tm >= 7
        if mask7.sum() > 0:
            tx_sp_when_tm7 = sp_tx[mask7]
            tx_sp_otherwise = sp_tx[~mask7]
            print(f"  {date}: corr={corr:.3f}  n_aligned={len(tm_a):>7d}  "
                  f"TXFD6 spread when TMFD6>=7: median={np.median(tx_sp_when_tm7):.1f} "
                  f"(n={mask7.sum()})  otherwise: median={np.median(tx_sp_otherwise):.1f}")
        else:
            print(f"  {date}: corr={corr:.3f}  n_aligned={len(tm_a):>7d}  "
                  f"NO TMFD6 spread >= 7 events")

    if all_corr:
        print(f"\n  Overall mean correlation: {np.mean(all_corr):.3f} "
              f"(std={np.std(all_corr):.3f})")

    # ===================================================================
    # Part 3: TXFD6 forward realized range conditional on TMFD6 spread > 7
    # ===================================================================
    print("\n" + "=" * 80)
    print("PART 3: TXFD6 Forward Realized Range after TMFD6 Spread >= 7 Trigger")
    print("=" * 80)

    for threshold in [5, 7, 10]:
        print(f"\n  --- Threshold: spread >= {threshold} ---")
        all_fwd_ranges = []
        all_base_ranges = []

        for date in OVERLAP_DATES:
            tm = load_l1(TMFD6_DIR, "TMFD6", date)
            tx = load_l1(TXFD6_DIR, "TXFD6", date)
            sp_tm = spread_pts(tm)

            episodes = detect_episodes(sp_tm, tm["local_ts"], float(threshold))
            if not episodes:
                continue

            for start_idx, end_idx, dur_ns, mean_sp in episodes:
                trigger_ts = tm["local_ts"][start_idx]

                # Forward window: 5min, 30min, 60min after trigger
                for fwd_label, fwd_ns in [("5min", 300e9), ("30min", 1800e9), ("60min", 3600e9)]:
                    mask_fwd = (tx["local_ts"] >= trigger_ts) & (tx["local_ts"] <= trigger_ts + fwd_ns)
                    if mask_fwd.sum() < 10:
                        continue
                    tx_fwd = tx[mask_fwd]
                    hi = tx_fwd["ask_px"].max()
                    lo = tx_fwd["bid_px"].min()
                    rng = hi - lo
                    all_fwd_ranges.append((date, fwd_label, rng, mean_sp))

            # Baseline: random 5min/30min/60min windows on same day
            n_tm = len(tm)
            for _ in range(min(10, len(episodes))):
                rand_idx = np.random.randint(0, n_tm)
                rand_ts = tm["local_ts"][rand_idx]
                for fwd_label, fwd_ns in [("5min", 300e9), ("30min", 1800e9), ("60min", 3600e9)]:
                    mask_fwd = (tx["local_ts"] >= rand_ts) & (tx["local_ts"] <= rand_ts + fwd_ns)
                    if mask_fwd.sum() < 10:
                        continue
                    tx_fwd = tx[mask_fwd]
                    hi = tx_fwd["ask_px"].max()
                    lo = tx_fwd["bid_px"].min()
                    rng = hi - lo
                    all_base_ranges.append((date, fwd_label, rng))

        # Summarize
        if not all_fwd_ranges:
            print(f"    No episodes found at threshold {threshold}")
            continue

        for fwd_label in ["5min", "30min", "60min"]:
            cond = [r[2] for r in all_fwd_ranges if r[1] == fwd_label]
            base = [r[2] for r in all_base_ranges if r[1] == fwd_label]
            if cond and base:
                print(f"    {fwd_label}: CONDITIONAL range: "
                      f"median={np.median(cond):.1f} mean={np.mean(cond):.1f} "
                      f"P75={np.percentile(cond, 75):.1f} P90={np.percentile(cond, 90):.1f} pts  "
                      f"(n={len(cond)})")
                print(f"    {fwd_label}: BASELINE range:    "
                      f"median={np.median(base):.1f} mean={np.mean(base):.1f} "
                      f"P75={np.percentile(base, 75):.1f} P90={np.percentile(base, 90):.1f} pts  "
                      f"(n={len(base)})")
                ratio = np.median(cond) / np.median(base) if np.median(base) > 0 else float("inf")
                print(f"    {fwd_label}: Conditional / Baseline ratio: {ratio:.2f}x")

    # ===================================================================
    # Part 4: TXFD6 forward realized vol (std of returns) conditional
    # ===================================================================
    print("\n" + "=" * 80)
    print("PART 4: TXFD6 Forward Realized Vol (5s return std) after TMFD6 Spread >= 7")
    print("=" * 80)

    for threshold in [7, 10]:
        cond_vols = []
        base_vols = []

        for date in OVERLAP_DATES:
            tm = load_l1(TMFD6_DIR, "TMFD6", date)
            tx = load_l1(TXFD6_DIR, "TXFD6", date)
            sp_tm = spread_pts(tm)

            episodes = detect_episodes(sp_tm, tm["local_ts"], float(threshold))

            # Compute 5s returns for TXFD6
            # Resample to 5s bars
            ts_tx = tx["local_ts"]
            mid_tx = tx["mid_price"]
            if len(ts_tx) < 100:
                continue

            t0 = ts_tx[0]
            bar_size_ns = 5_000_000_000  # 5 seconds
            n_bars = int((ts_tx[-1] - t0) / bar_size_ns) + 1
            bar_mids = np.full(n_bars, np.nan)
            bar_ts = np.arange(n_bars) * bar_size_ns + t0

            for i in range(n_bars):
                mask = (ts_tx >= bar_ts[i]) & (ts_tx < bar_ts[i] + bar_size_ns)
                if mask.any():
                    bar_mids[i] = mid_tx[mask][-1]  # last price in bar

            # Forward-fill NaNs
            for i in range(1, len(bar_mids)):
                if np.isnan(bar_mids[i]):
                    bar_mids[i] = bar_mids[i - 1]

            returns_5s = np.diff(bar_mids)
            returns_5s = returns_5s[~np.isnan(returns_5s)]

            for start_idx, end_idx, dur_ns, mean_sp in episodes:
                trigger_ts = tm["local_ts"][start_idx]
                # Forward 30min RV
                fwd_30min = 360  # 30min / 5s = 360 bars
                bar_idx = int((trigger_ts - t0) / bar_size_ns)
                if bar_idx < 0 or bar_idx + fwd_30min >= len(returns_5s):
                    continue
                rv = np.std(returns_5s[bar_idx:bar_idx + fwd_30min])
                cond_vols.append(rv)

            # Baseline
            for _ in range(min(10, max(1, len(episodes)))):
                bar_idx = np.random.randint(0, max(1, len(returns_5s) - 360))
                rv = np.std(returns_5s[bar_idx:bar_idx + 360])
                base_vols.append(rv)

        if cond_vols and base_vols:
            print(f"\n  Threshold >= {threshold}:")
            print(f"    Conditional RV (30min, 5s returns std): "
                  f"median={np.median(cond_vols):.3f} mean={np.mean(cond_vols):.3f} "
                  f"P75={np.percentile(cond_vols, 75):.3f} (n={len(cond_vols)})")
            print(f"    Baseline RV (30min, 5s returns std):    "
                  f"median={np.median(base_vols):.3f} mean={np.mean(base_vols):.3f} "
                  f"P75={np.percentile(base_vols, 75):.3f} (n={len(base_vols)})")
            ratio = np.median(cond_vols) / np.median(base_vols) if np.median(base_vols) > 0 else 0
            print(f"    Conditional / Baseline ratio: {ratio:.2f}x")
        else:
            print(f"\n  Threshold >= {threshold}: insufficient data")

    # ===================================================================
    # Part 5: Episode characterization — what happens during spread > 7?
    # ===================================================================
    print("\n" + "=" * 80)
    print("PART 5: Episode Characterization — TXFD6 move during TMFD6 spread > 7 episode")
    print("=" * 80)

    for threshold in [7, 10]:
        episode_moves = []

        for date in OVERLAP_DATES:
            tm = load_l1(TMFD6_DIR, "TMFD6", date)
            tx = load_l1(TXFD6_DIR, "TXFD6", date)
            sp_tm = spread_pts(tm)

            episodes = detect_episodes(sp_tm, tm["local_ts"], float(threshold))

            for start_idx, end_idx, dur_ns, mean_sp in episodes:
                ep_start_ts = tm["local_ts"][start_idx]
                ep_end_ts = tm["local_ts"][min(end_idx, len(tm) - 1)]

                mask = (tx["local_ts"] >= ep_start_ts) & (tx["local_ts"] <= ep_end_ts)
                if mask.sum() < 5:
                    continue

                tx_ep = tx[mask]
                hi = tx_ep["mid_price"].max()
                lo = tx_ep["mid_price"].min()
                rng = hi - lo
                direction = tx_ep["mid_price"][-1] - tx_ep["mid_price"][0]
                dur_s = dur_ns / 1e9

                episode_moves.append({
                    "date": date, "dur_s": dur_s, "range": rng,
                    "direction": direction, "mean_spread": mean_sp,
                })

        if episode_moves:
            ranges = [e["range"] for e in episode_moves]
            dirs = [e["direction"] for e in episode_moves]
            durs = [e["dur_s"] for e in episode_moves]
            print(f"\n  Threshold >= {threshold}: {len(episode_moves)} episodes across {len(OVERLAP_DATES)} days")
            print(f"    Range:     median={np.median(ranges):6.1f}  mean={np.mean(ranges):6.1f}  "
                  f"P75={np.percentile(ranges, 75):6.1f}  P90={np.percentile(ranges, 90):6.1f} pts")
            print(f"    Duration:  median={np.median(durs):6.1f}  mean={np.mean(durs):6.1f}  "
                  f"P75={np.percentile(durs, 75):6.1f}  P90={np.percentile(durs, 90):6.1f} s")
            print(f"    |Direction|: median={np.median(np.abs(dirs)):6.1f}  "
                  f"mean={np.mean(np.abs(dirs)):6.1f} pts")
            pct_positive = sum(1 for d in dirs if d > 0) / len(dirs) * 100
            print(f"    Direction bias: {pct_positive:.1f}% positive, "
                  f"{100-pct_positive:.1f}% negative")

            # Breakeven analysis for straddle
            print(f"\n    --- Straddle Breakeven Analysis ---")
            for straddle_cost in [40, 80, 120, 150]:
                pct_covering = sum(1 for r in ranges if r >= straddle_cost) / len(ranges) * 100
                print(f"    Episodes with range >= {straddle_cost} pts (straddle cost): "
                      f"{pct_covering:.1f}% ({sum(1 for r in ranges if r >= straddle_cost)}/{len(ranges)})")
        else:
            print(f"\n  Threshold >= {threshold}: no episodes with TXFD6 data")

    # ===================================================================
    # Part 6: CK data check — do we have more TXFD6 data?
    # ===================================================================
    print("\n" + "=" * 80)
    print("PART 6: Additional TXFD6 data in CK export")
    print("=" * 80)

    ck_dir = Path("research/data/ck_export/TXFD6")
    if ck_dir.exists():
        ck_files = sorted(ck_dir.glob("*.npz")) + sorted(ck_dir.glob("*.npy"))
        print(f"  Found {len(ck_files)} files in {ck_dir}")
        for f in ck_files[:10]:
            print(f"    {f.name}")
    else:
        print(f"  {ck_dir} does not exist")

    # Also check for night session data
    ck_night = Path("research/data/ck_export")
    if ck_night.exists():
        for subdir in sorted(ck_night.iterdir()):
            if subdir.is_dir():
                n_files = len(list(subdir.glob("*")))
                print(f"  {subdir.name}: {n_files} files")

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    np.random.seed(42)
    main()
