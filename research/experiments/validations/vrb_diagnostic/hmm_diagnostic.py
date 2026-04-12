"""HMM Regime-Conditioned Momentum Diagnostic for TMFD6.

Stage 2a diagnostic for Candidate B (HMM-RCM).

Steps:
1. Aggregate TMFD6 ticks into 5-min OHLCV bars (day session 08:45-13:45)
2. Fit 2-state Gaussian HMM on first 8 days (limited data)
3. Report state means, variances, transition matrix
4. Kill gate: |mu_1 - mu_2| < 2*max(sigma_1, sigma_2) -> states indistinguishable
5. OOS (remaining days): count implied trades with P(trending) > 0.7
6. Kill gate: N < 100 OOS trades

Note: only 16 day-session dates available. Using 8 IS / 8 OOS split.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

import numpy as np

try:
    import clickhouse_connect
except ImportError:
    print("ERROR: clickhouse_connect not installed")
    sys.exit(1)

try:
    from hmmlearn.hmm import GaussianHMM
except ImportError:
    print("WARNING: hmmlearn not installed, using manual implementation")
    GaussianHMM = None  # type: ignore[assignment, misc]


SCALE = 1_000_000
SYMBOL = "TMFD6"
DAY_START_MIN = 8 * 60 + 45
DAY_END_MIN = 13 * 60 + 45
BAR_SIZE_MIN = 5  # 5-minute bars


def get_client() -> clickhouse_connect.driver.Client:
    """Create ClickHouse client."""
    return clickhouse_connect.get_client(
        host=os.getenv("HFT_CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("HFT_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("HFT_CLICKHOUSE_USER", "default"),
        password=os.getenv("HFT_CLICKHOUSE_PASSWORD", "changeme"),
    )


def fetch_day_midprices(
    client: clickhouse_connect.driver.Client, date_str: str,
) -> list[tuple[int, float]]:
    """Fetch (minute_of_day, mid_price) for a single day session."""
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
    return [(int(row[0]), float(row[1]) / SCALE) for row in result.result_rows]


def build_5min_bars(
    raw_data: list[tuple[int, float]],
) -> list[tuple[int, float, float, float, float]]:
    """Build 5-min bars: (bar_start_minute, open, high, low, close)."""
    by_bar: dict[int, list[float]] = defaultdict(list)
    for min_of_day, mid in raw_data:
        bar_start = (min_of_day // BAR_SIZE_MIN) * BAR_SIZE_MIN
        by_bar[bar_start].append(mid)

    bars = []
    for bar_start in sorted(by_bar.keys()):
        prices = by_bar[bar_start]
        bars.append((bar_start, prices[0], max(prices), min(prices), prices[-1]))
    return bars


def fit_manual_2state_hmm(
    returns: np.ndarray, n_iter: int = 50,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Fit a 2-state Gaussian HMM manually using EM (Baum-Welch).

    Returns (means, stds, transition_matrix, start_prob).
    """
    n = len(returns)
    # Initialize with k-means-like split
    sorted_rets = np.sort(returns)
    mid = n // 2
    mu = np.array([np.mean(sorted_rets[:mid]), np.mean(sorted_rets[mid:])])
    sigma = np.array([np.std(sorted_rets[:mid]) + 1e-10, np.std(sorted_rets[mid:]) + 1e-10])
    trans = np.array([[0.9, 0.1], [0.1, 0.9]])
    pi = np.array([0.5, 0.5])

    for _iteration in range(n_iter):
        # E-step: forward-backward
        # Emission probabilities
        emit = np.zeros((n, 2))
        for k in range(2):
            emit[:, k] = (1.0 / (sigma[k] * np.sqrt(2 * np.pi))) * np.exp(
                -0.5 * ((returns - mu[k]) / sigma[k]) ** 2,
            )
        emit = np.clip(emit, 1e-300, None)

        # Forward
        alpha = np.zeros((n, 2))
        alpha[0] = pi * emit[0]
        alpha[0] /= alpha[0].sum() + 1e-300
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

        # Posterior
        gamma = alpha * beta
        gamma_sum = gamma.sum(axis=1, keepdims=True)
        gamma_sum = np.clip(gamma_sum, 1e-300, None)
        gamma = gamma / gamma_sum

        # Xi (transition posteriors)
        xi = np.zeros((2, 2))
        for t in range(n - 1):
            numerator = np.outer(alpha[t], emit[t + 1] * beta[t + 1]) * trans
            s = numerator.sum()
            if s > 0:
                xi += numerator / s

        # M-step
        for k in range(2):
            wk = gamma[:, k].sum() + 1e-10
            mu[k] = (gamma[:, k] * returns).sum() / wk
            sigma[k] = np.sqrt((gamma[:, k] * (returns - mu[k]) ** 2).sum() / wk) + 1e-10

        xi_row_sum = xi.sum(axis=1, keepdims=True)
        xi_row_sum = np.clip(xi_row_sum, 1e-10, None)
        trans = xi / xi_row_sum
        pi = gamma[0]

    # Ensure state 0 has lower mean (convention: state 0 = low/reverting, state 1 = high/trending)
    if mu[0] > mu[1]:
        mu = mu[::-1]
        sigma = sigma[::-1]
        trans = trans[::-1, ::-1]
        pi = pi[::-1]

    return mu, sigma, trans, pi


def forward_filter(
    returns: np.ndarray,
    mu: np.ndarray,
    sigma: np.ndarray,
    trans: np.ndarray,
    pi: np.ndarray,
) -> np.ndarray:
    """Online forward filtering to get P(state|data_1:t) at each timestep.

    Returns array of shape (n, 2) with state probabilities.
    """
    n = len(returns)
    filtered = np.zeros((n, 2))

    # Emission
    emit = np.zeros((n, 2))
    for k in range(2):
        emit[:, k] = (1.0 / (sigma[k] * np.sqrt(2 * np.pi))) * np.exp(
            -0.5 * ((returns - mu[k]) / sigma[k]) ** 2,
        )
    emit = np.clip(emit, 1e-300, None)

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


def run_hmm_diagnostic() -> dict:
    """Run the HMM diagnostic."""
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
    print(f"Found {len(dates)} day-session dates")

    # 2. Build 5-min bars for all dates
    all_bars: dict[str, list[tuple[int, float, float, float, float]]] = {}
    for d in dates:
        raw = fetch_day_midprices(client, d)
        if len(raw) < 100:
            print(f"  SKIP {d}: only {len(raw)} raw ticks")
            continue
        bars_5m = build_5min_bars(raw)
        if len(bars_5m) < 20:
            print(f"  SKIP {d}: only {len(bars_5m)} 5-min bars")
            continue
        all_bars[d] = bars_5m
        print(f"  {d}: {len(bars_5m)} 5-min bars")

    valid_dates = sorted(all_bars.keys())
    print(f"\nValid dates: {len(valid_dates)}")

    if len(valid_dates) < 4:
        print("FATAL: Insufficient dates for IS/OOS split")
        return {"error": "insufficient_dates", "n_dates": len(valid_dates)}

    # 3. Split IS/OOS (half and half)
    n_is = len(valid_dates) // 2
    is_dates = valid_dates[:n_is]
    oos_dates = valid_dates[n_is:]
    print(f"IS dates ({n_is}): {is_dates[0]} to {is_dates[-1]}")
    print(f"OOS dates ({len(oos_dates)}): {oos_dates[0]} to {oos_dates[-1]}")

    # 4. Build IS returns from 5-min bar closes
    is_returns_by_day: list[np.ndarray] = []
    is_all_returns: list[float] = []
    for d in is_dates:
        closes = np.array([b[4] for b in all_bars[d]])
        if len(closes) < 2:
            continue
        rets = np.diff(closes)  # simple returns in points
        is_returns_by_day.append(rets)
        is_all_returns.extend(rets.tolist())

    is_returns = np.array(is_all_returns)
    print(f"\nIS returns: {len(is_returns)} observations")
    print(f"IS return stats: mean={np.mean(is_returns):.4f}, std={np.std(is_returns):.4f}")

    # 5. Fit 2-state HMM
    print("\nFitting 2-state Gaussian HMM on IS data...")
    if GaussianHMM is not None:
        model = GaussianHMM(n_components=2, covariance_type="diag", n_iter=100, random_state=42)
        model.fit(is_returns.reshape(-1, 1))
        mu = model.means_.flatten()
        sigma = np.sqrt(model.covars_.flatten())
        trans = model.transmat_
        pi = model.startprob_
        # Sort by mean
        order = np.argsort(mu)
        mu = mu[order]
        sigma = sigma[order]
        trans = trans[np.ix_(order, order)]
        pi = pi[order]
    else:
        mu, sigma, trans, pi = fit_manual_2state_hmm(is_returns, n_iter=100)

    print(f"\nHMM Parameters:")
    print(f"  State 0 (low/reverting): mu={mu[0]:.4f}, sigma={sigma[0]:.4f}")
    print(f"  State 1 (high/trending): mu={mu[1]:.4f}, sigma={sigma[1]:.4f}")
    print(f"  Transition matrix:")
    print(f"    [{trans[0, 0]:.4f}, {trans[0, 1]:.4f}]")
    print(f"    [{trans[1, 0]:.4f}, {trans[1, 1]:.4f}]")
    print(f"  Start prob: [{pi[0]:.4f}, {pi[1]:.4f}]")

    # 6. Kill Gate: State distinguishability
    mu_diff = abs(mu[1] - mu[0])
    max_sigma = max(sigma[0], sigma[1])
    separation = mu_diff / (2 * max_sigma) if max_sigma > 0 else 0
    kg_distinguishable = mu_diff >= 2 * max_sigma
    print(f"\n--- Kill Gate: State Distinguishability ---")
    print(f"|mu_1 - mu_0| = {mu_diff:.4f}")
    print(f"2 * max(sigma) = {2 * max_sigma:.4f}")
    print(f"Separation ratio: {separation:.4f}")
    print(f"Kill Gate (|mu_diff| >= 2*max_sigma): {'PASS' if kg_distinguishable else 'FAIL'}")

    # Additional info: state persistence
    expected_state0_duration = 1.0 / (1.0 - trans[0, 0]) if trans[0, 0] < 1 else float("inf")
    expected_state1_duration = 1.0 / (1.0 - trans[1, 1]) if trans[1, 1] < 1 else float("inf")
    print(f"\nExpected state durations (5-min bars):")
    print(f"  State 0: {expected_state0_duration:.1f} bars = {expected_state0_duration * 5:.0f} min")
    print(f"  State 1: {expected_state1_duration:.1f} bars = {expected_state1_duration * 5:.0f} min")

    # 7. OOS forward filtering and trade counting
    print(f"\n--- OOS Analysis ---")
    oos_trades_trending = 0
    oos_trades_reverting = 0
    oos_bars_total = 0
    oos_trending_returns: list[float] = []
    oos_reverting_returns: list[float] = []

    for d in oos_dates:
        closes = np.array([b[4] for b in all_bars[d]])
        if len(closes) < 2:
            continue
        rets = np.diff(closes)

        # Forward filter
        filtered = forward_filter(rets, mu, sigma, trans, pi)

        for i in range(len(rets)):
            oos_bars_total += 1
            p_trending = filtered[i, 1]

            # Momentum trade: P(trending) > 0.7 AND go with return sign
            if p_trending > 0.7:
                oos_trades_trending += 1
                if i + 1 < len(rets):
                    oos_trending_returns.append(rets[i + 1])  # next bar return

            # Mean-reversion trade: P(trending) < 0.3
            elif p_trending < 0.3:
                oos_trades_reverting += 1
                if i + 1 < len(rets):
                    oos_reverting_returns.append(-rets[i + 1])  # fade direction

    total_oos_trades = oos_trades_trending + oos_trades_reverting
    print(f"OOS bars analyzed: {oos_bars_total}")
    print(f"OOS momentum trades (P>0.7): {oos_trades_trending}")
    print(f"OOS reversion trades (P<0.3): {oos_trades_reverting}")
    print(f"OOS total implied trades: {total_oos_trades}")

    kg_n_trades = total_oos_trades >= 100
    print(f"\nKill Gate (N >= 100 OOS trades): {'PASS' if kg_n_trades else 'FAIL'}")

    # PnL stats if we have trades
    cost_pts = 3.92
    if oos_trending_returns:
        trend_arr = np.array(oos_trending_returns)
        print(f"\nMomentum trades PnL:")
        print(f"  N: {len(trend_arr)}")
        print(f"  Mean return: {np.mean(trend_arr):.4f} pts")
        print(f"  Std: {np.std(trend_arr):.4f} pts")
        print(f"  Win rate: {np.mean(trend_arr > 0):.1%}")
        print(f"  Mean |return|: {np.mean(np.abs(trend_arr)):.4f} pts")
        print(f"  After cost ({cost_pts} pts RT): {np.mean(np.abs(trend_arr)) - cost_pts:.4f} pts")

    if oos_reverting_returns:
        rev_arr = np.array(oos_reverting_returns)
        print(f"\nReversion trades PnL:")
        print(f"  N: {len(rev_arr)}")
        print(f"  Mean return: {np.mean(rev_arr):.4f} pts")
        print(f"  Std: {np.std(rev_arr):.4f} pts")
        print(f"  Win rate: {np.mean(rev_arr > 0):.1%}")

    # 8. Compare regime to simple spread/rvol
    # (We don't have R17's exact regime labels, so just report state characteristics)
    print(f"\n--- State Characterization ---")
    for d in is_dates:
        closes = np.array([b[4] for b in all_bars[d]])
        if len(closes) < 2:
            continue
        rets = np.diff(closes)
        filtered = forward_filter(rets, mu, sigma, trans, pi)
        state_1_frac = np.mean(filtered[:, 1] > 0.5)
        print(f"  {d}: trending frac={state_1_frac:.1%}, "
              f"bars={len(rets)}, "
              f"daily_ret={closes[-1] - closes[0]:+.1f} pts")

    # Summary
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    overall = kg_distinguishable and kg_n_trades
    print(f"Kill Gate 1 (states distinguishable): {'PASS' if kg_distinguishable else 'FAIL'}")
    print(f"Kill Gate 2 (N >= 100 OOS trades):    {'PASS' if kg_n_trades else 'FAIL'}")
    print(f"Overall: {'PROCEED' if overall else 'KILLED'}")
    print(f"\nNote: 16 day-session dates, 8 IS / 8 OOS split. "
          f"IS has {len(is_returns)} bars, OOS has {oos_bars_total} bars.")

    return {
        "n_dates": len(valid_dates),
        "is_dates": is_dates,
        "oos_dates": oos_dates,
        "is_n_returns": len(is_returns),
        "oos_n_bars": oos_bars_total,
        "hmm_mu": mu.tolist(),
        "hmm_sigma": sigma.tolist(),
        "hmm_trans": trans.tolist(),
        "hmm_start_prob": pi.tolist(),
        "mu_diff": float(mu_diff),
        "separation_ratio": float(separation),
        "kg_distinguishable": bool(kg_distinguishable),
        "oos_trades_trending": oos_trades_trending,
        "oos_trades_reverting": oos_trades_reverting,
        "oos_total_trades": total_oos_trades,
        "kg_n_trades": bool(kg_n_trades),
        "overall_pass": bool(overall),
    }


if __name__ == "__main__":
    results = run_hmm_diagnostic()
    out_path = "research/experiments/validations/vrb_diagnostic/hmm_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {out_path}")
