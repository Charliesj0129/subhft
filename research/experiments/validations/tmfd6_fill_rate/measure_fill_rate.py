"""
BLOCKER-E2: TMFD6 Fill Rate at Wide Spreads

Simulates a hypothetical retail maker posting 1-lot at the touch during
wide-spread episodes (spread >= 5 pts) and measures fill rate, time-to-fill,
and winner's curse.

Simplification Assumptions:
1. FIFO queue priority (realistic for TAIFEX)
2. 36ms latency before our order enters queue (broker RTT)
3. Post on ONE side per episode (shorter queue side)
4. Treat any fill as 1 lot (no partial fills)
5. Volume field is always 0 (quote-only L1 data), so we infer
   fills from queue depletion: same-price + qty decrease at L1.
6. Queue cancellations (qty decrease not from fills) are
   indistinguishable from fills in L1 data — this OVERSTATES
   our fill rate (conservative for a kill gate).
7. Order cancelled if spread narrows below 5 or price moves away.

Data: research/data/raw/tmfd6/TMFD6_all_l1.npy (7.75M rows, ~20 days)
Prices are raw index points (NOT x10000 scaled).
"""

import numpy as np
from datetime import datetime
from collections import defaultdict
import sys


def load_data():
    path = "research/data/raw/tmfd6/TMFD6_all_l1.npy"
    d = np.load(path, mmap_mode="r")
    return d


def get_trading_days(local_ts):
    """Split data into trading days based on calendar date boundaries.

    Uses Asia/Taipei timezone (UTC+8) for TAIFEX trading hours.
    Returns list of boundary indices: [0, first_idx_of_day2, ..., len(data)]
    """
    UTC_OFFSET_NS = 8 * 3600 * 1_000_000_000  # UTC+8 for Taiwan
    SECONDS_PER_DAY = 86400

    day_boundaries = [0]
    prev_day = (local_ts[0] + UTC_OFFSET_NS) // (SECONDS_PER_DAY * 1_000_000_000)

    for i in range(1, len(local_ts)):
        curr_day = (local_ts[i] + UTC_OFFSET_NS) // (SECONDS_PER_DAY * 1_000_000_000)
        if curr_day != prev_day:
            day_boundaries.append(i)
            prev_day = curr_day

    day_boundaries.append(len(local_ts))
    return day_boundaries


def find_wide_spread_episodes(spread_pts, local_ts, min_spread=5):
    """Find contiguous periods where spread >= min_spread pts.

    Returns list of (start_idx, end_idx) where spread is wide.
    end_idx is the first index where spread < min_spread (exclusive).
    """
    is_wide = spread_pts >= min_spread
    episodes = []
    in_episode = False
    start = 0

    for i in range(len(is_wide)):
        if is_wide[i] and not in_episode:
            start = i
            in_episode = True
        elif not is_wide[i] and in_episode:
            episodes.append((start, i))
            in_episode = False

    if in_episode:
        episodes.append((start, len(is_wide)))

    return episodes


def simulate_fill_rate(data):
    """Main simulation loop."""
    bid_px = np.array(data["bid_px"], dtype=np.float64)
    ask_px = np.array(data["ask_px"], dtype=np.float64)
    bid_qty = np.array(data["bid_qty"], dtype=np.float64)
    ask_qty = np.array(data["ask_qty"], dtype=np.float64)
    mid_price = np.array(data["mid_price"], dtype=np.float64)
    local_ts = np.array(data["local_ts"], dtype=np.int64)
    spread_pts = ask_px - bid_px

    n = len(data)
    print(f"Total rows: {n:,}")
    print(f"Date range: {datetime.fromtimestamp(local_ts[0]/1e9)} to {datetime.fromtimestamp(local_ts[-1]/1e9)}")

    # Get trading day boundaries
    day_bounds = get_trading_days(local_ts)
    n_days = len(day_bounds) - 1
    print(f"Trading days detected: {n_days}")

    # Find wide-spread episodes
    episodes = find_wide_spread_episodes(spread_pts, local_ts, min_spread=5)
    print(f"Wide-spread episodes (spread >= 5 pts): {len(episodes):,}")

    LATENCY_NS = 36_000_000  # 36ms broker RTT

    # Results storage
    results = []

    for ep_start, ep_end in episodes:
        if ep_end - ep_start < 3:
            # Skip trivially short episodes (< 3 ticks)
            continue

        ep_duration_s = (local_ts[ep_end - 1] - local_ts[ep_start]) / 1e9

        # Choose side with shorter queue at episode start
        if bid_qty[ep_start] <= ask_qty[ep_start]:
            side = "buy"
            our_price = bid_px[ep_start]
            initial_queue = bid_qty[ep_start]
        else:
            side = "sell"
            our_price = ask_px[ep_start]
            initial_queue = ask_qty[ep_start]

        # Simulate 36ms latency — find first tick after latency
        entry_ts = local_ts[ep_start] + LATENCY_NS
        entry_idx = ep_start
        while entry_idx < ep_end and local_ts[entry_idx] < entry_ts:
            entry_idx += 1

        if entry_idx >= ep_end:
            # Episode ended before our order could enter
            results.append({
                "ep_start": ep_start,
                "ep_end": ep_end,
                "duration_s": ep_duration_s,
                "side": side,
                "our_price": our_price,
                "initial_queue": initial_queue,
                "spread_at_entry": spread_pts[ep_start],
                "filled": False,
                "fill_idx": None,
                "time_to_fill_s": None,
                "mid_at_fill": None,
            })
            continue

        # Queue position at entry = current depth at our price level + 1 (us)
        # We join the BACK of the queue
        if side == "buy":
            queue_pos = bid_qty[entry_idx] if bid_px[entry_idx] == our_price else -1
        else:
            queue_pos = ask_qty[entry_idx] if ask_px[entry_idx] == our_price else -1

        if queue_pos < 0:
            # Price already moved away before we could enter
            results.append({
                "ep_start": ep_start,
                "ep_end": ep_end,
                "duration_s": ep_duration_s,
                "side": side,
                "our_price": our_price,
                "initial_queue": initial_queue,
                "spread_at_entry": spread_pts[ep_start],
                "filled": False,
                "fill_idx": None,
                "time_to_fill_s": None,
                "mid_at_fill": None,
            })
            continue

        # Track queue advancement
        filled = False
        fill_idx = None
        prev_qty = queue_pos

        for i in range(entry_idx + 1, ep_end):
            if side == "buy":
                current_price = bid_px[i]
                current_qty = bid_qty[i]
            else:
                current_price = ask_px[i]
                current_qty = ask_qty[i]

            # Price moved away — our order is stale, cancel
            if current_price != our_price:
                break

            # Spread narrowed below threshold — episode ending
            if spread_pts[i] < 5:
                break

            # Queue depletion at our price level
            if current_qty < prev_qty:
                qty_consumed = prev_qty - current_qty
                queue_pos -= qty_consumed
                if queue_pos <= 0:
                    filled = True
                    fill_idx = i
                    break

            # Queue additions (new orders behind us) don't affect our position
            # but we track current qty for next iteration
            prev_qty = current_qty

        mid_at_fill = mid_price[fill_idx] if filled and fill_idx is not None else None

        results.append({
            "ep_start": ep_start,
            "ep_end": ep_end,
            "duration_s": ep_duration_s,
            "side": side,
            "our_price": our_price,
            "initial_queue": initial_queue,
            "spread_at_entry": spread_pts[ep_start],
            "filled": filled,
            "fill_idx": fill_idx,
            "time_to_fill_s": (local_ts[fill_idx] - local_ts[ep_start]) / 1e9 if filled else None,
            "mid_at_fill": mid_at_fill,
        })

    return results, data, local_ts, mid_price, day_bounds, n_days, spread_pts


def compute_winners_curse(results, local_ts, mid_price, n_total):
    """For filled orders, compute post-fill mid-price movement."""
    horizons_s = [1, 5, 30]
    horizons_ns = [h * 1_000_000_000 for h in horizons_s]

    curse_results = {h: {"adverse_count": 0, "total": 0, "movements": []} for h in horizons_s}

    filled = [r for r in results if r["filled"]]

    for r in filled:
        fill_idx = r["fill_idx"]
        fill_mid = r["mid_at_fill"]
        fill_ts = local_ts[fill_idx]
        side = r["side"]

        for h_s, h_ns in zip(horizons_s, horizons_ns):
            target_ts = fill_ts + h_ns
            # Find index at target timestamp
            # Binary search in local_ts from fill_idx
            future_idx = np.searchsorted(local_ts[fill_idx:min(fill_idx + 500000, n_total)], target_ts)
            future_idx += fill_idx

            if future_idx >= n_total:
                continue

            future_mid = mid_price[future_idx]
            movement = future_mid - fill_mid  # positive = price went up

            # Adverse for buyer = price went down after fill (bought, now worth less)
            # Adverse for seller = price went up after fill (sold, now worth more)
            if side == "buy":
                is_adverse = movement < 0
                adverse_magnitude = -movement if is_adverse else 0
            else:
                is_adverse = movement > 0
                adverse_magnitude = movement if is_adverse else 0

            curse_results[h_s]["total"] += 1
            if is_adverse:
                curse_results[h_s]["adverse_count"] += 1
            curse_results[h_s]["movements"].append(
                (movement if side == "sell" else -movement, adverse_magnitude)
            )

    return curse_results


def assign_day(ep_start_ts, day_bounds, local_ts):
    """Assign episode to a trading day."""
    for d in range(len(day_bounds) - 1):
        if day_bounds[d] <= 0 or day_bounds[d] >= len(local_ts):
            continue
        day_start_ts = local_ts[day_bounds[d]]
        if d + 1 < len(day_bounds):
            next_start = day_bounds[d + 1]
            if next_start >= len(local_ts):
                next_start = len(local_ts) - 1
            day_end_ts = local_ts[next_start]
        else:
            day_end_ts = local_ts[-1]

        if day_start_ts <= ep_start_ts <= day_end_ts:
            return d
    return -1


def main():
    print("=" * 70)
    print("BLOCKER-E2: TMFD6 Fill Rate at Wide Spreads")
    print("=" * 70)
    print()

    data = load_data()
    results, data, local_ts, mid_price, day_bounds, n_days, spread_pts = simulate_fill_rate(data)

    total_episodes = len(results)
    filled = [r for r in results if r["filled"]]
    not_filled = [r for r in results if not r["filled"]]
    n_filled = len(filled)
    fill_rate = n_filled / total_episodes * 100 if total_episodes > 0 else 0

    print(f"\n{'='*70}")
    print(f"FILL RATE RESULTS")
    print(f"{'='*70}")
    print(f"Total wide-spread episodes simulated: {total_episodes:,}")
    print(f"Filled: {n_filled:,}")
    print(f"Not filled: {len(not_filled):,}")
    print(f"Overall fill rate: {fill_rate:.1f}%")

    # Fill rate by spread bucket
    spread_buckets = [(5, 9), (10, 19), (20, 39), (40, float("inf"))]
    print(f"\n--- Fill Rate by Spread Bucket (points) ---")
    print(f"{'Bucket':>12} | {'Total':>8} | {'Filled':>8} | {'Rate':>8} | {'Avg Duration':>14}")
    print("-" * 65)
    for lo, hi in spread_buckets:
        label = f"[{lo}-{hi:.0f}]" if hi != float("inf") else f"[{lo}+]"
        bucket = [r for r in results if lo <= r["spread_at_entry"] <= hi]
        bucket_filled = [r for r in bucket if r["filled"]]
        rate = len(bucket_filled) / len(bucket) * 100 if bucket else 0
        avg_dur = np.mean([r["duration_s"] for r in bucket]) if bucket else 0
        print(f"{label:>12} | {len(bucket):>8,} | {len(bucket_filled):>8,} | {rate:>7.1f}% | {avg_dur:>12.1f}s")

    # Time-to-fill stats
    if filled:
        ttf = [r["time_to_fill_s"] for r in filled]
        print(f"\n--- Time-to-Fill (filled episodes only) ---")
        print(f"Median: {np.median(ttf):.2f}s")
        print(f"Mean:   {np.mean(ttf):.2f}s")
        print(f"P25:    {np.percentile(ttf, 25):.2f}s")
        print(f"P75:    {np.percentile(ttf, 75):.2f}s")
        print(f"P95:    {np.percentile(ttf, 95):.2f}s")

    # Fills per session
    fills_per_day = defaultdict(int)
    episodes_per_day = defaultdict(int)
    for r in results:
        ep_ts = local_ts[r["ep_start"]]
        day = assign_day(ep_ts, day_bounds, local_ts)
        episodes_per_day[day] += 1
        if r["filled"]:
            fills_per_day[day] += 1

    print(f"\n--- Fills per Trading Day ---")
    print(f"{'Day':>5} | {'Episodes':>10} | {'Fills':>8} | {'Rate':>8}")
    print("-" * 45)
    total_fills = 0
    for d in sorted(episodes_per_day.keys()):
        f = fills_per_day.get(d, 0)
        e = episodes_per_day[d]
        total_fills += f
        bound_idx = min(day_bounds[d], len(local_ts) - 1)
        day_date = datetime.fromtimestamp(local_ts[bound_idx] / 1e9).strftime("%m/%d")
        print(f"{day_date:>5} | {e:>10,} | {f:>8,} | {f/e*100 if e else 0:>7.1f}%")

    avg_fills_per_day = total_fills / n_days if n_days > 0 else 0
    print(f"\nTotal fills across all days: {total_fills:,}")
    print(f"Average fills/session: {avg_fills_per_day:.1f}")

    # Winner's curse
    print(f"\n{'='*70}")
    print(f"WINNER'S CURSE ANALYSIS")
    print(f"{'='*70}")
    curse = compute_winners_curse(results, local_ts, mid_price, len(data))

    for h_s in [1, 5, 30]:
        cr = curse[h_s]
        if cr["total"] == 0:
            print(f"\n--- {h_s}s horizon: No data ---")
            continue
        adverse_pct = cr["adverse_count"] / cr["total"] * 100
        movements = [m[0] for m in cr["movements"]]  # signed movement (negative = adverse)
        adverse_mags = [m[1] for m in cr["movements"] if m[1] > 0]
        avg_movement = np.mean(movements)
        avg_adverse = np.mean(adverse_mags) if adverse_mags else 0
        print(f"\n--- {h_s}s post-fill horizon ---")
        print(f"Total fills with {h_s}s lookahead: {cr['total']:,}")
        print(f"Adverse movement: {cr['adverse_count']:,} / {cr['total']:,} = {adverse_pct:.1f}%")
        print(f"Avg signed movement (neg=adverse): {avg_movement:.2f} pts")
        print(f"Avg adverse magnitude (when adverse): {avg_adverse:.2f} pts")

    # Kill gate check
    print(f"\n{'='*70}")
    print(f"KILL GATE ASSESSMENT")
    print(f"{'='*70}")

    kill_fills = avg_fills_per_day < 5
    print(f"Fills/session: {avg_fills_per_day:.1f} {'< 5 -> KILL' if kill_fills else '>= 5 -> PASS'}")

    # Winner's curse kill: if adverse at 5s > net spread capture for majority
    if curse[5]["total"] > 0:
        # Net spread capture = our_spread/2 - 2 pts (half RT cost)
        # For a maker filling at the touch during wide spread:
        # Spread capture = spread_at_entry / 2 (we capture half the spread)
        # Cost = 2 pts (half of 4 pt RT cost, since this is one leg)
        filled_spreads = [r["spread_at_entry"] for r in filled]
        avg_spread_capture = np.mean(filled_spreads) / 2 - 2  # half spread minus half RT cost
        adverse_at_5s = np.mean([m[1] for m in curse[5]["movements"]])
        kill_curse = adverse_at_5s > avg_spread_capture and curse[5]["adverse_count"] / curse[5]["total"] > 0.5
        print(f"Avg spread capture (half spread - half RT): {avg_spread_capture:.2f} pts")
        print(f"Avg adverse at 5s: {adverse_at_5s:.2f} pts")
        print(f"Adverse fraction at 5s: {curse[5]['adverse_count']/curse[5]['total']*100:.1f}%")
        print(f"Winner's curse kill: {'KILL' if kill_curse else 'PASS'}")
    else:
        kill_curse = False
        print("Winner's curse: insufficient data")

    overall = "KILL" if (kill_fills or kill_curse) else "PASS"
    print(f"\n>>> OVERALL RECOMMENDATION: {overall} <<<")

    # Detailed fill queue analysis
    if filled:
        print(f"\n{'='*70}")
        print(f"QUEUE DEPTH ANALYSIS")
        print(f"{'='*70}")
        queues = [r["initial_queue"] for r in filled]
        print(f"Initial queue depth at fill episodes:")
        print(f"  Mean: {np.mean(queues):.1f}, Median: {np.median(queues):.0f}")
        print(f"  P25: {np.percentile(queues, 25):.0f}, P75: {np.percentile(queues, 75):.0f}")

        # Spread at fill
        fill_spreads = [r["spread_at_entry"] for r in filled]
        print(f"Spread at filled episodes:")
        print(f"  Mean: {np.mean(fill_spreads):.1f}, Median: {np.median(fill_spreads):.0f}")

    return results, curse, avg_fills_per_day, fill_rate


if __name__ == "__main__":
    results, curse, avg_fills_per_day, fill_rate = main()
