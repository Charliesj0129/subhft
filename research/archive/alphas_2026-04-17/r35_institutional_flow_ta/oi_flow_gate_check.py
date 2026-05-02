"""
R35 OI Flow Gate Check: Does dealer OI distribution predict next-day TXFD6 returns?

Signals tested:
1. dealer_net (aggregate daily dealer net OI)
2. dealer_frac (aggregate daily dealer fraction)
3. d_dealer_net (daily change in dealer net OI)
4. d_dealer_frac (daily change in dealer fraction)
5. total_oi (total options OI)
6. d_total_oi (daily change in total OI)
7. put_call_oi_ratio (put OI / call OI from strike data)
8. oi_com (OI center-of-mass: OI-weighted avg strike)
9. d_oi_com (daily change in OI COM)

Gate criteria (daily frequency, N~58):
- |IC| > 0.05 for any signal -> PROCEED
- |t-stat| > 1.5 (relaxed for small N)
- Sign stability: >60% of rolling 20-day windows
"""

import os
import sys
import csv
import glob
import numpy as np
from pathlib import Path
from scipy import stats

BASE = Path("/home/charlie/hft_platform")
OI_DIR = BASE / "research/data/raw/taifex_oi"
OUT_DIR = BASE / "outputs/team_artifacts/alpha-research-r35"


def load_scrape_summary():
    """Load aggregate daily OI signals from scrape_summary.csv"""
    rows = []
    with open(OI_DIR / "scrape_summary.csv") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "date": row["date"],
                "n_strikes": int(row["n_strikes"]),
                "total_oi": int(row["total_oi"]),
                "dealer_net": int(row["dealer_net"]),
                "dealer_frac": float(row["dealer_frac"]),
            })
    return sorted(rows, key=lambda x: x["date"])


def load_strike_level_oi():
    """Load per-file strike-level OI data to compute put/call ratio and COM."""
    files = sorted(glob.glob(str(OI_DIR / "*_strike_oi.csv")))
    daily = {}
    for fpath in files:
        fname = os.path.basename(fpath)
        if fname == "scrape_summary.csv":
            continue
        date = fname.replace("_strike_oi.csv", "")

        put_oi = 0
        call_oi = 0
        weighted_strike_sum = 0.0
        total_oi_for_com = 0

        with open(fpath) as f:
            reader = csv.DictReader(f)
            for row in reader:
                strike = int(row["strike"])
                oi = int(row["total_oi"])
                cp = row["cp"]

                if cp == "P":
                    put_oi += oi
                elif cp == "C":
                    call_oi += oi

                weighted_strike_sum += strike * oi
                total_oi_for_com += oi

        pc_ratio = put_oi / call_oi if call_oi > 0 else float("nan")
        com = weighted_strike_sum / total_oi_for_com if total_oi_for_com > 0 else float("nan")

        daily[date] = {
            "put_oi": put_oi,
            "call_oi": call_oi,
            "pc_ratio": pc_ratio,
            "oi_com": com,
        }
    return daily


def load_txfd6_prices_clickhouse():
    """Try to get TXFD6 daily close from ClickHouse."""
    import subprocess
    query = """
    SELECT
        toDate(exch_ts/1e9) as d,
        argMax(price_scaled, exch_ts) / 10000.0 as close_px
    FROM hft.market_data
    WHERE symbol='TXFD6' AND type='Tick'
    GROUP BY d
    ORDER BY d
    """
    try:
        result = subprocess.run(
            ["docker", "exec", "clickhouse", "clickhouse-client", "--query", query],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return {}
        prices = {}
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            prices[parts[0]] = float(parts[1])
        return prices
    except Exception:
        return {}


def load_txfd6_prices_parquet():
    """Try to get TXFD6 daily close from golden parquet files."""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return {}

    parquet_dir = BASE / "research/data/real/golden/TXFD6"
    if not parquet_dir.exists():
        return {}

    prices = {}
    for pf in sorted(parquet_dir.glob("*.parquet")):
        date = pf.stem  # e.g., 2026-02-05
        try:
            table = pq.read_table(str(pf))
            df = table.to_pandas()
            # Golden parquet prices are x1,000,000 scale
            if "price" in df.columns:
                last_price = df["price"].iloc[-1] / 1_000_000.0
            elif "price_scaled" in df.columns:
                last_price = df["price_scaled"].iloc[-1] / 1_000_000.0
            else:
                # Try first numeric-looking column
                continue
            prices[date] = last_price
        except Exception:
            continue
    return prices


def compute_signals(summary_data, strike_data):
    """Compute all daily signals."""
    dates = [r["date"] for r in summary_data]
    signals = {d: {} for d in dates}

    # Aggregate signals from summary
    for i, row in enumerate(summary_data):
        d = row["date"]
        signals[d]["dealer_net"] = row["dealer_net"]
        signals[d]["dealer_frac"] = row["dealer_frac"]
        signals[d]["total_oi"] = row["total_oi"]

        if i > 0:
            prev = summary_data[i - 1]
            signals[d]["d_dealer_net"] = row["dealer_net"] - prev["dealer_net"]
            signals[d]["d_dealer_frac"] = row["dealer_frac"] - prev["dealer_frac"]
            signals[d]["d_total_oi"] = row["total_oi"] - prev["total_oi"]
        else:
            signals[d]["d_dealer_net"] = float("nan")
            signals[d]["d_dealer_frac"] = float("nan")
            signals[d]["d_total_oi"] = float("nan")

    # Strike-level signals
    sorted_dates = sorted(strike_data.keys())
    for i, d in enumerate(sorted_dates):
        if d in signals:
            signals[d]["pc_ratio"] = strike_data[d]["pc_ratio"]
            signals[d]["oi_com"] = strike_data[d]["oi_com"]
            if i > 0:
                prev_d = sorted_dates[i - 1]
                signals[d]["d_oi_com"] = strike_data[d]["oi_com"] - strike_data[prev_d]["oi_com"]
                signals[d]["d_pc_ratio"] = strike_data[d]["pc_ratio"] - strike_data[prev_d]["pc_ratio"]
            else:
                signals[d]["d_oi_com"] = float("nan")
                signals[d]["d_pc_ratio"] = float("nan")

    return signals


def run_gate_check(signals, prices):
    """Run IC gate check: signal(day t) vs return(day t+1)."""
    # Get sorted dates that have both signals and prices
    signal_dates = sorted(signals.keys())
    price_dates = sorted(prices.keys())

    # Compute daily returns: ret(t) = close(t) / close(t-1) - 1
    returns = {}
    for i in range(1, len(price_dates)):
        d = price_dates[i]
        d_prev = price_dates[i - 1]
        returns[d] = prices[d] / prices[d_prev] - 1.0

    # Find overlapping dates: signal on day t, return on day t+1
    # For each signal date, find the next available return date
    pairs = []
    for i, sd in enumerate(signal_dates):
        # Find next date with return data
        next_return_dates = [rd for rd in sorted(returns.keys()) if rd > sd]
        if next_return_dates:
            rd = next_return_dates[0]
            pairs.append((sd, rd))

    if len(pairs) < 5:
        return None, "Insufficient overlap: {} pairs".format(len(pairs))

    # Extract signal names
    sample_signals = signals[pairs[0][0]]
    signal_names = [k for k in sample_signals.keys() if k not in ("date",)]

    results = {}
    for sname in signal_names:
        x_vals = []
        y_vals = []
        for sd, rd in pairs:
            sv = signals[sd].get(sname, float("nan"))
            rv = returns[rd]
            if not (np.isnan(sv) or np.isnan(rv)):
                x_vals.append(sv)
                y_vals.append(rv)

        n = len(x_vals)
        if n < 5:
            results[sname] = {"n": n, "ic": float("nan"), "t": float("nan"), "status": "SKIP (n<5)"}
            continue

        x_arr = np.array(x_vals)
        y_arr = np.array(y_vals)

        # Spearman rank IC
        ic, p_val = stats.spearmanr(x_arr, y_arr)

        # t-stat
        t_stat = ic * np.sqrt((n - 2) / (1 - ic**2)) if abs(ic) < 1.0 else float("inf")

        # Rolling IC sign stability (20-day windows)
        sign_stable = float("nan")
        if n >= 25:
            window = 20
            positive_count = 0
            total_windows = 0
            for start in range(n - window + 1):
                wx = x_arr[start:start + window]
                wy = y_arr[start:start + window]
                wic, _ = stats.spearmanr(wx, wy)
                if not np.isnan(wic):
                    if wic * ic > 0:  # same sign as overall IC
                        positive_count += 1
                    total_windows += 1
            sign_stable = positive_count / total_windows if total_windows > 0 else float("nan")

        # Quintile returns (if enough data)
        quintile_rets = []
        if n >= 10:
            sorted_idx = np.argsort(x_arr)
            q_size = n // 5
            if q_size >= 1:
                for q in range(5):
                    start = q * q_size
                    end = start + q_size if q < 4 else n
                    q_ret = np.mean(y_arr[sorted_idx[start:end]])
                    quintile_rets.append(q_ret)

        # Gate decision
        if abs(ic) >= 0.05 and abs(t_stat) >= 1.5:
            status = "PASS"
        elif abs(ic) >= 0.05:
            status = "MARGINAL (IC ok, t low)"
        elif abs(t_stat) >= 1.5:
            status = "MARGINAL (t ok, IC low)"
        else:
            status = "FAIL"

        results[sname] = {
            "n": n,
            "ic": ic,
            "p_val": p_val,
            "t_stat": t_stat,
            "sign_stability": sign_stable,
            "quintile_rets": quintile_rets,
            "status": status,
        }

    return results, pairs


def format_results(results, pairs, prices, summary_data):
    """Format results as text."""
    lines = []
    lines.append("=" * 70)
    lines.append("R35 OI FLOW GATE CHECK RESULTS")
    lines.append("=" * 70)
    lines.append("")

    # Data summary
    if pairs:
        lines.append(f"OI data range: {summary_data[0]['date']} to {summary_data[-1]['date']} ({len(summary_data)} days)")
        lines.append(f"Price data days: {len(prices)}")
        lines.append(f"Signal-return pairs: {len(pairs)}")
        lines.append(f"Date range of pairs: {pairs[0][0]} -> {pairs[-1][1]}")
    lines.append("")

    # Data quality warning
    lines.append("DATA QUALITY NOTES:")
    lines.append("- dealer_net_oi per strike is ESTIMATED (total_oi * aggregate_dealer_fraction)")
    lines.append("- Per-strike dealer breakdown is NOT available from this scraper")
    lines.append("- Signals use aggregate dealer_net and dealer_frac from scrape_summary.csv")
    lines.append("- OI center-of-mass uses total OI per strike (not dealer-specific)")
    lines.append("")

    # Gate criteria
    lines.append("GATE CRITERIA (daily frequency, relaxed for small N):")
    lines.append("  |IC| > 0.05 AND |t| > 1.5 -> PASS")
    lines.append("  Sign stability > 60% of rolling 20-day windows -> bonus")
    lines.append("")

    if results is None:
        lines.append("RESULT: INSUFFICIENT DATA OVERLAP")
        return "\n".join(lines)

    # Results table
    lines.append(f"{'Signal':<20} {'N':>4} {'IC':>8} {'t-stat':>8} {'p-val':>8} {'SignStab':>8} {'Status':<20}")
    lines.append("-" * 80)

    any_pass = False
    for sname, r in sorted(results.items(), key=lambda x: -abs(x[1].get("ic", 0))):
        ic = r.get("ic", float("nan"))
        t = r.get("t_stat", float("nan"))
        p = r.get("p_val", float("nan"))
        ss = r.get("sign_stability", float("nan"))
        status = r.get("status", "?")
        n = r.get("n", 0)

        ic_str = f"{ic:.4f}" if not np.isnan(ic) else "N/A"
        t_str = f"{t:.2f}" if not np.isnan(t) else "N/A"
        p_str = f"{p:.4f}" if not np.isnan(p) else "N/A"
        ss_str = f"{ss:.1%}" if not np.isnan(ss) else "N/A"

        lines.append(f"{sname:<20} {n:>4} {ic_str:>8} {t_str:>8} {p_str:>8} {ss_str:>8} {status:<20}")

        if "PASS" in status:
            any_pass = True

        # Quintile returns
        qr = r.get("quintile_rets", [])
        if qr:
            qr_str = " | ".join(f"Q{i+1}:{v*10000:.1f}bps" for i, v in enumerate(qr))
            lines.append(f"  Quintiles: {qr_str}")

    lines.append("")
    lines.append("=" * 70)

    if any_pass:
        lines.append("GATE VERDICT: ** PROCEED ** — at least one signal passes IC + t-stat gates")
    else:
        # Check for marginal
        marginals = [s for s, r in results.items() if "MARGINAL" in r.get("status", "")]
        if marginals:
            lines.append(f"GATE VERDICT: ** MARGINAL ** — {len(marginals)} signal(s) borderline: {', '.join(marginals)}")
            lines.append("Recommendation: Extend OI data to 120+ days and re-test")
        else:
            lines.append("GATE VERDICT: ** KILL ** — no signal reaches IC or t-stat threshold")
            lines.append("Recommendation: Pivot to TSMOM (Candidate 2)")

    lines.append("=" * 70)
    return "\n".join(lines)


def main():
    print("Loading OI summary data...")
    summary = load_scrape_summary()
    print(f"  {len(summary)} days of OI data: {summary[0]['date']} to {summary[-1]['date']}")

    print("Loading strike-level OI data...")
    strike_data = load_strike_level_oi()
    print(f"  {len(strike_data)} days of strike data")

    print("Loading TXFD6 prices from ClickHouse...")
    prices = load_txfd6_prices_clickhouse()
    print(f"  {len(prices)} days from ClickHouse")

    if len(prices) < 5:
        print("Trying golden parquet...")
        parquet_prices = load_txfd6_prices_parquet()
        print(f"  {len(parquet_prices)} days from parquet")
        prices.update(parquet_prices)

    print(f"  Total price days: {len(prices)}")
    if prices:
        print(f"  Price range: {min(prices.keys())} to {max(prices.keys())}")
        print(f"  Price values: {min(prices.values()):.1f} to {max(prices.values()):.1f}")

    print("\nComputing signals...")
    signals = compute_signals(summary, strike_data)

    print("Running gate check...")
    results, pairs_or_msg = run_gate_check(signals, prices)

    if results is None:
        output = format_results(None, None, prices, summary)
    else:
        output = format_results(results, pairs_or_msg, prices, summary)

    print("\n" + output)

    # Write results
    out_path = OUT_DIR / "track_oi_flow_gate_check.md"
    with open(out_path, "w") as f:
        f.write("```\n")
        f.write(output)
        f.write("\n```\n")
    print(f"\nResults written to {out_path}")

    return results


if __name__ == "__main__":
    main()
