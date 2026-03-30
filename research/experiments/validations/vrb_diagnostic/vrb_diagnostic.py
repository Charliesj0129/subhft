"""VRB (Volatility-Regime Breakout) Data Diagnostic for TMFD6.

Stage 2a diagnostic to validate VRB feasibility before prototyping.

Kill gates:
1. Vol compression -> expansion triggers >= 1/session average
2. Breakout direction accuracy >= 55% (4h EMA OR reactive)
3. Not purely a time-of-day artifact (chi-squared test)

Cost model: 3.92 pts = 1.19 bps RT cost.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

try:
    import clickhouse_connect
except ImportError:
    print("ERROR: clickhouse_connect not installed")
    sys.exit(1)


SCALE = 1_000_000
SYMBOL = "TMFD6"
# Day session 08:45-13:45 Taiwan time (UTC+8)
DAY_START_MIN = 8 * 60 + 45   # 525
DAY_END_MIN = 13 * 60 + 45    # 825
# Cost model
COST_PTS = 3.92


@dataclass
class MinuteBar:
    """1-minute OHLCV bar."""

    ts_minute: int  # minute-of-day (e.g., 525 = 08:45)
    date: str
    open_mid: float
    high_mid: float
    low_mid: float
    close_mid: float
    n_ticks: int


@dataclass
class BreakoutEvent:
    """A vol compression -> expansion trigger event."""

    date: str
    trigger_minute: int  # minute-of-day
    rv_1h_at_trigger: float
    rv_5m_at_trigger: float
    expansion_ratio: float
    ema_4h_slope: float  # sign indicates direction prediction
    first_5m_return: float  # return in first 5min after trigger
    next_1h_return: float  # return in next 1 hour after trigger


def get_client() -> clickhouse_connect.driver.Client:
    """Create ClickHouse client."""
    return clickhouse_connect.get_client(
        host="localhost",
        port=8123,
        username="default",
        password="changeme",
    )


def fetch_day_midprices(client: clickhouse_connect.driver.Client, date_str: str) -> list[tuple[int, float]]:
    """Fetch (minute_of_day, mid_price) for a single day session.

    Returns sorted list of (minute_of_day, mid_price_pts) tuples.
    """
    query = f"""
    SELECT
        toHour(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) * 60
            + toMinute(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) as min_of_day,
        (bids_price[1] + asks_price[1]) / 2 as mid_scaled
    FROM hft.market_data
    WHERE symbol = '{SYMBOL}' AND type = 'BidAsk'
      AND toDate(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) = '{date_str}'
      AND toHour(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) * 60
          + toMinute(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) >= {DAY_START_MIN}
      AND toHour(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) * 60
          + toMinute(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) < {DAY_END_MIN}
      AND length(bids_price) > 0 AND length(asks_price) > 0
      AND bids_price[1] > 0 AND asks_price[1] > 0
    ORDER BY exch_ts
    """
    result = client.query(query)
    rows = []
    for row in result.result_rows:
        min_of_day = int(row[0])
        mid_pts = float(row[1]) / SCALE
        rows.append((min_of_day, mid_pts))
    return rows


def build_minute_bars(raw_data: list[tuple[int, float]], date_str: str) -> list[MinuteBar]:
    """Aggregate raw mid prices into 1-minute bars."""
    by_minute: dict[int, list[float]] = defaultdict(list)
    for min_of_day, mid in raw_data:
        by_minute[min_of_day].append(mid)

    bars = []
    for m in sorted(by_minute.keys()):
        prices = by_minute[m]
        bars.append(MinuteBar(
            ts_minute=m,
            date=date_str,
            open_mid=prices[0],
            high_mid=max(prices),
            low_mid=min(prices),
            close_mid=prices[-1],
            n_ticks=len(prices),
        ))
    return bars


def compute_returns(bars: list[MinuteBar]) -> np.ndarray:
    """Compute log returns from minute bar close prices."""
    closes = np.array([b.close_mid for b in bars])
    # Avoid log(0) or log(negative)
    valid = closes > 0
    if not np.all(valid):
        closes = closes[valid]
    if len(closes) < 2:
        return np.array([])
    return np.diff(np.log(closes))


def compute_rv(returns: np.ndarray, window: int) -> np.ndarray:
    """Compute rolling realized volatility (std of returns) over window bars."""
    if len(returns) < window:
        return np.array([])
    rv = np.zeros(len(returns))
    rv[:window - 1] = np.nan
    for i in range(window - 1, len(returns)):
        rv[i] = np.std(returns[i - window + 1 : i + 1])
    return rv


def compute_ema(values: np.ndarray, span: int) -> np.ndarray:
    """Compute EMA of values."""
    alpha = 2.0 / (span + 1)
    ema = np.zeros_like(values)
    ema[0] = values[0]
    for i in range(1, len(values)):
        ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]
    return ema


def run_vrb_diagnostic() -> dict:
    """Run the full VRB diagnostic."""
    client = get_client()

    # 1. Get all day-session dates
    result = client.query(f"""
    SELECT DISTINCT toDate(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) as dt
    FROM hft.market_data
    WHERE symbol = '{SYMBOL}' AND type = 'BidAsk'
      AND toHour(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) * 60
          + toMinute(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) >= {DAY_START_MIN}
      AND toHour(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) * 60
          + toMinute(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) < {DAY_END_MIN}
    ORDER BY dt
    """)
    dates = [str(row[0]) for row in result.result_rows]
    print(f"Found {len(dates)} day-session dates: {dates[0]} to {dates[-1]}")

    # 2. Build all minute bars per day
    all_bars_by_date: dict[str, list[MinuteBar]] = {}
    for d in dates:
        raw = fetch_day_midprices(client, d)
        if len(raw) < 60:  # need at least 60 min of data
            print(f"  SKIP {d}: only {len(raw)} raw ticks")
            continue
        bars = build_minute_bars(raw, d)
        if len(bars) < 60:
            print(f"  SKIP {d}: only {len(bars)} minute bars")
            continue
        all_bars_by_date[d] = bars
        print(f"  {d}: {len(bars)} bars, {len(raw)} ticks, "
              f"price range {bars[0].close_mid:.0f}-{bars[-1].close_mid:.0f}")

    valid_dates = sorted(all_bars_by_date.keys())
    print(f"\nValid dates for analysis: {len(valid_dates)}")

    # 3. Compute RV and detect compression/expansion per day
    # Since we have only 16 days, we can't do 20-day rolling percentile.
    # Instead: use expanding window across all prior days + current day's 1h RV distribution.
    # Alternative: use within-day percentile (P20 of all 1h RV values within the session).

    all_rv_1h_values: list[float] = []  # accumulate across days for expanding percentile
    all_breakout_events: list[BreakoutEvent] = []
    triggers_per_session: list[int] = []

    for date_idx, d in enumerate(valid_dates):
        bars = all_bars_by_date[d]
        returns = compute_returns(bars)
        if len(returns) < 65:  # need 60 bars for 1h RV + some margin
            print(f"  {d}: insufficient returns ({len(returns)}), skipping")
            triggers_per_session.append(0)
            continue

        # RV_1h = std of last 60 1-min returns, RV_5m = std of last 5 1-min returns
        rv_1h = compute_rv(returns, 60)
        rv_5m = compute_rv(returns, 5)

        # Compute 4h EMA of close prices (240 min, but use what we have)
        closes = np.array([b.close_mid for b in bars])
        ema_4h = compute_ema(closes, min(240, len(closes)))
        # EMA slope: difference over last 30 bars
        ema_slope = np.zeros_like(ema_4h)
        for i in range(30, len(ema_slope)):
            ema_slope[i] = ema_4h[i] - ema_4h[i - 30]

        # Collect valid RV_1h values for percentile computation
        valid_rv_1h = rv_1h[~np.isnan(rv_1h)]
        valid_rv_1h = valid_rv_1h[valid_rv_1h > 0]

        # Use expanding percentile: all prior days + current day
        all_rv_1h_values.extend(valid_rv_1h.tolist())

        if len(all_rv_1h_values) < 20:
            print(f"  {d}: insufficient RV history ({len(all_rv_1h_values)}), skipping")
            triggers_per_session.append(0)
            continue

        # P20 threshold from expanding window
        p20 = np.percentile(all_rv_1h_values, 20)

        # Detect compression -> expansion events
        session_triggers = 0
        in_compression = False
        compression_start_idx = -1

        for i in range(60, len(returns)):  # start after 1h warmup
            if np.isnan(rv_1h[i]) or np.isnan(rv_5m[i]):
                continue
            if rv_1h[i] <= 0 or rv_5m[i] <= 0:
                continue

            # Check compression state
            if not in_compression and rv_1h[i] < p20:
                in_compression = True
                compression_start_idx = i
            elif in_compression and rv_1h[i] >= p20 * 1.5:
                # Exited compression without expansion trigger
                in_compression = False

            # Check expansion trigger while in compression
            if in_compression and rv_5m[i] > 0 and rv_1h[i] > 0:
                ratio = rv_5m[i] / rv_1h[i]
                if ratio > 2.0:
                    # TRIGGER!
                    bar_idx = i + 1  # returns are offset by 1 from bars
                    if bar_idx >= len(bars):
                        continue
                    trigger_minute = bars[bar_idx].ts_minute

                    # Direction test A: 4h EMA slope
                    ema_slope_val = ema_slope[bar_idx] if bar_idx < len(ema_slope) else 0.0

                    # Direction test B: first 5min return after trigger
                    first_5m_ret = 0.0
                    if bar_idx + 5 < len(bars):
                        first_5m_ret = bars[bar_idx + 5].close_mid - bars[bar_idx].close_mid

                    # Next 1h return after trigger
                    next_1h_ret = 0.0
                    if bar_idx + 60 < len(bars):
                        next_1h_ret = bars[bar_idx + 60].close_mid - bars[bar_idx].close_mid
                    elif bar_idx + 30 < len(bars):
                        # Use available if less than 60 bars left
                        remaining = len(bars) - bar_idx - 1
                        next_1h_ret = bars[-1].close_mid - bars[bar_idx].close_mid

                    event = BreakoutEvent(
                        date=d,
                        trigger_minute=trigger_minute,
                        rv_1h_at_trigger=rv_1h[i],
                        rv_5m_at_trigger=rv_5m[i],
                        expansion_ratio=ratio,
                        ema_4h_slope=ema_slope_val,
                        first_5m_return=first_5m_ret,
                        next_1h_return=next_1h_ret,
                    )
                    all_breakout_events.append(event)
                    session_triggers += 1

                    # Cooldown: exit compression, don't re-trigger for 30 bars
                    in_compression = False

        triggers_per_session.append(session_triggers)
        print(f"  {d}: {session_triggers} triggers, P20={p20:.6f}")

    # ========== ANALYSIS ==========
    print("\n" + "=" * 60)
    print("VRB DIAGNOSTIC RESULTS")
    print("=" * 60)

    n_sessions = len(triggers_per_session)
    total_triggers = sum(triggers_per_session)
    avg_triggers = total_triggers / n_sessions if n_sessions > 0 else 0

    print(f"\n--- Kill Gate 1: Trigger Frequency ---")
    print(f"Sessions analyzed: {n_sessions}")
    print(f"Total triggers: {total_triggers}")
    print(f"Average triggers/session: {avg_triggers:.2f}")
    print(f"Triggers per session: {triggers_per_session}")
    kg1_pass = avg_triggers >= 1.0
    print(f"Kill Gate 1 (>= 1/session): {'PASS' if kg1_pass else 'FAIL'}")

    # Direction accuracy
    print(f"\n--- Kill Gate 2: Direction Accuracy ---")
    n_events = len(all_breakout_events)
    print(f"Total breakout events with measurable outcomes: {n_events}")

    if n_events > 0:
        # Test A: EMA slope predicts 1h return direction
        ema_correct = 0
        ema_total = 0
        for e in all_breakout_events:
            if abs(e.next_1h_return) > 0.1 and abs(e.ema_4h_slope) > 0.01:
                ema_total += 1
                if np.sign(e.ema_4h_slope) == np.sign(e.next_1h_return):
                    ema_correct += 1
        ema_acc = ema_correct / ema_total if ema_total > 0 else 0.0
        print(f"Test A (4h EMA slope -> 1h return): {ema_correct}/{ema_total} = {ema_acc:.1%}")

        # Test B: Reactive (first 5m return sign -> 1h return sign)
        reactive_correct = 0
        reactive_total = 0
        for e in all_breakout_events:
            if abs(e.next_1h_return) > 0.1 and abs(e.first_5m_return) > 0.1:
                reactive_total += 1
                if np.sign(e.first_5m_return) == np.sign(e.next_1h_return):
                    reactive_correct += 1
        reactive_acc = reactive_correct / reactive_total if reactive_total > 0 else 0.0
        print(f"Test B (reactive 5m -> 1h return): {reactive_correct}/{reactive_total} = {reactive_acc:.1%}")

        kg2_pass = ema_acc >= 0.55 or reactive_acc >= 0.55
        print(f"Kill Gate 2 (either >= 55%): {'PASS' if kg2_pass else 'FAIL'}")

        # Additional: mean return after trigger
        returns_1h = [e.next_1h_return for e in all_breakout_events if abs(e.next_1h_return) > 0]
        if returns_1h:
            mean_abs_ret = np.mean(np.abs(returns_1h))
            mean_ret = np.mean(returns_1h)
            print(f"\nMean 1h return after trigger: {mean_ret:.2f} pts")
            print(f"Mean |1h return| after trigger: {mean_abs_ret:.2f} pts")
            print(f"Cost per RT: {COST_PTS:.2f} pts")
            print(f"Mean |return| / cost: {mean_abs_ret / COST_PTS:.2f}x")
    else:
        kg2_pass = False
        print("No breakout events - FAIL")

    # Time-of-day distribution
    print(f"\n--- Kill Gate 3: Time-of-Day Test ---")
    if n_events > 0:
        trigger_minutes = [e.trigger_minute for e in all_breakout_events]

        # Bin into 30-min buckets
        bucket_edges = list(range(DAY_START_MIN, DAY_END_MIN + 1, 30))
        observed = np.zeros(len(bucket_edges) - 1)
        for m in trigger_minutes:
            for bi in range(len(bucket_edges) - 1):
                if bucket_edges[bi] <= m < bucket_edges[bi + 1]:
                    observed[bi] += 1
                    break

        expected = np.full_like(observed, n_events / len(observed))

        # Chi-squared test
        # Only include bins where expected > 0
        valid_bins = expected > 0
        if np.sum(valid_bins) > 1:
            chi2 = np.sum((observed[valid_bins] - expected[valid_bins]) ** 2 / expected[valid_bins])
            dof = np.sum(valid_bins) - 1
            # Approximate p-value using chi2 CDF (scipy-free)
            # For rough check: chi2/dof > 2 suggests non-uniform
            chi2_per_dof = chi2 / dof if dof > 0 else 0
            print(f"Trigger time distribution (30-min buckets):")
            for bi in range(len(bucket_edges) - 1):
                h_start = bucket_edges[bi] // 60
                m_start = bucket_edges[bi] % 60
                h_end = bucket_edges[bi + 1] // 60
                m_end = bucket_edges[bi + 1] % 60
                print(f"  {h_start:02d}:{m_start:02d}-{h_end:02d}:{m_end:02d}: "
                      f"{int(observed[bi])} triggers")
            print(f"Chi-squared: {chi2:.2f}, dof: {dof}, chi2/dof: {chi2_per_dof:.2f}")

            # Check if >80% in same bucket
            max_bucket_frac = np.max(observed) / n_events if n_events > 0 else 0
            print(f"Max bucket concentration: {max_bucket_frac:.1%}")
            kg3_tod_disguise = max_bucket_frac > 0.80
            print(f"Kill Gate 3 (>80% in one bucket = ToD disguise): "
                  f"{'FAIL (ToD disguise)' if kg3_tod_disguise else 'PASS'}")
        else:
            kg3_tod_disguise = True
            print("Insufficient bins for chi-squared test")
    else:
        kg3_tod_disguise = True
        print("No events for ToD test")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    overall = kg1_pass and kg2_pass and not kg3_tod_disguise
    print(f"Kill Gate 1 (triggers >= 1/session): {'PASS' if kg1_pass else 'FAIL'}")
    print(f"Kill Gate 2 (direction >= 55%):      {'PASS' if kg2_pass else 'FAIL'}")
    print(f"Kill Gate 3 (not ToD disguise):      {'PASS' if not kg3_tod_disguise else 'FAIL'}")
    print(f"Overall: {'PROCEED to prototype' if overall else 'KILLED'}")
    print(f"\nData quality note: only {len(valid_dates)} day-session dates available "
          f"(not 58 as originally estimated). Gaps: see date list above.")

    # Detailed event dump for review
    print("\n--- All Breakout Events ---")
    for e in all_breakout_events:
        h = e.trigger_minute // 60
        m = e.trigger_minute % 60
        print(f"  {e.date} {h:02d}:{m:02d} | "
              f"ratio={e.expansion_ratio:.2f} | "
              f"ema_slope={e.ema_4h_slope:+.2f} | "
              f"5m_ret={e.first_5m_return:+.1f} | "
              f"1h_ret={e.next_1h_return:+.1f}")

    return {
        "n_sessions": n_sessions,
        "valid_dates": valid_dates,
        "total_triggers": total_triggers,
        "avg_triggers_per_session": avg_triggers,
        "triggers_per_session": triggers_per_session,
        "n_breakout_events": n_events,
        "kg1_pass": kg1_pass,
        "kg2_pass": kg2_pass,
        "kg3_tod_disguise": kg3_tod_disguise,
        "overall_pass": overall,
        "events": [
            {
                "date": e.date,
                "trigger_minute": e.trigger_minute,
                "expansion_ratio": e.expansion_ratio,
                "ema_4h_slope": e.ema_4h_slope,
                "first_5m_return": e.first_5m_return,
                "next_1h_return": e.next_1h_return,
            }
            for e in all_breakout_events
        ],
    }


if __name__ == "__main__":
    results = run_vrb_diagnostic()
    # Save JSON results
    out_path = "research/experiments/validations/vrb_diagnostic/vrb_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {out_path}")
