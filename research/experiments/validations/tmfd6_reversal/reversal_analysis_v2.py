"""TMFD6 Reversal Frequency Analysis v2 — Memory-optimized.

BLOCKER-E4: Direction A (RCM) viability measurement.
BLOCKER-E5: Tick rule trade-side classification feasibility.

Processes data day-by-day with memory limits.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

import numpy as np

try:
    import clickhouse_connect
except ImportError:
    print("ERROR: clickhouse_connect not installed")
    sys.exit(1)

SCALE = 1_000_000  # TMFD6 price scale in ClickHouse
CH_SETTINGS = {"max_memory_usage": 2_000_000_000, "max_threads": 1}


@dataclass
class Stats:
    total: int = 0
    predictions: int = 0
    correct: int = 0
    reversals: int = 0
    no_move: int = 0

    @property
    def reversal_rate(self) -> float:
        d = self.correct + self.reversals
        return self.reversals / d if d > 0 else 0.0

    @property
    def accuracy(self) -> float:
        d = self.correct + self.reversals
        return self.correct / d if d > 0 else 0.0

    def merge(self, other: "Stats") -> None:
        self.total += other.total
        self.predictions += other.predictions
        self.correct += other.correct
        self.reversals += other.reversals
        self.no_move += other.no_move


def get_client():
    return clickhouse_connect.get_client(
        host="localhost", port=8123, username="default", password="changeme"
    )


def get_trading_days(client) -> list[str]:
    result = client.query("""
        SELECT toDate(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) as day, count() as cnt
        FROM hft.market_data
        WHERE symbol = 'TMFD6' AND type = 'BidAsk'
        GROUP BY day HAVING cnt > 10000
        ORDER BY day
    """, settings=CH_SETTINGS)
    return [str(row[0]) for row in result.result_rows]


def load_day(client, day: str):
    """Load BidAsk data for one day. Returns numpy arrays."""
    result = client.query(f"""
        SELECT exch_ts, bids_price[1], bids_vol[1], asks_price[1], asks_vol[1]
        FROM hft.market_data
        WHERE symbol = 'TMFD6' AND type = 'BidAsk'
          AND toDate(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) = '{day}'
          AND bids_price[1] > 0 AND asks_price[1] > 0
          AND bids_vol[1] > 0 AND asks_vol[1] > 0
        ORDER BY exch_ts
        SETTINGS max_memory_usage = 2000000000, max_threads = 1
    """)
    rows = result.result_rows
    if len(rows) < 100:
        return None
    n = len(rows)
    ts = np.empty(n, dtype=np.int64)
    bid_p = np.empty(n, dtype=np.int64)
    bid_v = np.empty(n, dtype=np.int64)
    ask_p = np.empty(n, dtype=np.int64)
    ask_v = np.empty(n, dtype=np.int64)
    for i, r in enumerate(rows):
        ts[i] = r[0]
        bid_p[i] = r[1]
        bid_v[i] = r[2]
        ask_p[i] = r[3]
        ask_v[i] = r[4]
    del rows  # free memory
    mid = (bid_p.astype(np.float64) + ask_p.astype(np.float64)) / 2.0
    spread_pts = (ask_p - bid_p).astype(np.float64) / SCALE
    total_vol = bid_v.astype(np.float64) + ask_v.astype(np.float64)
    obi = np.where(total_vol > 0, (bid_v.astype(np.float64) - ask_v.astype(np.float64)) / total_vol, 0.0)
    del bid_p, bid_v, ask_p, ask_v, total_vol
    return ts, mid, spread_pts, obi


def analyze_reversals(ts, mid, spread_pts, obi, horizon_ns, obi_thr,
                      spread_lo=0.0, spread_hi=999999.0, step=20):
    """Compute reversal stats with subsampling."""
    s = Stats()
    n = len(ts)
    for i in range(0, n, step):
        sp = spread_pts[i]
        if sp < spread_lo or sp >= spread_hi:
            continue
        s.total += 1
        ob = obi[i]
        if abs(ob) <= obi_thr:
            continue
        # find forward mid
        target = ts[i] + horizon_ns
        j = np.searchsorted(ts, target, side="left")
        if j >= n:
            continue
        s.predictions += 1
        delta = mid[j] - mid[i]
        if delta == 0:
            s.no_move += 1
            continue
        predicts_up = ob > obi_thr
        went_up = delta > 0
        if predicts_up == went_up:
            s.correct += 1
        else:
            s.reversals += 1
    return s


def ts_to_hm(ts_ns):
    sec = ts_ns // 1_000_000_000 + 8 * 3600
    sod = sec % 86400
    return sod // 3600, (sod % 3600) // 60


def analyze_by_time(ts, mid, spread_pts, obi, horizon_ns, obi_thr, step=20):
    """Reversal stats by time-of-day bucket."""
    buckets = [
        ("08:45-09:15", 8*60+45, 9*60+15),
        ("09:15-10:00", 9*60+15, 10*60),
        ("10:00-11:00", 10*60, 11*60),
        ("11:00-12:00", 11*60, 12*60),
        ("12:00-13:00", 12*60, 13*60),
        ("13:00-13:45", 13*60, 13*60+45),
    ]
    results = {name: Stats() for name, _, _ in buckets}
    n = len(ts)
    for i in range(0, n, step):
        h, m = ts_to_hm(ts[i])
        hm = h * 60 + m
        bname = None
        for name, lo, hi in buckets:
            if lo <= hm < hi:
                bname = name
                break
        if bname is None:
            continue
        ob = obi[i]
        if abs(ob) <= obi_thr:
            results[bname].total += 1
            continue
        target = ts[i] + horizon_ns
        j = np.searchsorted(ts, target, side="left")
        if j >= n:
            continue
        results[bname].total += 1
        results[bname].predictions += 1
        delta = mid[j] - mid[i]
        if delta == 0:
            results[bname].no_move += 1
            continue
        if (ob > obi_thr) == (delta > 0):
            results[bname].correct += 1
        else:
            results[bname].reversals += 1
    return results


def main():
    client = get_client()
    days = get_trading_days(client)
    print(f"Trading days: {len(days)} ({days[0]} to {days[-1]})")

    # Configurations
    horizons = {"1s": 1_000_000_000, "5s": 5_000_000_000, "30s": 30_000_000_000}
    obi_thrs = [0.0, 0.1, 0.2, 0.3, 0.5]
    spread_buckets = [
        ("0-3", 0, 4), ("4", 4, 5), ("5-9", 5, 10),
        ("10-19", 10, 20), ("20-99", 20, 100), ("100+", 100, 999999),
    ]

    # Accumulators
    # (obi_thr, horizon_label) -> Stats
    overall: dict[tuple, Stats] = {}
    for thr in obi_thrs:
        for hz in horizons:
            overall[(thr, hz)] = Stats()

    # (spread_label) -> Stats  (at obi=0.0, horizon=5s)
    by_spread: dict[str, Stats] = {lab: Stats() for lab, _, _ in spread_buckets}

    # (time_label) -> Stats  (at obi=0.0, horizon=5s)
    by_time: dict[str, Stats] = {}

    # Spread distribution
    all_spread_pts = []

    for day in days:
        print(f"  Loading {day}...", end=" ", flush=True)
        data = load_day(client, day)
        if data is None:
            print("skipped (too few events)")
            continue
        ts, mid, spread_pts, obi = data
        print(f"{len(ts):,} events", flush=True)

        # Spread stats
        all_spread_pts.append(spread_pts.copy())

        # Overall reversal by (obi_thr, horizon)
        for thr in obi_thrs:
            for hz_label, hz_ns in horizons.items():
                s = analyze_reversals(ts, mid, spread_pts, obi, hz_ns, thr, step=20)
                overall[(thr, hz_label)].merge(s)

        # By spread bucket (obi=0.0, horizon=5s)
        for lab, lo, hi in spread_buckets:
            s = analyze_reversals(ts, mid, spread_pts, obi, horizons["5s"], 0.0,
                                  spread_lo=lo, spread_hi=hi, step=20)
            by_spread[lab].merge(s)

        # By time of day (obi=0.0, horizon=5s)
        time_day = analyze_by_time(ts, mid, spread_pts, obi, horizons["5s"], 0.0, step=20)
        for k, s in time_day.items():
            if k not in by_time:
                by_time[k] = Stats()
            by_time[k].merge(s)

        del ts, mid, spread_pts, obi, data  # free

    # ── Print results ──

    print("\n" + "=" * 70)
    print("SPREAD DISTRIBUTION")
    print("=" * 70)
    sp = np.concatenate(all_spread_pts)
    print(f"Total events: {len(sp):,}")
    print(f"Median: {np.median(sp):.1f} pts, Mean: {np.mean(sp):.1f} pts")
    print(f"P25: {np.percentile(sp, 25):.1f}, P75: {np.percentile(sp, 75):.1f}")
    for lab, lo, hi in spread_buckets:
        pct = np.mean((sp >= lo) & (sp < hi)) * 100
        print(f"  {lab:>8}: {pct:5.1f}%")
    print(f"  >= 5 pts: {np.mean(sp >= 5)*100:.1f}%")
    del sp, all_spread_pts

    print("\n" + "=" * 70)
    print("REVERSAL FREQUENCY BY OBI THRESHOLD x HORIZON")
    print("=" * 70)
    print(f"{'OBI_thr':>8} {'Horizon':>8} {'Sampled':>10} {'Predict':>10} "
          f"{'Accuracy':>10} {'Reversal%':>10} {'NoMove%':>10} {'#Rev':>10}")
    for thr in obi_thrs:
        for hz in horizons:
            s = overall[(thr, hz)]
            nm_pct = s.no_move / s.predictions * 100 if s.predictions > 0 else 0
            print(f"{thr:>8.1f} {hz:>8} {s.total:>10,} {s.predictions:>10,} "
                  f"{s.accuracy:>10.1%} {s.reversal_rate:>10.1%} {nm_pct:>10.1%} {s.reversals:>10,}")

    print("\n" + "=" * 70)
    print("REVERSAL BY SPREAD BUCKET (OBI>0.0, horizon=5s)")
    print("=" * 70)
    print(f"{'Spread':>10} {'Sampled':>10} {'Predict':>10} "
          f"{'Accuracy':>10} {'Reversal%':>10} {'NoMove%':>10}")
    for lab, _, _ in spread_buckets:
        s = by_spread[lab]
        nm_pct = s.no_move / s.predictions * 100 if s.predictions > 0 else 0
        print(f"{lab:>10} {s.total:>10,} {s.predictions:>10,} "
              f"{s.accuracy:>10.1%} {s.reversal_rate:>10.1%} {nm_pct:>10.1%}")

    print("\n" + "=" * 70)
    print("REVERSAL BY TIME OF DAY (OBI>0.0, horizon=5s)")
    print("=" * 70)
    print(f"{'Period':>15} {'Sampled':>10} {'Predict':>10} "
          f"{'Accuracy':>10} {'Reversal%':>10} {'NoMove%':>10}")
    for k in sorted(by_time.keys()):
        s = by_time[k]
        nm_pct = s.no_move / s.predictions * 100 if s.predictions > 0 else 0
        print(f"{k:>15} {s.total:>10,} {s.predictions:>10,} "
              f"{s.accuracy:>10.1%} {s.reversal_rate:>10.1%} {nm_pct:>10.1%}")

    # ── Tick rule feasibility ──
    print("\n" + "=" * 70)
    print("TICK RULE FEASIBILITY (BLOCKER-E5)")
    print("=" * 70)
    total_ticks = 0
    classified = 0
    zero_ticks = 0
    for day in days[:5]:
        result = client.query(f"""
            SELECT price_scaled FROM hft.market_data
            WHERE symbol = 'TMFD6' AND type = 'Tick'
              AND toDate(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) = '{day}'
              AND price_scaled > 0
            ORDER BY exch_ts
            SETTINGS max_memory_usage = 1000000000, max_threads = 1
        """)
        prices = [r[0] for r in result.result_rows]
        if len(prices) < 2:
            continue
        p = np.array(prices, dtype=np.int64)
        d = np.diff(p)
        total_ticks += len(d)
        classified += np.count_nonzero(d)
        zero_ticks += np.count_nonzero(d == 0)

    print(f"Ticks sampled (5 days): {total_ticks:,}")
    print(f"Classified (uptick/downtick): {classified:,} ({classified/max(1,total_ticks):.1%})")
    print(f"Zero-tick (unclassifiable): {zero_ticks:,} ({zero_ticks/max(1,total_ticks):.1%})")
    print(f"Avg ticks/day: {total_ticks/5:.0f}")

    # ── Conditional: spread >= 5, reversal by horizon ──
    print("\n" + "=" * 70)
    print("REVERSAL AT SPREAD >= 5 ONLY (OBI>0.0)")
    print("=" * 70)
    wide_stats: dict[str, Stats] = {hz: Stats() for hz in horizons}
    for day in days:
        data = load_day(client, day)
        if data is None:
            continue
        ts, mid, spread_pts, obi = data
        for hz_label, hz_ns in horizons.items():
            s = analyze_reversals(ts, mid, spread_pts, obi, hz_ns, 0.0,
                                  spread_lo=5.0, spread_hi=999999.0, step=20)
            wide_stats[hz_label].merge(s)
        del ts, mid, spread_pts, obi, data

    print(f"{'Horizon':>8} {'Sampled':>10} {'Predict':>10} "
          f"{'Accuracy':>10} {'Reversal%':>10} {'NoMove%':>10}")
    for hz in horizons:
        s = wide_stats[hz]
        nm_pct = s.no_move / s.predictions * 100 if s.predictions > 0 else 0
        print(f"{hz:>8} {s.total:>10,} {s.predictions:>10,} "
              f"{s.accuracy:>10.1%} {s.reversal_rate:>10.1%} {nm_pct:>10.1%}")

    client.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
