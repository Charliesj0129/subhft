"""TMFD6 Reversal Frequency Analysis — BLOCKER-E4 for R18 Direction A.

Measures:
1. OBI prediction accuracy at L1
2. Reversal rate (OBI incorrectly predicts direction)
3. Reversal rate by spread bucket and time-of-day
4. Trade-side classification feasibility (tick rule)

Queries ClickHouse in daily batches to avoid memory issues.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

try:
    import clickhouse_connect
except ImportError:
    print("ERROR: clickhouse_connect not installed. Run: pip install clickhouse-connect")
    sys.exit(1)


SCALE = 1_000_000  # price scale factor in ClickHouse for TMFD6
SYMBOL = "TMFD6"

# OBI thresholds to test
OBI_THRESHOLDS = [0.0, 0.1, 0.2, 0.3, 0.5]

# Forward horizons in nanoseconds
HORIZONS_NS = {
    "1s": 1_000_000_000,
    "5s": 5_000_000_000,
    "30s": 30_000_000_000,
}

# Spread buckets in raw units (x SCALE)
SPREAD_BUCKETS_PTS = [
    (0, 4, "0-3"),
    (4, 5, "4"),
    (5, 10, "5-9"),
    (10, 20, "10-19"),
    (20, 100, "20-99"),
    (100, 999999, "100+"),
]


@dataclass
class ReversalStats:
    total: int = 0
    obi_positive: int = 0  # OBI > threshold (predicts UP)
    obi_negative: int = 0  # OBI < -threshold (predicts DOWN)
    obi_neutral: int = 0   # |OBI| <= threshold (no prediction)
    correct_up: int = 0    # OBI predicts UP and price goes UP
    correct_down: int = 0  # OBI predicts DOWN and price goes DOWN
    wrong_up: int = 0      # OBI predicts UP but price goes DOWN (reversal)
    wrong_down: int = 0    # OBI predicts DOWN but price goes UP (reversal)
    no_move: int = 0       # price doesn't move

    @property
    def predictions(self) -> int:
        return self.obi_positive + self.obi_negative

    @property
    def correct(self) -> int:
        return self.correct_up + self.correct_down

    @property
    def reversals(self) -> int:
        return self.wrong_up + self.wrong_down

    @property
    def reversal_rate(self) -> float:
        denom = self.correct + self.reversals
        return self.reversals / denom if denom > 0 else 0.0

    @property
    def accuracy(self) -> float:
        denom = self.correct + self.reversals
        return self.correct / denom if denom > 0 else 0.0


def get_trading_days(client) -> list[str]:
    """Get list of trading days with sufficient data."""
    result = client.query("""
        SELECT toDate(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) as day, count() as cnt
        FROM hft.market_data
        WHERE symbol = 'TMFD6' AND type = 'BidAsk'
        GROUP BY day HAVING cnt > 10000
        ORDER BY day
    """)
    return [str(row[0]) for row in result.result_rows]


def load_day_bidask(client, day: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Load BidAsk data for one day. Returns (ts, mid, spread, obi) arrays.

    Uses SETTINGS to limit memory and only selects needed columns.
    """
    result = client.query(f"""
        SELECT exch_ts, bids_price[1], bids_vol[1], asks_price[1], asks_vol[1]
        FROM hft.market_data
        WHERE symbol = 'TMFD6' AND type = 'BidAsk'
          AND toDate(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) = '{day}'
          AND bids_price[1] > 0 AND asks_price[1] > 0
          AND bids_vol[1] > 0 AND asks_vol[1] > 0
        ORDER BY exch_ts
        SETTINGS max_memory_usage = 3000000000, max_threads = 1
    """)
    rows = result.result_rows
    if not rows:
        return np.array([]), np.array([]), np.array([]), np.array([])

    ts = np.array([r[0] for r in rows], dtype=np.int64)
    bid_p = np.array([r[1] for r in rows], dtype=np.int64)
    bid_v = np.array([r[2] for r in rows], dtype=np.int64)
    ask_p = np.array([r[3] for r in rows], dtype=np.int64)
    ask_v = np.array([r[4] for r in rows], dtype=np.int64)

    mid = (bid_p + ask_p) / 2.0  # float for precision
    spread_pts = (ask_p - bid_p) / SCALE  # in points
    total_vol = (bid_v + ask_v).astype(np.float64)
    obi = np.where(total_vol > 0, (bid_v - ask_v) / total_vol, 0.0)

    return ts, mid, spread_pts, obi


def load_day_ticks(client, day: str) -> tuple[np.ndarray, np.ndarray]:
    """Load Tick data for one day. Returns (ts, price) arrays."""
    result = client.query(f"""
        SELECT exch_ts, price_scaled
        FROM hft.market_data
        WHERE symbol = 'TMFD6' AND type = 'Tick'
          AND toDate(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) = '{day}'
          AND price_scaled > 0
        ORDER BY exch_ts
    """)
    rows = result.result_rows
    if not rows:
        return np.array([]), np.array([])
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    price = np.array([r[1] for r in rows], dtype=np.int64)
    return ts, price


def find_forward_mid(ts: np.ndarray, mid: np.ndarray, idx: int, horizon_ns: int) -> float | None:
    """Find mid-price at ts[idx] + horizon_ns using binary search."""
    target_ts = ts[idx] + horizon_ns
    j = np.searchsorted(ts, target_ts, side="left")
    if j >= len(ts):
        return None
    # Use the first observation at or after the target timestamp
    return mid[j]


def compute_reversal_stats(
    ts: np.ndarray, mid: np.ndarray, spread_pts: np.ndarray, obi: np.ndarray,
    horizon_ns: int, obi_threshold: float,
    spread_lo: float = 0.0, spread_hi: float = 999999.0,
    sample_step: int = 10,
) -> ReversalStats:
    """Compute reversal stats for given parameters.

    sample_step: subsample every N events to reduce computation.
    """
    stats = ReversalStats()
    n = len(ts)

    for i in range(0, n, sample_step):
        sp = spread_pts[i]
        if sp < spread_lo or sp >= spread_hi:
            continue

        stats.total += 1
        ob = obi[i]

        if ob > obi_threshold:
            stats.obi_positive += 1
        elif ob < -obi_threshold:
            stats.obi_negative += 1
        else:
            stats.obi_neutral += 1
            continue  # no prediction made

        future_mid = find_forward_mid(ts, mid, i, horizon_ns)
        if future_mid is None:
            stats.total -= 1
            if ob > obi_threshold:
                stats.obi_positive -= 1
            else:
                stats.obi_negative -= 1
            continue

        delta = future_mid - mid[i]
        if delta == 0:
            stats.no_move += 1
            continue

        if ob > obi_threshold:
            if delta > 0:
                stats.correct_up += 1
            else:
                stats.wrong_up += 1
        else:  # ob < -threshold
            if delta < 0:
                stats.correct_down += 1
            else:
                stats.wrong_down += 1

    return stats


def ts_to_hour_minute(ts_ns: int) -> tuple[int, int]:
    """Convert nanosecond timestamp to (hour, minute) in Asia/Taipei (UTC+8)."""
    sec = ts_ns // 1_000_000_000
    sec_utc8 = sec + 8 * 3600
    sec_of_day = sec_utc8 % 86400
    return sec_of_day // 3600, (sec_of_day % 3600) // 60


def compute_reversal_by_time(
    ts: np.ndarray, mid: np.ndarray, spread_pts: np.ndarray, obi: np.ndarray,
    horizon_ns: int, obi_threshold: float,
    sample_step: int = 10,
) -> dict[str, ReversalStats]:
    """Compute reversal stats bucketed by time of day."""
    time_buckets = {
        "08:45-09:15": (8*60+45, 9*60+15),
        "09:15-10:00": (9*60+15, 10*60),
        "10:00-11:00": (10*60, 11*60),
        "11:00-12:00": (11*60, 12*60),
        "12:00-13:00": (12*60, 13*60),
        "13:00-13:45": (13*60, 13*60+45),
    }
    results: dict[str, ReversalStats] = {k: ReversalStats() for k in time_buckets}
    n = len(ts)

    for i in range(0, n, sample_step):
        h, m = ts_to_hour_minute(ts[i])
        hm = h * 60 + m
        bucket_key = None
        for key, (lo, hi) in time_buckets.items():
            if lo <= hm < hi:
                bucket_key = key
                break
        if bucket_key is None:
            continue

        ob = obi[i]
        if abs(ob) <= obi_threshold:
            results[bucket_key].total += 1
            results[bucket_key].obi_neutral += 1
            continue

        future_mid = find_forward_mid(ts, mid, i, horizon_ns)
        if future_mid is None:
            continue

        results[bucket_key].total += 1
        delta = future_mid - mid[i]

        if ob > obi_threshold:
            results[bucket_key].obi_positive += 1
            if delta > 0:
                results[bucket_key].correct_up += 1
            elif delta < 0:
                results[bucket_key].wrong_up += 1
            else:
                results[bucket_key].no_move += 1
        else:
            results[bucket_key].obi_negative += 1
            if delta < 0:
                results[bucket_key].correct_down += 1
            elif delta > 0:
                results[bucket_key].wrong_down += 1
            else:
                results[bucket_key].no_move += 1

    return results


def check_tick_rule_feasibility(client, days: list[str]) -> dict:
    """Check if tick rule (uptick=buy, downtick=sell) is feasible."""
    total_ticks = 0
    classified = 0
    zero_ticks = 0

    for day in days[:5]:  # sample 5 days
        ts, price = load_day_ticks(client, day)
        if len(ts) < 2:
            continue
        total_ticks += len(ts) - 1
        diffs = np.diff(price)
        classified += np.count_nonzero(diffs)
        zero_ticks += np.count_nonzero(diffs == 0)

    return {
        "total_ticks": total_ticks,
        "classified": classified,
        "zero_ticks": zero_ticks,
        "classification_rate": classified / total_ticks if total_ticks > 0 else 0,
        "avg_ticks_per_day": total_ticks / min(5, len(days)) if days else 0,
    }


def main():
    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="default", password="changeme"
    )
    days = get_trading_days(client)
    print(f"Trading days with >10k BidAsk events: {len(days)}")
    print(f"Days: {days[0]} to {days[-1]}")

    # ── Overall spread distribution ──
    print("\n=== SPREAD DISTRIBUTION ===")
    all_spreads = []
    for day in days:
        _, _, spread_pts, _ = load_day_bidask(client, day)
        if len(spread_pts) > 0:
            all_spreads.append(spread_pts)
    spreads = np.concatenate(all_spreads)
    print(f"Total BidAsk events: {len(spreads):,}")
    print(f"Spread (pts): median={np.median(spreads):.1f}, mean={np.mean(spreads):.1f}, "
          f"P25={np.percentile(spreads, 25):.1f}, P75={np.percentile(spreads, 75):.1f}")
    wide_pct = np.mean(spreads >= 5) * 100
    print(f"Spread >= 5 pts: {wide_pct:.1f}%")
    for lo, hi, label in SPREAD_BUCKETS_PTS:
        pct = np.mean((spreads >= lo) & (spreads < hi)) * 100
        print(f"  Spread {label}: {pct:.1f}%")

    # ── Reversal analysis: by threshold × horizon ──
    print("\n=== REVERSAL FREQUENCY (all spreads) ===")
    # Use threshold 0.0 and 0.2 as primary; horizon 1s and 5s
    primary_combos = [(0.0, "1s"), (0.0, "5s"), (0.0, "30s"),
                      (0.2, "1s"), (0.2, "5s"), (0.2, "30s")]

    agg_stats: dict[tuple, ReversalStats] = {}
    for combo in primary_combos:
        agg_stats[combo] = ReversalStats()

    for day in days:
        ts, mid, spread_pts, obi = load_day_bidask(client, day)
        if len(ts) < 100:
            continue
        print(f"  Processing {day}: {len(ts):,} events", flush=True)

        for obi_thr, hz_label in primary_combos:
            hz_ns = HORIZONS_NS[hz_label]
            s = compute_reversal_stats(ts, mid, spread_pts, obi, hz_ns, obi_thr, sample_step=5)
            agg = agg_stats[(obi_thr, hz_label)]
            agg.total += s.total
            agg.obi_positive += s.obi_positive
            agg.obi_negative += s.obi_negative
            agg.obi_neutral += s.obi_neutral
            agg.correct_up += s.correct_up
            agg.correct_down += s.correct_down
            agg.wrong_up += s.wrong_up
            agg.wrong_down += s.wrong_down
            agg.no_move += s.no_move

    print(f"\n{'OBI_thr':>8} {'Horizon':>8} {'Predictions':>12} {'Accuracy':>10} {'Reversal%':>10} {'NoMove%':>10} {'Reversals':>10}")
    for (obi_thr, hz_label), s in agg_stats.items():
        denom = s.correct + s.reversals
        no_move_pct = s.no_move / s.predictions * 100 if s.predictions > 0 else 0
        print(f"{obi_thr:>8.1f} {hz_label:>8} {s.predictions:>12,} {s.accuracy:>10.1%} "
              f"{s.reversal_rate:>10.1%} {no_move_pct:>10.1%} {s.reversals:>10,}")

    # ── Reversal by spread bucket (OBI_thr=0.0, horizon=5s) ──
    print("\n=== REVERSAL BY SPREAD BUCKET (OBI>0.0, horizon=5s) ===")
    spread_bucket_stats: dict[str, ReversalStats] = {}
    for _, _, label in SPREAD_BUCKETS_PTS:
        spread_bucket_stats[label] = ReversalStats()

    for day in days:
        ts, mid, spread_pts, obi = load_day_bidask(client, day)
        if len(ts) < 100:
            continue
        for lo, hi, label in SPREAD_BUCKETS_PTS:
            s = compute_reversal_stats(ts, mid, spread_pts, obi, HORIZONS_NS["5s"], 0.0,
                                       spread_lo=lo, spread_hi=hi, sample_step=10)
            agg = spread_bucket_stats[label]
            agg.total += s.total
            agg.obi_positive += s.obi_positive
            agg.obi_negative += s.obi_negative
            agg.obi_neutral += s.obi_neutral
            agg.correct_up += s.correct_up
            agg.correct_down += s.correct_down
            agg.wrong_up += s.wrong_up
            agg.wrong_down += s.wrong_down
            agg.no_move += s.no_move

    print(f"{'Spread':>10} {'Predictions':>12} {'Accuracy':>10} {'Reversal%':>10} {'NoMove%':>10} {'Reversals':>10}")
    for label in [l for _, _, l in SPREAD_BUCKETS_PTS]:
        s = spread_bucket_stats[label]
        denom = s.correct + s.reversals
        no_move_pct = s.no_move / s.predictions * 100 if s.predictions > 0 else 0
        print(f"{label:>10} {s.predictions:>12,} {s.accuracy:>10.1%} "
              f"{s.reversal_rate:>10.1%} {no_move_pct:>10.1%} {s.reversals:>10,}")

    # ── Reversal by time of day (OBI>0.0, horizon=5s) ──
    print("\n=== REVERSAL BY TIME OF DAY (OBI>0.0, horizon=5s) ===")
    time_agg: dict[str, ReversalStats] = {}
    for day in days:
        ts, mid, spread_pts, obi = load_day_bidask(client, day)
        if len(ts) < 100:
            continue
        day_time = compute_reversal_by_time(ts, mid, spread_pts, obi, HORIZONS_NS["5s"], 0.0, sample_step=10)
        for k, s in day_time.items():
            if k not in time_agg:
                time_agg[k] = ReversalStats()
            agg = time_agg[k]
            agg.total += s.total
            agg.obi_positive += s.obi_positive
            agg.obi_negative += s.obi_negative
            agg.obi_neutral += s.obi_neutral
            agg.correct_up += s.correct_up
            agg.correct_down += s.correct_down
            agg.wrong_up += s.wrong_up
            agg.wrong_down += s.wrong_down
            agg.no_move += s.no_move

    print(f"{'Period':>15} {'Predictions':>12} {'Accuracy':>10} {'Reversal%':>10} {'NoMove%':>10}")
    for k in sorted(time_agg.keys()):
        s = time_agg[k]
        denom = s.correct + s.reversals
        no_move_pct = s.no_move / s.predictions * 100 if s.predictions > 0 else 0
        print(f"{k:>15} {s.predictions:>12,} {s.accuracy:>10.1%} "
              f"{s.reversal_rate:>10.1%} {no_move_pct:>10.1%}")

    # ── Tick rule feasibility (BLOCKER-E5) ──
    print("\n=== TICK RULE FEASIBILITY (BLOCKER-E5) ===")
    tick_info = check_tick_rule_feasibility(client, days)
    print(f"Total ticks sampled (5 days): {tick_info['total_ticks']:,}")
    print(f"Classified (uptick/downtick): {tick_info['classified']:,} ({tick_info['classification_rate']:.1%})")
    print(f"Zero-tick (unclassifiable): {tick_info['zero_ticks']:,} ({tick_info['zero_ticks']/max(1,tick_info['total_ticks']):.1%})")
    print(f"Avg ticks/day: {tick_info['avg_ticks_per_day']:.0f}")

    client.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
