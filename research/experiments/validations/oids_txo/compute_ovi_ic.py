"""
TXO OVI → TMFD6 Overnight Return IC Test
Round 17 OIDS Stage 2

Computes daily aggregate OVI from TXO trade ticks using Lee-Ready classification,
then tests rank IC against TMFD6 overnight returns.
"""
import subprocess
import numpy as np
from scipy import stats

def ch_query(sql: str) -> str:
    """Run a ClickHouse query and return stdout."""
    result = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client",
         "--query", sql],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"CH error: {result.stderr[:500]}")
    return result.stdout.strip()


def get_tmfd6_prices() -> dict:
    """Get TMFD6 daily open/close prices."""
    raw = ch_query("""
        SELECT toDate(exch_ts/1e9) as day,
               argMin(price_scaled, exch_ts) as open_px,
               argMax(price_scaled, exch_ts) as close_px
        FROM hft.market_data
        WHERE symbol = 'TMFD6' AND type = 'Tick' AND price_scaled > 0
        GROUP BY day ORDER BY day
        SETTINGS max_memory_usage = 4500000000,
                 max_bytes_before_external_sort = 1000000000
    """)
    prices = {}
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        day = parts[0]
        prices[day] = {
            "open": int(parts[1]),
            "close": int(parts[2]),
        }
    return prices


def get_txo_ticks_with_quotes(suffix_filter: str, date: str) -> list:
    """
    Get TXO ticks with nearest prior BidAsk mid-price for Lee-Ready.
    Returns list of (symbol, price, volume, mid_price).
    """
    # Get ticks
    tick_raw = ch_query(f"""
        SELECT symbol, exch_ts, price_scaled, volume
        FROM hft.market_data
        WHERE symbol LIKE 'TXO%{suffix_filter}'
          AND type = 'Tick'
          AND toDate(exch_ts/1e9) = '{date}'
          AND price_scaled > 0 AND volume > 0
        ORDER BY symbol, exch_ts
        SETTINGS max_memory_usage = 4500000000
    """)
    if not tick_raw:
        return []

    ticks = []
    for line in tick_raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        ticks.append({
            "symbol": parts[0],
            "ts": int(parts[1]),
            "price": int(parts[2]),
            "volume": int(parts[3]),
        })

    # For each unique symbol, get BidAsk quotes for the day
    symbols = sorted(set(t["symbol"] for t in ticks))
    quotes_by_sym = {}
    for sym in symbols:
        q_raw = ch_query(f"""
            SELECT exch_ts,
                   bids_price[1] as bid1,
                   asks_price[1] as ask1
            FROM hft.market_data
            WHERE symbol = '{sym}'
              AND type = 'BidAsk'
              AND toDate(exch_ts/1e9) = '{date}'
              AND bids_price[1] > 0 AND asks_price[1] > 0
            ORDER BY exch_ts
            SETTINGS max_memory_usage = 4500000000
        """)
        quotes = []
        if q_raw:
            for line in q_raw.split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                quotes.append({
                    "ts": int(parts[0]),
                    "mid": (int(parts[1]) + int(parts[2])) / 2,
                })
        quotes_by_sym[sym] = quotes

    # Lee-Ready: classify each tick
    results = []
    for t in ticks:
        sym_quotes = quotes_by_sym.get(t["symbol"], [])
        if not sym_quotes:
            continue
        # Find nearest prior quote (or same timestamp)
        mid = None
        for q in sym_quotes:
            if q["ts"] <= t["ts"]:
                mid = q["mid"]
            else:
                break
        if mid is None:
            # Use first available quote
            mid = sym_quotes[0]["mid"]

        results.append({
            "symbol": t["symbol"],
            "price": t["price"],
            "volume": t["volume"],
            "mid": mid,
        })
    return results


def classify_trade(price: int, mid: float) -> str:
    """Lee-Ready classification: buy if price > mid, sell if price < mid."""
    if price > mid:
        return "buy"
    elif price < mid:
        return "sell"
    else:
        return "mid"  # ambiguous


def is_call(symbol: str) -> bool:
    """Determine if TXO symbol is a call option based on TAIFEX encoding."""
    # TXO{strike}{month_letter}{year_digit}
    # Call month codes: A=Jan, B=Feb, C=Mar, D=Apr, ...
    # Put month codes: M=Jan, N=Feb, O=Mar, P=Apr, ...
    month_letter = symbol[-2]
    return month_letter in "ABCDEFGHIJKL"


def compute_daily_ovi(trades: list) -> dict:
    """
    Compute OVI from classified trades.
    Bullish = call_buy + put_sell
    Bearish = call_sell + put_buy
    OVI = (bullish - bearish) / (bullish + bearish)
    """
    bullish_vol = 0
    bearish_vol = 0
    n_classified = 0
    n_mid = 0

    for t in trades:
        direction = classify_trade(t["price"], t["mid"])
        call = is_call(t["symbol"])
        vol = t["volume"]

        if direction == "mid":
            n_mid += 1
            continue

        n_classified += 1
        if call:
            if direction == "buy":
                bullish_vol += vol
            else:
                bearish_vol += vol
        else:  # put
            if direction == "sell":
                bullish_vol += vol
            else:  # buy
                bearish_vol += vol

    total = bullish_vol + bearish_vol
    if total == 0:
        ovi = 0.0
    else:
        ovi = (bullish_vol - bearish_vol) / total

    return {
        "ovi": ovi,
        "bullish_vol": bullish_vol,
        "bearish_vol": bearish_vol,
        "total_vol": total,
        "n_classified": n_classified,
        "n_mid": n_mid,
    }


def main():
    print("=" * 60)
    print("TXO OVI → TMFD6 Overnight Return IC Test")
    print("=" * 60)

    # Get TMFD6 prices
    tmf_prices = get_tmfd6_prices()
    tmf_days = sorted(tmf_prices.keys())
    print(f"\nTMFD6 trading days: {len(tmf_days)}")
    print(f"  Range: {tmf_days[0]} to {tmf_days[-1]}")

    # Define test dates — all dates with BOTH TXO ticks and TMFD6 data
    # Period 1: N6 (Feb put only) — Jan 26 to Feb 10
    n6_dates = [
        "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30", "2026-01-31",
        "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06", "2026-02-10",
    ]
    # Period 2: D6+P6 (Apr call+put) — Mar 20 to Mar 25
    dp6_dates = [
        "2026-03-20", "2026-03-23", "2026-03-24", "2026-03-25",
    ]
    # Skip Jan 26 (only 9 TXO ticks) and Mar 26 (today, incomplete)

    all_results = []

    # Period 1: N6 only (put-only OVI — we can only compute put direction)
    print("\n--- Period 1: N6 (Feb put only) ---")
    for date in n6_dates:
        trades = get_txo_ticks_with_quotes("N6", date)
        if not trades:
            print(f"  {date}: no trades found")
            continue
        ovi_result = compute_daily_ovi(trades)

        # Get overnight return: close(d) -> open(d+1)
        idx = tmf_days.index(date) if date in tmf_days else -1
        if idx < 0 or idx + 1 >= len(tmf_days):
            print(f"  {date}: no TMFD6 match or no next day")
            continue

        close_d = tmf_prices[tmf_days[idx]]["close"]
        next_day = tmf_days[idx + 1]
        open_d1 = tmf_prices[next_day]["open"]
        overnight_ret = (open_d1 - close_d) / close_d

        all_results.append({
            "date": date,
            "period": "N6",
            "ovi": ovi_result["ovi"],
            "total_vol": ovi_result["total_vol"],
            "n_trades": ovi_result["n_classified"],
            "overnight_ret": overnight_ret,
            "next_day": next_day,
        })

        print(f"  {date}: OVI={ovi_result['ovi']:+.4f} "
              f"(vol={ovi_result['total_vol']}, n={ovi_result['n_classified']}+{ovi_result['n_mid']}mid) "
              f"→ {next_day} overnight_ret={overnight_ret*10000:+.1f}bps")

    # Period 2: D6+P6 (both calls and puts)
    print("\n--- Period 2: D6+P6 (Apr call+put) ---")
    for date in dp6_dates:
        # Get both call (D6) and put (P6) ticks
        trades_d6 = get_txo_ticks_with_quotes("D6", date)
        trades_p6 = get_txo_ticks_with_quotes("P6", date)
        trades = trades_d6 + trades_p6

        if not trades:
            print(f"  {date}: no trades found")
            continue
        ovi_result = compute_daily_ovi(trades)

        idx = tmf_days.index(date) if date in tmf_days else -1
        if idx < 0 or idx + 1 >= len(tmf_days):
            print(f"  {date}: no TMFD6 match or no next day")
            continue

        close_d = tmf_prices[tmf_days[idx]]["close"]
        next_day = tmf_days[idx + 1]
        open_d1 = tmf_prices[next_day]["open"]
        overnight_ret = (open_d1 - close_d) / close_d

        all_results.append({
            "date": date,
            "period": "D6P6",
            "ovi": ovi_result["ovi"],
            "total_vol": ovi_result["total_vol"],
            "n_trades": ovi_result["n_classified"],
            "overnight_ret": overnight_ret,
            "next_day": next_day,
        })

        print(f"  {date}: OVI={ovi_result['ovi']:+.4f} "
              f"(bull={ovi_result['bullish_vol']}, bear={ovi_result['bearish_vol']}, "
              f"n={ovi_result['n_classified']}+{ovi_result['n_mid']}mid) "
              f"→ {next_day} overnight_ret={overnight_ret*10000:+.1f}bps")

    # Compute IC
    print("\n" + "=" * 60)
    print("IC RESULTS")
    print("=" * 60)

    if len(all_results) < 5:
        print(f"\nFATAL: Only {len(all_results)} data points. Cannot compute meaningful IC.")
        print("KILL GATE: < 20 independent observations")
        return

    ovis = np.array([r["ovi"] for r in all_results])
    rets = np.array([r["overnight_ret"] for r in all_results])

    # Rank IC (Spearman)
    rank_ic, p_value = stats.spearmanr(ovis, rets)
    # Pearson IC
    pearson_ic, pearson_p = stats.pearsonr(ovis, rets)

    print(f"\nN observations: {len(all_results)}")
    print(f"Rank IC (Spearman): {rank_ic:+.4f}  (p={p_value:.4f})")
    print(f"Pearson IC:         {pearson_ic:+.4f}  (p={pearson_p:.4f})")
    print(f"OVI range: [{ovis.min():+.4f}, {ovis.max():+.4f}]")
    print(f"Return range: [{rets.min()*10000:+.1f}, {rets.max()*10000:+.1f}] bps")

    if len(all_results) < 20:
        print(f"\nWARNING: Only {len(all_results)} observations — below 20-day kill gate.")
        print("Results are statistically meaningless at this sample size.")

    if abs(rank_ic) < 0.05:
        print(f"\nKILL GATE: |IC| = {abs(rank_ic):.4f} < 0.05 threshold")
    else:
        print(f"\nIC passes threshold: |IC| = {abs(rank_ic):.4f} >= 0.05")
        print("BUT sample size is far too small for any conclusion.")


if __name__ == "__main__":
    main()
