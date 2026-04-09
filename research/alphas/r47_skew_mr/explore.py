"""R47 TXO Data Exploration — bid-ask spread profiling and IV skew dynamics.

Critical prerequisite for C1 (IV Skew Mean-Reversion via Vertical Spreads).

Analyzes:
1. TXO bid-ask spreads across moneyness buckets
2. Spread distribution by tick regime (premium < 50 vs >= 50)
3. IV skew time series using Black-76 implied vol
4. Skew mean-reversion half-life estimation
5. Strike/expiry cross-section richness

Usage:
    CLICKHOUSE_PASSWORD=changeme python -m research.alphas.r47_skew_mr.explore
"""

from __future__ import annotations

import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime

import numpy as np
from scipy.stats import norm


# ---------------------------------------------------------------------------
# ClickHouse client
# ---------------------------------------------------------------------------

def _get_ch_client():
    """Get ClickHouse client with env-based config."""
    try:
        import clickhouse_connect
    except ImportError:
        print("ERROR: clickhouse_connect not installed. pip install clickhouse-connect")
        sys.exit(1)

    return clickhouse_connect.get_client(
        host=os.environ.get("HFT_CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("HFT_CLICKHOUSE_HTTP_PORT", "8123")),
        username="default",
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
    )


def _query(client, sql: str) -> list[tuple]:
    """Execute SQL and return rows as list of tuples."""
    result = client.query(sql)
    return [tuple(row) for row in result.result_rows]


# ---------------------------------------------------------------------------
# Black-76 IV solver (standalone, no platform import to keep explore portable)
# ---------------------------------------------------------------------------

_CDF = norm.cdf
_PDF = norm.pdf


def _b76_price(F: float, K: float, T: float, sigma: float, cp: int) -> float:
    """Black-76 price. cp: +1=call, -1=put."""
    if T <= 0 or sigma <= 0:
        return max(cp * (F - K), 0.0)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return cp * (F * _CDF(cp * d1) - K * _CDF(cp * d2))


def _b76_vega(F: float, K: float, T: float, sigma: float) -> float:
    """Black-76 vega (derivative w.r.t. sigma)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * sqrt_T)
    return F * sqrt_T * _PDF(d1)


def implied_vol(F: float, K: float, T: float, price: float, cp: int,
                max_iter: int = 50, tol: float = 1e-6) -> float:
    """Newton-Raphson IV solver. Returns NaN on failure."""
    if T <= 0 or price <= 0 or F <= 0 or K <= 0:
        return float("nan")
    intrinsic = max(cp * (F - K), 0.0)
    if price < intrinsic - 1e-8:
        return float("nan")
    # Brenner-Subrahmanyam initial guess
    sigma = math.sqrt(2.0 * math.pi / T) * price / F
    sigma = max(0.05, min(sigma, 3.0))
    for _ in range(max_iter):
        p = _b76_price(F, K, T, sigma, cp)
        v = _b76_vega(F, K, T, sigma)
        if v < 1e-12:
            break
        sigma -= (p - price) / v
        if sigma <= 0.001:
            sigma = 0.001
        if abs(p - price) < tol:
            return sigma
    return float("nan") if abs(_b76_price(F, K, T, sigma, cp) - price) > 0.5 else sigma


# ---------------------------------------------------------------------------
# 1. Load TXO BidAsk data
# ---------------------------------------------------------------------------

def load_txo_bidask(client, sample_every: int = 10) -> list[dict]:
    """Load TXO BidAsk quotes from ClickHouse (sampled for performance).

    Args:
        sample_every: take every Nth row (default 10 = ~5M rows from 50M).

    Returns list of dicts with keys:
        symbol, exch_ts, bid, ask, strike_scaled, option_right, expiry
    """
    print(f"Loading TXO BidAsk data (sampling 1/{sample_every})...")

    # Use row_number() modulo for deterministic sampling
    sql = f"""
    SELECT
        symbol,
        exch_ts,
        bids_price[1] as bid,
        asks_price[1] as ask,
        strike_scaled,
        option_right,
        expiry
    FROM hft.market_data
    WHERE symbol LIKE 'TXO%'
      AND type = 'BidAsk'
      AND length(bids_price) > 0
      AND length(asks_price) > 0
      AND bids_price[1] > 0
      AND asks_price[1] > 0
      AND cityHash64(exch_ts, symbol) % {sample_every} = 0
    ORDER BY exch_ts
    """
    try:
        rows = _query(client, sql)
    except Exception:
        # Fallback: try without instrument columns (older schema)
        sql_fallback = f"""
        SELECT
            symbol,
            exch_ts,
            bids_price[1] as bid,
            asks_price[1] as ask,
            0 as strike_scaled,
            '' as option_right,
            toDate('1970-01-01') as expiry
        FROM hft.market_data
        WHERE symbol LIKE 'TXO%'
          AND type = 'BidAsk'
          AND length(bids_price) > 0
          AND length(asks_price) > 0
          AND bids_price[1] > 0
          AND asks_price[1] > 0
          AND cityHash64(exch_ts, symbol) % {sample_every} = 0
        ORDER BY exch_ts
        """
        rows = _query(client, sql_fallback)

    if not rows:
        print("ERROR: No TXO BidAsk data found. Trying Tick data...")
        return []

    print(f"  Loaded {len(rows):,} TXO BidAsk rows")
    return [
        {
            "symbol": r[0],
            "exch_ts": r[1],
            "bid": r[2],
            "ask": r[3],
            "strike_scaled": r[4],
            "option_right": r[5],
            "expiry": r[6],
        }
        for r in rows
    ]


def load_txo_count(client) -> dict:
    """Quick count of TXO data by type."""
    sql = """
    SELECT type, count() as cnt
    FROM hft.market_data
    WHERE symbol LIKE 'TXO%'
    GROUP BY type
    ORDER BY cnt DESC
    """
    rows = _query(client, sql)
    return {r[0]: r[1] for r in rows}


def load_txo_symbols(client) -> list[tuple]:
    """Get distinct TXO symbols with row counts."""
    sql = """
    SELECT symbol, count() as cnt
    FROM hft.market_data
    WHERE symbol LIKE 'TXO%'
    GROUP BY symbol
    ORDER BY cnt DESC
    LIMIT 100
    """
    return _query(client, sql)


def load_tx_futures_mid(client) -> dict[int, float]:
    """Load TX futures mid prices indexed by 5-minute bucket.

    Returns dict: bucket_ts -> mid_price (float, unscaled).
    """
    print("Loading TX futures reference prices...")
    sql = """
    SELECT
        toInt64(floor(exch_ts / 300000000000) * 300000000000) as bucket,
        avg((bids_price[1] + asks_price[1]) / 2.0) as mid
    FROM hft.market_data
    WHERE (symbol = 'TXFD6' OR symbol = 'TXF')
      AND type = 'BidAsk'
      AND length(bids_price) > 0
      AND length(asks_price) > 0
      AND bids_price[1] > 0
    GROUP BY bucket
    ORDER BY bucket
    """
    rows = _query(client, sql)
    if not rows:
        print("  WARNING: No TX futures data found")
        return {}
    print(f"  Loaded {len(rows):,} 5-min TX futures buckets")
    # prices are x1e6 in CK
    return {int(r[0]): float(r[1]) / 1_000_000.0 for r in rows}


# ---------------------------------------------------------------------------
# 2. Spread analysis
# ---------------------------------------------------------------------------

def analyze_spreads(data: list[dict]) -> None:
    """Profile bid-ask spreads across moneyness and tick regime."""
    print("\n" + "=" * 70)
    print("SECTION 1: BID-ASK SPREAD ANALYSIS")
    print("=" * 70)

    spreads = []
    mids = []
    for row in data:
        bid = row["bid"] / 1_000_000.0  # unscale from CK x1e6
        ask = row["ask"] / 1_000_000.0
        spread = ask - bid
        mid = (bid + ask) / 2.0
        if spread > 0 and mid > 0:
            spreads.append(spread)
            mids.append(mid)

    spreads = np.array(spreads)
    mids = np.array(mids)
    n = len(spreads)
    print(f"\nTotal valid quotes: {n:,}")

    if n == 0:
        print("No valid spread data. Aborting spread analysis.")
        return

    # Overall spread stats
    print(f"\n--- Overall Spread Statistics (pts) ---")
    print(f"  Mean:   {np.mean(spreads):.2f}")
    print(f"  Median: {np.median(spreads):.2f}")
    print(f"  P25:    {np.percentile(spreads, 25):.2f}")
    print(f"  P75:    {np.percentile(spreads, 75):.2f}")
    print(f"  P95:    {np.percentile(spreads, 95):.2f}")
    print(f"  Max:    {np.max(spreads):.2f}")

    # By tick regime (premium < 50 vs >= 50)
    print(f"\n--- Spread by Tick Regime ---")
    mask_low = mids < 50
    mask_high = mids >= 50
    for label, mask in [("Premium < 50 (1pt tick)", mask_low),
                        ("Premium >= 50 (5pt tick)", mask_high)]:
        s = spreads[mask]
        if len(s) == 0:
            print(f"  {label}: NO DATA")
            continue
        print(f"  {label}: n={len(s):,}")
        print(f"    Mean={np.mean(s):.2f}  Median={np.median(s):.2f}  "
              f"P95={np.percentile(s, 95):.2f}")

    # By moneyness bucket (using mid price as proxy for moneyness)
    # Lower premium = more OTM, higher premium = more ATM/ITM
    print(f"\n--- Spread by Premium Bucket ---")
    buckets = [
        ("Deep OTM (0-5 pts)", 0, 5),
        ("OTM (5-20 pts)", 5, 20),
        ("Near OTM (20-50 pts)", 20, 50),
        ("ATM (50-200 pts)", 50, 200),
        ("ITM (200-500 pts)", 200, 500),
        ("Deep ITM (500+ pts)", 500, 1e9),
    ]
    for label, lo, hi in buckets:
        mask = (mids >= lo) & (mids < hi)
        s = spreads[mask]
        if len(s) == 0:
            print(f"  {label}: NO DATA")
            continue
        cost_ntd = np.mean(s) * 50  # cost in NTD per crossing
        print(f"  {label}: n={len(s):,}")
        print(f"    Spread: Mean={np.mean(s):.2f}  Median={np.median(s):.2f}  "
              f"P95={np.percentile(s, 95):.2f}")
        print(f"    Crossing cost: {cost_ntd:.0f} NTD (mean)")

    # Spread as % of premium
    print(f"\n--- Spread as % of Mid ---")
    pct = (spreads / mids) * 100
    for label, lo, hi in buckets:
        mask = (mids >= lo) & (mids < hi)
        p = pct[mask]
        if len(p) == 0:
            continue
        print(f"  {label}: Mean={np.mean(p):.1f}%  Median={np.median(p):.1f}%")


# ---------------------------------------------------------------------------
# 3. Cross-section richness
# ---------------------------------------------------------------------------

def analyze_cross_section(data: list[dict]) -> None:
    """Analyze how many strikes/expiries are quoted simultaneously."""
    print("\n" + "=" * 70)
    print("SECTION 2: CROSS-SECTION RICHNESS")
    print("=" * 70)

    # Group by 5-minute bucket
    buckets: dict[int, set] = defaultdict(set)
    sym_by_bucket: dict[int, set] = defaultdict(set)
    for row in data:
        bucket = int(row["exch_ts"]) // 300_000_000_000
        sym = row["symbol"]
        buckets[bucket].add(sym)
        sym_by_bucket[bucket].add(sym)

    n_buckets = len(buckets)
    syms_per_bucket = [len(v) for v in buckets.values()]

    print(f"\n5-minute buckets with TXO data: {n_buckets:,}")
    if not syms_per_bucket:
        print("No cross-section data.")
        return

    arr = np.array(syms_per_bucket)
    print(f"Symbols quoted per 5-min bucket:")
    print(f"  Mean: {np.mean(arr):.1f}")
    print(f"  Median: {np.median(arr):.1f}")
    print(f"  Min: {np.min(arr)}")
    print(f"  Max: {np.max(arr)}")
    print(f"  P25: {np.percentile(arr, 25):.0f}")
    print(f"  P75: {np.percentile(arr, 75):.0f}")

    # Parse symbol to extract strike and right
    # Format: TXO<strike><month_code><year_digit>
    # e.g., TXO22500D6 -> strike=22500, right=C (D=Apr call)
    call_months = set("ABCDEFGHIJKL")
    put_months = set("MNOPQRSTUVWX")

    strikes_per_bucket = []
    for bucket, syms in buckets.items():
        strikes = set()
        for s in syms:
            if not s.startswith("TXO"):
                continue
            # Extract strike: digits between TXO and the month code
            rest = s[3:]  # e.g., "22500D6"
            digits = ""
            for ch in rest:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                strikes.add(int(digits))
        strikes_per_bucket.append(len(strikes))

    if strikes_per_bucket:
        arr_s = np.array(strikes_per_bucket)
        print(f"\nDistinct strikes per 5-min bucket:")
        print(f"  Mean: {np.mean(arr_s):.1f}")
        print(f"  Median: {np.median(arr_s):.1f}")
        print(f"  Min: {np.min(arr_s)}")
        print(f"  Max: {np.max(arr_s)}")


# ---------------------------------------------------------------------------
# 4. IV Skew time series
# ---------------------------------------------------------------------------

def compute_skew_timeseries(
    data: list[dict],
    tx_mid: dict[int, float],
) -> tuple[np.ndarray, np.ndarray]:
    """Compute IV skew time series from TXO BidAsk data.

    For each 5-minute bucket:
    1. Get TX futures mid as underlying F
    2. For all TXO quotes in that bucket, compute mid price and IV
    3. Find OTM put nearest to 25-delta and OTM call nearest to 25-delta
    4. Skew = IV_put - IV_call

    Returns (timestamps_ns, skew_values).
    """
    print("\n" + "=" * 70)
    print("SECTION 3: IV SKEW TIME SERIES")
    print("=" * 70)

    if not tx_mid:
        print("No TX futures data — cannot compute IV. Skipping.")
        return np.array([]), np.array([])

    # Parse TXO symbols
    call_months = "ABCDEFGHIJKL"
    put_months = "MNOPQRSTUVWX"

    def parse_txo(sym: str) -> tuple[int, str] | None:
        """Parse TXO symbol -> (strike, 'C'|'P') or None."""
        if not sym.startswith("TXO"):
            return None
        rest = sym[3:]
        digits = ""
        month_char = ""
        for ch in rest:
            if ch.isdigit() and not month_char:
                digits += ch
            elif ch.isalpha() and not month_char:
                month_char = ch
            # skip year digit
        if not digits or not month_char:
            return None
        strike = int(digits)
        if month_char.upper() in call_months:
            return (strike, "C")
        elif month_char.upper() in put_months:
            return (strike, "P")
        return None

    # Group quotes by 5-min bucket
    bucket_data: dict[int, list] = defaultdict(list)
    for row in data:
        bucket = int(row["exch_ts"]) // 300_000_000_000
        bid = row["bid"] / 1_000_000.0
        ask = row["ask"] / 1_000_000.0
        mid = (bid + ask) / 2.0
        parsed = parse_txo(row["symbol"])
        if parsed and mid > 0.5:  # filter out near-zero quotes
            strike, right = parsed
            bucket_data[bucket].append((strike, right, mid))

    print(f"5-min buckets with parseable TXO quotes: {len(bucket_data):,}")

    # For each bucket, compute skew
    timestamps = []
    skew_values = []
    n_computed = 0
    n_skipped = 0

    # Assume ~20 trading days to expiry as rough T estimate
    # More precise: use expiry date from symbol, but for exploration this suffices
    T_default = 20.0 / 252.0  # ~20 trading days

    for bucket in sorted(bucket_data.keys()):
        bucket_ts = bucket * 300_000_000_000
        F = tx_mid.get(bucket_ts)
        if F is None or F <= 0:
            n_skipped += 1
            continue

        quotes = bucket_data[bucket]
        # Separate calls and puts
        call_ivs: dict[int, float] = {}
        put_ivs: dict[int, float] = {}

        for strike, right, mid_price in quotes:
            cp = 1 if right == "C" else -1
            iv = implied_vol(F, float(strike), T_default, mid_price, cp)
            if not math.isnan(iv) and 0.05 < iv < 2.0:
                if right == "C":
                    call_ivs[strike] = iv
                else:
                    put_ivs[strike] = iv

        # Find approximate 25-delta strikes
        # 25-delta call: strike where delta ~ 0.25 (OTM call, strike > F)
        # 25-delta put: strike where delta ~ -0.25 (OTM put, strike < F)
        # Approximate: N(d1) = 0.25 for call -> d1 ~ -0.674
        # Strike ~ F * exp(0.674 * sigma * sqrt(T) + 0.5 * sigma^2 * T)
        # For simplicity, use strikes ~2-5% OTM

        atm_strike = round(F / 50) * 50  # nearest 50-pt strike
        otm_call_strikes = sorted([k for k in call_ivs if k > atm_strike])
        otm_put_strikes = sorted([k for k in put_ivs if k < atm_strike], reverse=True)

        if not otm_call_strikes or not otm_put_strikes:
            n_skipped += 1
            continue

        # Take the 2nd OTM strike if available (roughly 25D), else 1st
        call_strike = otm_call_strikes[1] if len(otm_call_strikes) > 1 else otm_call_strikes[0]
        put_strike = otm_put_strikes[1] if len(otm_put_strikes) > 1 else otm_put_strikes[0]

        call_iv = call_ivs[call_strike]
        put_iv = put_ivs[put_strike]

        skew = put_iv - call_iv
        timestamps.append(bucket_ts)
        skew_values.append(skew)
        n_computed += 1

    print(f"Skew computed: {n_computed:,} buckets, skipped: {n_skipped:,}")

    ts = np.array(timestamps, dtype=np.int64)
    sk = np.array(skew_values, dtype=np.float64)

    if len(sk) > 10:
        print(f"\n--- IV Skew Statistics ---")
        print(f"  Mean:   {np.mean(sk):.4f}")
        print(f"  Std:    {np.std(sk):.4f}")
        print(f"  Median: {np.median(sk):.4f}")
        print(f"  P5:     {np.percentile(sk, 5):.4f}")
        print(f"  P95:    {np.percentile(sk, 95):.4f}")
        print(f"  Min:    {np.min(sk):.4f}")
        print(f"  Max:    {np.max(sk):.4f}")

    return ts, sk


# ---------------------------------------------------------------------------
# 5. Skew mean-reversion analysis
# ---------------------------------------------------------------------------

def analyze_skew_mr(ts: np.ndarray, skew: np.ndarray) -> None:
    """Estimate skew mean-reversion half-life via AR(1) regression."""
    print("\n" + "=" * 70)
    print("SECTION 4: SKEW MEAN-REVERSION ANALYSIS")
    print("=" * 70)

    if len(skew) < 30:
        print(f"Insufficient data ({len(skew)} points). Need >= 30.")
        return

    # AR(1) on skew: skew_t = alpha + beta * skew_{t-1} + eps
    # Half-life = -log(2) / log(beta) in units of sampling interval (5 min)
    y = skew[1:]
    x = skew[:-1]
    n = len(y)

    # OLS: beta = cov(x,y) / var(x), alpha = mean(y) - beta * mean(x)
    mx, my = np.mean(x), np.mean(y)
    beta = np.sum((x - mx) * (y - my)) / np.sum((x - mx) ** 2)
    alpha = my - beta * mx

    residuals = y - (alpha + beta * x)
    r_squared = 1.0 - np.sum(residuals**2) / np.sum((y - my)**2)

    print(f"\nAR(1) regression: skew_t = {alpha:.6f} + {beta:.4f} * skew_{{t-1}}")
    print(f"  R-squared: {r_squared:.4f}")
    print(f"  beta: {beta:.4f}")

    if beta <= 0 or beta >= 1:
        print(f"  WARNING: beta={beta:.4f} outside (0,1). Skew may NOT be mean-reverting.")
        if beta >= 1:
            print("  beta >= 1 implies unit root / random walk. KILL signal for C1.")
        return

    half_life_buckets = -math.log(2) / math.log(beta)
    half_life_minutes = half_life_buckets * 5
    half_life_hours = half_life_minutes / 60

    print(f"\n--- Mean-Reversion Half-Life ---")
    print(f"  Half-life: {half_life_buckets:.1f} buckets ({half_life_minutes:.0f} min / {half_life_hours:.1f} hr)")

    if half_life_hours < 0.5:
        print("  WARNING: Very fast MR. May be bid-ask bounce noise, not real signal.")
    elif half_life_hours < 4:
        print("  GOOD: MR within intraday trading horizon. Suitable for C1.")
    elif half_life_hours < 24:
        print("  OK: MR within 1-day horizon. Overnight hold may be needed.")
    else:
        print("  SLOW: MR > 1 day. May need multi-day holding period.")

    # Long-term mean
    if abs(1 - beta) > 1e-8:
        long_term_mean = alpha / (1 - beta)
        print(f"  Long-term mean skew: {long_term_mean:.4f}")

    # Z-score analysis
    print(f"\n--- Skew Z-Score Distribution ---")
    # Rolling z-score with window = 2 * half-life
    window = max(10, int(2 * half_life_buckets))
    if len(skew) > window + 10:
        z_scores = []
        for i in range(window, len(skew)):
            w = skew[i - window:i]
            mu = np.mean(w)
            std = np.std(w)
            if std > 1e-8:
                z_scores.append((skew[i] - mu) / std)
        z = np.array(z_scores)
        print(f"  Window: {window} buckets ({window * 5} min)")
        print(f"  Z-score mean: {np.mean(z):.3f}  std: {np.std(z):.3f}")
        print(f"  |z| > 1.0: {np.sum(np.abs(z) > 1.0):,} ({100*np.mean(np.abs(z) > 1.0):.1f}%)")
        print(f"  |z| > 1.5: {np.sum(np.abs(z) > 1.5):,} ({100*np.mean(np.abs(z) > 1.5):.1f}%)")
        print(f"  |z| > 2.0: {np.sum(np.abs(z) > 2.0):,} ({100*np.mean(np.abs(z) > 2.0):.1f}%)")

        # Forward return of skew after extreme z
        print(f"\n--- Skew Change After Extreme Z ---")
        for thresh in [1.0, 1.5, 2.0]:
            # Look at skew change 1, 6, 12, 24 buckets forward (5min, 30min, 1hr, 2hr)
            for fwd in [1, 6, 12, 24]:
                if len(z) <= fwd:
                    continue
                z_trim = z[:len(z) - fwd]
                skew_start = skew[window:window + len(z_trim)]
                skew_fwd = skew[window + fwd:window + fwd + len(z_trim)]
                delta_skew = skew_fwd - skew_start

                mask_high = z_trim > thresh
                mask_low = z_trim < -thresh
                n_high = np.sum(mask_high)
                n_low = np.sum(mask_low)
                if n_high > 5:
                    mr_high = np.mean(delta_skew[mask_high])
                else:
                    mr_high = float("nan")
                if n_low > 5:
                    mr_low = np.mean(delta_skew[mask_low])
                else:
                    mr_low = float("nan")
                fwd_min = fwd * 5
                print(f"  |z|>{thresh} fwd={fwd_min}min: "
                      f"high(n={n_high}) dSkew={mr_high:+.4f}  "
                      f"low(n={n_low}) dSkew={mr_low:+.4f}")


# ---------------------------------------------------------------------------
# 6. Day-by-day summary
# ---------------------------------------------------------------------------

def day_summary(data: list[dict]) -> None:
    """Print per-day row counts and trading hours."""
    print("\n" + "=" * 70)
    print("SECTION 5: DAY-BY-DAY SUMMARY")
    print("=" * 70)

    day_counts: dict[str, int] = defaultdict(int)
    for row in data:
        ts_s = int(row["exch_ts"]) // 1_000_000_000
        day = datetime.utcfromtimestamp(ts_s).strftime("%Y-%m-%d")
        day_counts[day] += 1

    print(f"\nTrading days: {len(day_counts)}")
    for day in sorted(day_counts.keys()):
        print(f"  {day}: {day_counts[day]:>10,} quotes")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Run all exploration analyses."""
    print("=" * 70)
    print("R47 TXO DATA EXPLORATION")
    print("C1 Prerequisite: Bid-Ask Spread Profiling + IV Skew Dynamics")
    print("=" * 70)

    client = _get_ch_client()

    # Quick overview
    print("\n--- TXO Data Overview ---")
    counts = load_txo_count(client)
    for typ, cnt in counts.items():
        print(f"  {typ}: {cnt:,}")

    syms = load_txo_symbols(client)
    print(f"\nDistinct TXO symbols: {len(syms)}")
    if syms:
        print("Top 10 by row count:")
        for sym, cnt in syms[:10]:
            print(f"  {sym}: {cnt:,}")

    # Load BidAsk data
    data = load_txo_bidask(client)
    if not data:
        print("\nFATAL: No TXO BidAsk data. Cannot proceed.")
        print("Check: does hft.market_data contain TXO% symbols with type='BidAsk'?")
        sys.exit(1)

    # Run analyses
    analyze_spreads(data)
    analyze_cross_section(data)
    day_summary(data)

    # IV Skew analysis
    tx_mid = load_tx_futures_mid(client)
    ts, skew = compute_skew_timeseries(data, tx_mid)
    if len(skew) > 10:
        analyze_skew_mr(ts, skew)

    # Final verdict
    print("\n" + "=" * 70)
    print("EXPLORATION COMPLETE")
    print("=" * 70)
    print("Key questions for C1 viability:")
    print("  1. Are OTM spreads <= 3 pts? (Check Section 1)")
    print("  2. Are there >= 5 strikes per bucket? (Check Section 2)")
    print("  3. Is skew beta < 1? (Check Section 4 — if beta >= 1, KILL C1)")
    print("  4. Is half-life < 24 hours? (Check Section 4)")
    print("  5. Does skew revert after extreme z? (Check Section 4)")


if __name__ == "__main__":
    main()
