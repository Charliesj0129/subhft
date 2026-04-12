"""VRB + HMM Re-diagnostic with Night Session Data for TMFD6.

Stage 2a-bis: Re-run both diagnostics including night session (15:00-05:00).
Night session adds ~14h/day vs 5h day, roughly tripling the sample.

Reports day / night / combined results separately.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

try:
    import clickhouse_connect
except ImportError:
    print("ERROR: clickhouse_connect not installed")
    sys.exit(1)

SCALE = 1_000_000
SYMBOL = "TMFD6"
COST_PTS = 3.92

# Session definitions (Taiwan time, minute-of-day)
# Day: 08:45 (525) to 13:45 (825)
# Night: 15:00 (900) to 05:00 next day (300)
# Night crosses midnight, so we handle two ranges: 900-1440 and 0-300

SessionType = Literal["day", "night"]


def get_client() -> clickhouse_connect.driver.Client:
    return clickhouse_connect.get_client(
        host=os.getenv("HFT_CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("HFT_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("HFT_CLICKHOUSE_USER", "default"),
        password=os.getenv("HFT_CLICKHOUSE_PASSWORD", ""),
    )


def fetch_session_midprices(
    client: clickhouse_connect.driver.Client,
    date_str: str,
    session: SessionType,
) -> list[tuple[int, float]]:
    """Fetch (sequential_minute_index, mid_price) for a session.

    For night session, we normalize minutes to a continuous sequence:
    15:00=0, 16:00=60, ..., 23:59=539, 00:00=540, ..., 04:59=839
    For day session: 08:45=0, 09:00=15, ..., 13:44=299
    """
    if session == "day":
        query = f"""
        SELECT
            toHour(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) * 60
                + toMinute(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) as min_of_day,
            (bids_price[1] + asks_price[1]) / 2 as mid_scaled
        FROM hft.market_data
        WHERE symbol = '{SYMBOL}' AND type = 'BidAsk'
          AND toDate(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) = '{date_str}'
          AND toHour(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) * 60
              + toMinute(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) >= 525
          AND toHour(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) * 60
              + toMinute(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) < 825
          AND length(bids_price) > 0 AND length(asks_price) > 0
          AND bids_price[1] > 0 AND asks_price[1] > 0
        ORDER BY exch_ts
        """
        result = client.query(query)
        rows = []
        for row in result.result_rows:
            mod = int(row[0])
            seq_min = mod - 525  # normalize: 525->0, 526->1, etc.
            mid_pts = float(row[1]) / SCALE
            rows.append((seq_min, mid_pts))
        return rows
    else:
        # Night: same calendar date 15:00-23:59 + next calendar date 00:00-04:59
        # Query part 1: same date, hours 15-23
        q1 = f"""
        SELECT
            toHour(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) * 60
                + toMinute(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) as min_of_day,
            (bids_price[1] + asks_price[1]) / 2 as mid_scaled,
            exch_ts
        FROM hft.market_data
        WHERE symbol = '{SYMBOL}' AND type = 'BidAsk'
          AND toDate(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) = '{date_str}'
          AND toHour(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) >= 15
          AND length(bids_price) > 0 AND length(asks_price) > 0
          AND bids_price[1] > 0 AND asks_price[1] > 0
        ORDER BY exch_ts
        """
        # Query part 2: next calendar date, hours 0-4
        # We need to compute next date
        from datetime import datetime, timedelta
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        next_date = (dt + timedelta(days=1)).strftime("%Y-%m-%d")

        q2 = f"""
        SELECT
            toHour(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) * 60
                + toMinute(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) as min_of_day,
            (bids_price[1] + asks_price[1]) / 2 as mid_scaled,
            exch_ts
        FROM hft.market_data
        WHERE symbol = '{SYMBOL}' AND type = 'BidAsk'
          AND toDate(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) = '{next_date}'
          AND toHour(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) < 5
          AND length(bids_price) > 0 AND length(asks_price) > 0
          AND bids_price[1] > 0 AND asks_price[1] > 0
        ORDER BY exch_ts
        """

        rows = []
        r1 = client.query(q1)
        for row in r1.result_rows:
            mod = int(row[0])
            seq_min = mod - 900  # 900->0, 960->60, etc.
            mid_pts = float(row[1]) / SCALE
            rows.append((seq_min, mid_pts))

        r2 = client.query(q2)
        for row in r2.result_rows:
            mod = int(row[0])
            seq_min = mod + 540  # 0->540, 60->600, ..., 299->839
            mid_pts = float(row[1]) / SCALE
            rows.append((seq_min, mid_pts))

        rows.sort(key=lambda x: x[0])
        return rows


def build_minute_bars(
    raw_data: list[tuple[int, float]],
) -> list[tuple[int, float]]:
    """Build 1-minute bars (close price per minute). Returns (seq_minute, close_price)."""
    by_min: dict[int, list[float]] = defaultdict(list)
    for seq_min, mid in raw_data:
        by_min[seq_min].append(mid)
    return [(m, prices[-1]) for m, prices in sorted(by_min.items())]


def build_5min_bars(
    raw_data: list[tuple[int, float]],
) -> list[tuple[int, float]]:
    """Build 5-min bars (close price per 5-min bucket). Returns (bar_start, close_price)."""
    by_bar: dict[int, list[float]] = defaultdict(list)
    for seq_min, mid in raw_data:
        bar_start = (seq_min // 5) * 5
        by_bar[bar_start].append(mid)
    return [(b, prices[-1]) for b, prices in sorted(by_bar.items())]


def compute_rv(returns: np.ndarray, window: int) -> np.ndarray:
    """Rolling realized volatility (std)."""
    n = len(returns)
    rv = np.full(n, np.nan)
    for i in range(window - 1, n):
        rv[i] = np.std(returns[i - window + 1 : i + 1])
    return rv


def compute_ema(values: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1)
    ema = np.zeros_like(values, dtype=float)
    ema[0] = values[0]
    for i in range(1, len(values)):
        ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]
    return ema


# ===================== VRB DIAGNOSTIC =====================

def run_vrb_session(
    all_bars: dict[str, list[tuple[int, float]]],
    session_label: str,
) -> dict:
    """Run VRB diagnostic on a set of sessions."""
    valid_dates = sorted(all_bars.keys())
    n_sessions = len(valid_dates)
    print(f"\n{'='*60}")
    print(f"VRB Diagnostic: {session_label} ({n_sessions} sessions)")
    print(f"{'='*60}")

    all_rv_1h_values: list[float] = []
    triggers_per_session: list[int] = []
    events: list[dict] = []

    for d in valid_dates:
        bars_1m = all_bars[d]
        if len(bars_1m) < 65:
            triggers_per_session.append(0)
            continue

        closes = np.array([b[1] for b in bars_1m])
        minutes = np.array([b[0] for b in bars_1m])
        rets = np.diff(np.log(np.clip(closes, 1.0, None)))

        if len(rets) < 65:
            triggers_per_session.append(0)
            continue

        rv_1h = compute_rv(rets, 60)
        rv_5m = compute_rv(rets, 5)

        # EMA of closes (use 240-bar span for ~4h equivalent)
        ema_span = min(240, len(closes))
        ema_4h = compute_ema(closes, ema_span)
        ema_slope = np.zeros_like(ema_4h)
        for i in range(30, len(ema_slope)):
            ema_slope[i] = ema_4h[i] - ema_4h[i - 30]

        # Accumulate RV values for expanding percentile
        valid_rv = rv_1h[~np.isnan(rv_1h)]
        valid_rv = valid_rv[valid_rv > 0]
        all_rv_1h_values.extend(valid_rv.tolist())

        if len(all_rv_1h_values) < 20:
            triggers_per_session.append(0)
            continue

        p20 = np.percentile(all_rv_1h_values, 20)

        session_triggers = 0
        in_compression = False

        for i in range(60, len(rets)):
            if np.isnan(rv_1h[i]) or np.isnan(rv_5m[i]):
                continue
            if rv_1h[i] <= 0 or rv_5m[i] <= 0:
                continue

            if not in_compression and rv_1h[i] < p20:
                in_compression = True
            elif in_compression and rv_1h[i] >= p20 * 1.5:
                in_compression = False

            if in_compression:
                ratio = rv_5m[i] / rv_1h[i]
                if ratio > 2.0:
                    bar_idx = i + 1
                    if bar_idx >= len(closes):
                        continue

                    trigger_min = int(minutes[bar_idx]) if bar_idx < len(minutes) else -1
                    ema_slope_val = ema_slope[bar_idx] if bar_idx < len(ema_slope) else 0.0

                    first_5m_ret = 0.0
                    if bar_idx + 5 < len(closes):
                        first_5m_ret = closes[bar_idx + 5] - closes[bar_idx]

                    next_1h_ret = 0.0
                    if bar_idx + 60 < len(closes):
                        next_1h_ret = closes[bar_idx + 60] - closes[bar_idx]
                    elif bar_idx + 30 < len(closes):
                        next_1h_ret = closes[-1] - closes[bar_idx]

                    events.append({
                        "date": d,
                        "trigger_minute": trigger_min,
                        "expansion_ratio": ratio,
                        "ema_4h_slope": ema_slope_val,
                        "first_5m_return": first_5m_ret,
                        "next_1h_return": next_1h_ret,
                    })
                    session_triggers += 1
                    in_compression = False  # cooldown

        triggers_per_session.append(session_triggers)

    total_triggers = sum(triggers_per_session)
    avg_triggers = total_triggers / n_sessions if n_sessions > 0 else 0
    n_events = len(events)

    print(f"Total triggers: {total_triggers}")
    print(f"Avg triggers/session: {avg_triggers:.2f}")
    kg1 = avg_triggers >= 1.0
    print(f"Kill Gate 1 (>= 1/session): {'PASS' if kg1 else 'FAIL'}")

    # Direction accuracy
    ema_correct = ema_total = 0
    reactive_correct = reactive_total = 0
    for e in events:
        if abs(e["next_1h_return"]) > 0.1 and abs(e["ema_4h_slope"]) > 0.01:
            ema_total += 1
            if np.sign(e["ema_4h_slope"]) == np.sign(e["next_1h_return"]):
                ema_correct += 1
        if abs(e["next_1h_return"]) > 0.1 and abs(e["first_5m_return"]) > 0.1:
            reactive_total += 1
            if np.sign(e["first_5m_return"]) == np.sign(e["next_1h_return"]):
                reactive_correct += 1

    ema_acc = ema_correct / ema_total if ema_total > 0 else 0.0
    reactive_acc = reactive_correct / reactive_total if reactive_total > 0 else 0.0
    print(f"Direction A (EMA): {ema_correct}/{ema_total} = {ema_acc:.1%}")
    print(f"Direction B (reactive): {reactive_correct}/{reactive_total} = {reactive_acc:.1%}")
    kg2 = ema_acc >= 0.55 or reactive_acc >= 0.55
    print(f"Kill Gate 2 (either >= 55%): {'PASS' if kg2 else 'FAIL'}")

    # PnL stats
    rets_1h = [e["next_1h_return"] for e in events if abs(e["next_1h_return"]) > 0]
    mean_abs_ret = np.mean(np.abs(rets_1h)) if rets_1h else 0.0
    mean_ret = np.mean(rets_1h) if rets_1h else 0.0
    print(f"Mean 1h |return|: {mean_abs_ret:.2f} pts (cost: {COST_PTS:.2f})")

    # ToD distribution
    kg3_tod = False
    if n_events > 0:
        trigger_mins = [e["trigger_minute"] for e in events]
        # Bin into 30-min buckets
        min_t = min(trigger_mins)
        max_t = max(trigger_mins)
        n_buckets = max(1, (max_t - min_t) // 30 + 1)
        bucket_counts = np.zeros(n_buckets)
        for m in trigger_mins:
            bi = min((m - min_t) // 30, n_buckets - 1)
            bucket_counts[bi] += 1
        max_frac = np.max(bucket_counts) / n_events
        print(f"Max ToD bucket concentration: {max_frac:.1%}")
        kg3_tod = max_frac > 0.80
        print(f"Kill Gate 3 (>80% = ToD disguise): {'FAIL' if kg3_tod else 'PASS'}")
    else:
        kg3_tod = True
        print("No events for ToD test -- FAIL")

    overall = kg1 and kg2 and not kg3_tod
    print(f"Overall: {'PROCEED' if overall else 'KILLED'}")

    # Print events
    for e in events:
        print(f"  {e['date']} min={e['trigger_minute']:>4} | "
              f"ratio={e['expansion_ratio']:.2f} | "
              f"ema={e['ema_4h_slope']:+.1f} | "
              f"5m={e['first_5m_return']:+.1f} | "
              f"1h={e['next_1h_return']:+.1f}")

    return {
        "session": session_label,
        "n_sessions": n_sessions,
        "total_triggers": total_triggers,
        "avg_triggers": avg_triggers,
        "kg1_pass": kg1,
        "ema_acc": ema_acc,
        "reactive_acc": reactive_acc,
        "kg2_pass": kg2,
        "kg3_tod": kg3_tod,
        "overall": overall,
        "n_events": n_events,
        "events": events,
        "mean_abs_1h_return": mean_abs_ret,
    }


# ===================== HMM DIAGNOSTIC =====================

def fit_2state_hmm(returns: np.ndarray, n_iter: int = 100) -> tuple:
    """Fit 2-state Gaussian HMM via Baum-Welch. Returns (mu, sigma, trans, pi)."""
    n = len(returns)
    sorted_rets = np.sort(returns)
    mid = n // 2
    mu = np.array([np.mean(sorted_rets[:mid]), np.mean(sorted_rets[mid:])])
    sigma = np.array([
        max(np.std(sorted_rets[:mid]), 1e-6),
        max(np.std(sorted_rets[mid:]), 1e-6),
    ])
    trans = np.array([[0.95, 0.05], [0.05, 0.95]])
    pi = np.array([0.5, 0.5])

    for _ in range(n_iter):
        emit = np.zeros((n, 2))
        for k in range(2):
            emit[:, k] = (1.0 / (sigma[k] * np.sqrt(2 * np.pi))) * np.exp(
                -0.5 * ((returns - mu[k]) / sigma[k]) ** 2,
            )
        emit = np.clip(emit, 1e-300, None)

        # Forward
        alpha = np.zeros((n, 2))
        alpha[0] = pi * emit[0]
        s = alpha[0].sum()
        if s > 0:
            alpha[0] /= s
        for t in range(1, n):
            alpha[t] = emit[t] * (alpha[t - 1] @ trans)
            s = alpha[t].sum()
            if s > 0:
                alpha[t] /= s

        # Backward
        beta = np.zeros((n, 2))
        beta[-1] = 1.0
        for t in range(n - 2, -1, -1):
            beta[t] = trans @ (emit[t + 1] * beta[t + 1])
            s = beta[t].sum()
            if s > 0:
                beta[t] /= s

        gamma = alpha * beta
        gamma_sum = np.clip(gamma.sum(axis=1, keepdims=True), 1e-300, None)
        gamma = gamma / gamma_sum

        xi = np.zeros((2, 2))
        for t in range(n - 1):
            num = np.outer(alpha[t], emit[t + 1] * beta[t + 1]) * trans
            s = num.sum()
            if s > 0:
                xi += num / s

        for k in range(2):
            wk = gamma[:, k].sum() + 1e-10
            mu[k] = (gamma[:, k] * returns).sum() / wk
            sigma[k] = max(np.sqrt((gamma[:, k] * (returns - mu[k]) ** 2).sum() / wk), 1e-6)

        xi_sum = np.clip(xi.sum(axis=1, keepdims=True), 1e-10, None)
        trans = xi / xi_sum
        pi = gamma[0]

    if mu[0] > mu[1]:
        mu = mu[::-1]
        sigma = sigma[::-1]
        trans = trans[::-1, ::-1]
        pi = pi[::-1]

    return mu, sigma, trans, pi


def forward_filter(returns, mu, sigma, trans, pi):
    n = len(returns)
    emit = np.zeros((n, 2))
    for k in range(2):
        emit[:, k] = (1.0 / (sigma[k] * np.sqrt(2 * np.pi))) * np.exp(
            -0.5 * ((returns - mu[k]) / sigma[k]) ** 2,
        )
    emit = np.clip(emit, 1e-300, None)

    filtered = np.zeros((n, 2))
    filtered[0] = pi * emit[0]
    s = filtered[0].sum()
    if s > 0:
        filtered[0] /= s
    for t in range(1, n):
        filtered[t] = emit[t] * (filtered[t - 1] @ trans)
        s = filtered[t].sum()
        if s > 0:
            filtered[t] /= s
    return filtered


def run_hmm_session(
    all_bars_5m: dict[str, list[tuple[int, float]]],
    session_label: str,
) -> dict:
    """Run HMM diagnostic on 5-min bars."""
    valid_dates = sorted(all_bars_5m.keys())
    n_dates = len(valid_dates)
    print(f"\n{'='*60}")
    print(f"HMM Diagnostic: {session_label} ({n_dates} dates)")
    print(f"{'='*60}")

    if n_dates < 4:
        print("FATAL: too few dates")
        return {"error": "too_few_dates", "n_dates": n_dates}

    n_is = n_dates // 2
    is_dates = valid_dates[:n_is]
    oos_dates = valid_dates[n_is:]
    print(f"IS: {len(is_dates)} dates ({is_dates[0]} to {is_dates[-1]})")
    print(f"OOS: {len(oos_dates)} dates ({oos_dates[0]} to {oos_dates[-1]})")

    # Build IS returns
    is_returns_list: list[float] = []
    for d in is_dates:
        closes = np.array([b[1] for b in all_bars_5m[d]])
        if len(closes) < 2:
            continue
        rets = np.diff(closes)
        is_returns_list.extend(rets.tolist())

    is_returns = np.array(is_returns_list)
    print(f"IS: {len(is_returns)} 5-min returns")
    print(f"IS stats: mean={np.mean(is_returns):.4f}, std={np.std(is_returns):.4f}")

    # Fit HMM
    print("Fitting 2-state HMM...")
    mu, sigma, trans, pi = fit_2state_hmm(is_returns)

    print(f"State 0: mu={mu[0]:.4f}, sigma={sigma[0]:.4f}")
    print(f"State 1: mu={mu[1]:.4f}, sigma={sigma[1]:.4f}")
    print(f"Trans: [[{trans[0,0]:.4f}, {trans[0,1]:.4f}], [{trans[1,0]:.4f}, {trans[1,1]:.4f}]]")

    mu_diff = abs(mu[1] - mu[0])
    max_sigma = max(sigma[0], sigma[1])
    separation = mu_diff / (2 * max_sigma) if max_sigma > 0 else 0
    kg_sep = mu_diff >= 2 * max_sigma
    print(f"|mu_diff|={mu_diff:.4f}, 2*max_sigma={2*max_sigma:.4f}, ratio={separation:.4f}")
    print(f"Kill Gate (separation): {'PASS' if kg_sep else 'FAIL'}")

    dur0 = 1.0 / (1.0 - trans[0, 0]) if trans[0, 0] < 1 else float("inf")
    dur1 = 1.0 / (1.0 - trans[1, 1]) if trans[1, 1] < 1 else float("inf")
    print(f"State durations: S0={dur0:.1f} bars ({dur0*5:.0f}min), S1={dur1:.1f} bars ({dur1*5:.0f}min)")

    # OOS
    oos_bars = 0
    oos_trending = 0
    oos_reverting = 0
    trend_rets: list[float] = []
    revert_rets: list[float] = []

    for d in oos_dates:
        closes = np.array([b[1] for b in all_bars_5m[d]])
        if len(closes) < 2:
            continue
        rets = np.diff(closes)
        filtered = forward_filter(rets, mu, sigma, trans, pi)

        for i in range(len(rets)):
            oos_bars += 1
            p1 = filtered[i, 1]
            if p1 > 0.7:
                oos_trending += 1
                if i + 1 < len(rets):
                    trend_rets.append(rets[i + 1])
            elif p1 < 0.3:
                oos_reverting += 1
                if i + 1 < len(rets):
                    revert_rets.append(-rets[i + 1])

    total_trades = oos_trending + oos_reverting
    kg_n = total_trades >= 100
    print(f"\nOOS: {oos_bars} bars, trending={oos_trending}, reverting={oos_reverting}, total={total_trades}")
    print(f"Kill Gate (N>=100): {'PASS' if kg_n else 'FAIL'}")

    if trend_rets:
        t_arr = np.array(trend_rets)
        print(f"Momentum PnL: mean={np.mean(t_arr):.4f}, std={np.std(t_arr):.4f}, "
              f"WR={np.mean(t_arr>0):.1%}, N={len(t_arr)}")
    if revert_rets:
        r_arr = np.array(revert_rets)
        print(f"Reversion PnL: mean={np.mean(r_arr):.4f}, std={np.std(r_arr):.4f}, "
              f"WR={np.mean(r_arr>0):.1%}, N={len(r_arr)}")

    # State fractions per IS date
    print("\nIS state characterization:")
    for d in is_dates:
        closes = np.array([b[1] for b in all_bars_5m[d]])
        if len(closes) < 2:
            continue
        rets = np.diff(closes)
        filtered = forward_filter(rets, mu, sigma, trans, pi)
        s1_frac = np.mean(filtered[:, 1] > 0.5)
        daily_ret = closes[-1] - closes[0]
        print(f"  {d}: trending={s1_frac:.0%}, daily_ret={daily_ret:+.1f}, bars={len(rets)}")

    overall = kg_sep and kg_n
    print(f"\nOverall: {'PROCEED' if overall else 'KILLED'}")

    return {
        "session": session_label,
        "n_dates": n_dates,
        "is_n": len(is_returns),
        "oos_n_bars": oos_bars,
        "mu": mu.tolist(),
        "sigma": sigma.tolist(),
        "trans": trans.tolist(),
        "mu_diff": float(mu_diff),
        "separation": float(separation),
        "kg_sep": bool(kg_sep),
        "oos_trending": oos_trending,
        "oos_reverting": oos_reverting,
        "total_trades": total_trades,
        "kg_n": bool(kg_n),
        "overall": bool(overall),
        "trend_mean_ret": float(np.mean(trend_rets)) if trend_rets else None,
        "trend_wr": float(np.mean(np.array(trend_rets) > 0)) if trend_rets else None,
        "revert_mean_ret": float(np.mean(revert_rets)) if revert_rets else None,
    }


# ===================== MAIN =====================

def main() -> dict:
    client = get_client()

    # Get all dates with data
    result = client.query(f"""
    SELECT DISTINCT toDate(toDateTime(exch_ts / 1000000000, 'Asia/Taipei')) as dt
    FROM hft.market_data
    WHERE symbol = '{SYMBOL}' AND type = 'BidAsk'
    ORDER BY dt
    """)
    all_dates = [str(row[0]) for row in result.result_rows]
    print(f"Total dates in DB: {len(all_dates)}")

    # Build bars for each session type
    day_1m: dict[str, list[tuple[int, float]]] = {}
    night_1m: dict[str, list[tuple[int, float]]] = {}
    day_5m: dict[str, list[tuple[int, float]]] = {}
    night_5m: dict[str, list[tuple[int, float]]] = {}

    for d in all_dates:
        # Day session
        raw_day = fetch_session_midprices(client, d, "day")
        if len(raw_day) >= 100:
            bars = build_minute_bars(raw_day)
            if len(bars) >= 60:
                day_1m[d] = bars
                day_5m[d] = build_5min_bars(raw_day)
                print(f"  {d} DAY: {len(bars)} 1m-bars, {len(day_5m[d])} 5m-bars")

        # Night session
        raw_night = fetch_session_midprices(client, d, "night")
        if len(raw_night) >= 100:
            bars = build_minute_bars(raw_night)
            if len(bars) >= 60:
                night_1m[d] = bars
                night_5m[d] = build_5min_bars(raw_night)
                print(f"  {d} NIGHT: {len(bars)} 1m-bars, {len(night_5m[d])} 5m-bars")

    print(f"\nDay sessions: {len(day_1m)}, Night sessions: {len(night_1m)}")

    # Combined: merge day and night for same trading date
    # For combined, we treat each session independently (don't merge across session break)
    combined_1m: dict[str, list[tuple[int, float]]] = {}
    combined_5m: dict[str, list[tuple[int, float]]] = {}
    all_session_dates = sorted(set(list(day_1m.keys()) + list(night_1m.keys())))
    for d in all_session_dates:
        merged_1m = []
        merged_5m = []
        if d in day_1m:
            # Day bars keep original seq_min
            merged_1m.extend(day_1m[d])
            merged_5m.extend(day_5m[d])
        if d in night_1m:
            # Night bars: offset by 1000 to avoid collision with day
            offset = 1000
            merged_1m.extend([(m + offset, p) for m, p in night_1m[d]])
            merged_5m.extend([(m + offset, p) for m, p in night_5m[d]])
        if len(merged_1m) >= 60:
            combined_1m[d] = sorted(merged_1m, key=lambda x: x[0])
            combined_5m[d] = sorted(merged_5m, key=lambda x: x[0])

    print(f"Combined sessions: {len(combined_1m)}")

    results = {}

    # ===== VRB =====
    results["vrb_day"] = run_vrb_session(day_1m, "Day (08:45-13:45)")
    results["vrb_night"] = run_vrb_session(night_1m, "Night (15:00-05:00)")
    results["vrb_combined"] = run_vrb_session(combined_1m, "Combined (day+night)")

    # ===== HMM =====
    results["hmm_day"] = run_hmm_session(day_5m, "Day (08:45-13:45)")
    results["hmm_night"] = run_hmm_session(night_5m, "Night (15:00-05:00)")
    results["hmm_combined"] = run_hmm_session(combined_5m, "Combined (day+night)")

    # Final summary
    print(f"\n{'='*60}")
    print("FINAL SUMMARY")
    print(f"{'='*60}")
    for key in ["vrb_day", "vrb_night", "vrb_combined", "hmm_day", "hmm_night", "hmm_combined"]:
        r = results[key]
        label = r.get("session", key)
        overall = r.get("overall", False)
        print(f"  {label:30s}: {'PROCEED' if overall else 'KILLED'}")

    return results


if __name__ == "__main__":
    results = main()
    out_path = "research/experiments/validations/vrb_diagnostic/night_diagnostic_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {out_path}")
