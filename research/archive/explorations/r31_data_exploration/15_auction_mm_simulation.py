"""R31 Stage 1b: Corrected Passive MM Simulation with Auction Model.

Fixes all 5 fatal flaws identified by Challenger & Execution reviewer:
  1. Symmetric fill criterion for both entry AND exit
  2. 5-second call auction batching (TWSE continuous session)
  3. 50% queue position haircut
  4. Full-sample AND ex-outlier (2026-03-04) reporting
  5. Config drift: report imbalance in ratio + ppm, holding in ticks + seconds

Author: R31 Researcher
"""
import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict
import hashlib
import warnings

warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")
TARGETS = ["2301", "1303", "2886", "2891", "2882"]
OUTLIER_DATE = "2026-03-04"

# ── Parameters ──
IMB_THRESH_RATIO = 0.3          # L5 imbalance ratio threshold
IMB_THRESH_PPM = int(0.3 * 1e6) # same in ppm = 300000
AUCTION_WINDOW_S = 5.0          # TWSE call auction interval (seconds)
QUEUE_FILL_PROB = 0.5           # 50% queue position haircut
RT_COMMISSION_BPS = 5.85        # round-trip commission in bps
MIN_BA_PER_DAY = 50             # minimum BidAsk updates to process a day


def filter_continuous_session(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only continuous trading session (09:00-13:25 local = UTC+8)."""
    ts_s = df["exch_ts"].values / 1e9
    tod_s = (ts_s + 8 * 3600) % 86400
    continuous_start = 9 * 3600       # 09:00
    continuous_end = 13 * 3600 + 25 * 60  # 13:25
    mask = (tod_s >= continuous_start) & (tod_s <= continuous_end)
    return df[mask].copy()


def dedup_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Remove duplicate rows (each event appears twice in some data)."""
    return df.drop_duplicates(subset=["exch_ts", "price_scaled", "type"], keep="first")


def deterministic_coin(ts_ns: int) -> bool:
    """Deterministic 50% fill based on hash of timestamp (reproducible)."""
    h = hashlib.md5(int(ts_ns).to_bytes(8, "big")).hexdigest()
    return int(h[:8], 16) < 0x80000000  # 50% threshold


def build_auction_windows(ba: pd.DataFrame, ticks: pd.DataFrame):
    """Group BidAsk updates into 5-second auction windows.

    Returns list of dicts with:
      - window_start_ns, window_end_ns
      - best_bid, best_ask (last snapshot in window)
      - l5_imbalance (last snapshot in window)
      - clearing_price (last trade price in window, or mid if no trades)
      - n_updates (number of BidAsk updates in window)
      - wall_clock_s (seconds since first window)
    """
    if len(ba) == 0:
        return []

    ts_ba = ba["exch_ts"].values
    first_ts = ts_ba[0]

    # Compute window indices for BidAsk
    window_idx_ba = ((ts_ba - first_ts) / 1e9 / AUCTION_WINDOW_S).astype(int)

    # Extract L5 book data
    bp = ba["bids_price"].values
    bv = ba["bids_vol"].values
    ap = ba["asks_price"].values
    av = ba["asks_vol"].values

    best_bid = np.array(
        [x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in bp],
        dtype=float,
    )
    best_ask = np.array(
        [x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in ap],
        dtype=float,
    )

    l5bv = np.array(
        [x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in bv],
        dtype=float,
    )
    l5av = np.array(
        [x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in av],
        dtype=float,
    )
    l5t = l5bv + l5av
    l5_imb = np.where(l5t > 0, (l5bv - l5av) / l5t, 0.0)

    # Prepare tick data for clearing prices
    ts_tk = ticks["exch_ts"].values if len(ticks) > 0 else np.array([], dtype=np.int64)
    tk_prices = ticks["price_scaled"].values.astype(float) if len(ticks) > 0 else np.array([], dtype=float)
    window_idx_tk = (
        ((ts_tk - first_ts) / 1e9 / AUCTION_WINDOW_S).astype(int) if len(ts_tk) > 0
        else np.array([], dtype=int)
    )

    max_win = max(window_idx_ba[-1] if len(window_idx_ba) > 0 else 0,
                  window_idx_tk[-1] if len(window_idx_tk) > 0 else 0)

    windows = []
    for w in range(max_win + 1):
        ba_mask = window_idx_ba == w
        if not ba_mask.any():
            continue  # skip windows with no book updates

        # Use last BidAsk snapshot in window
        last_idx = np.where(ba_mask)[0][-1]
        bb = best_bid[last_idx]
        ba_val = best_ask[last_idx]
        imb = l5_imb[last_idx]
        ts_last = ts_ba[last_idx]

        if np.isnan(bb) or np.isnan(ba_val) or bb <= 0 or ba_val <= 0:
            continue

        mid = (bb + ba_val) / 2.0

        # Clearing price: last trade in this window, or mid
        tk_mask = window_idx_tk == w
        if tk_mask.any():
            clearing = tk_prices[tk_mask][-1]
        else:
            clearing = mid

        windows.append({
            "window_idx": w,
            "ts_ns": ts_last,
            "best_bid": bb,
            "best_ask": ba_val,
            "mid": mid,
            "l5_imbalance": imb,
            "clearing_price": clearing,
            "n_updates": int(ba_mask.sum()),
            "wall_clock_s": (ts_last - first_ts) / 1e9,
        })

    return windows


def simulate_auction_mm(sym: str, imb_thresh: float = IMB_THRESH_RATIO):
    """Run auction-based passive MM simulation.

    Returns per-trade results with all 3 exit scenarios.
    """
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    all_trades = []
    daily_stats = defaultdict(lambda: {
        "n_signals": 0,
        "n_entry_fill": 0,
        "n_queue_pass": 0,
        "trades_conservative": [],
        "trades_moderate": [],
        "trades_worstcase": [],
    })

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        df = filter_continuous_session(df)
        df = dedup_rows(df)

        ba = df[df["type"] == "BidAsk"].sort_values("exch_ts").reset_index(drop=True)
        ticks = df[df["type"] == "Tick"].sort_values("exch_ts").reset_index(drop=True)

        if len(ba) < MIN_BA_PER_DAY:
            continue

        windows = build_auction_windows(ba, ticks)
        if len(windows) < 10:
            continue

        ds = daily_stats[date_str]
        i = 0
        while i < len(windows) - 2:  # need at least 2 future windows
            w = windows[i]

            if w["l5_imbalance"] <= imb_thresh:
                i += 1
                continue

            ds["n_signals"] += 1

            # ── ENTRY ──
            # Post limit buy at best_bid of window i.
            # Check if clearing_price of window i+1 is <= our limit (we fill).
            entry_limit = w["best_bid"]
            next_w = windows[i + 1]

            entry_filled = next_w["clearing_price"] <= entry_limit
            if not entry_filled:
                # Also check: did best_bid retreat through our level in next window?
                # (bid retreated = adverse selection fill)
                entry_filled = next_w["best_bid"] < entry_limit
            if not entry_filled:
                # Check ask crossed through our bid (market moved down to us)
                entry_filled = next_w["best_ask"] <= entry_limit

            if not entry_filled:
                i += 1
                continue

            ds["n_entry_fill"] += 1

            # ── QUEUE POSITION HAIRCUT (Flaw 3) ──
            if not deterministic_coin(next_w["ts_ns"]):
                i += 2  # skip ahead
                continue

            ds["n_queue_pass"] += 1

            # ── EXIT ──
            # Try to exit in the next available window(s) after fill.
            # Look ahead up to 10 windows for exit opportunity.
            entry_price = entry_limit
            entry_mid = w["mid"]
            entry_ts = next_w["ts_ns"]

            # Exit window: start from i+2 (window after fill)
            exit_found = False
            for k in range(i + 2, min(i + 12, len(windows))):
                exit_w = windows[k]

                # ── (a) Conservative exit: symmetric to entry ──
                # Post limit sell at best_ask of exit_w.
                # Fill if clearing_price of exit_w+1 >= our ask
                # OR if best_ask of exit_w+1 > our ask (ask advanced through us)
                # OR if best_bid >= our ask (someone lifted our offer)
                exit_ask = exit_w["best_ask"]
                conservative_filled = False
                if k + 1 < len(windows):
                    next_exit_w = windows[k + 1]
                    conservative_filled = (
                        next_exit_w["clearing_price"] >= exit_ask
                        or next_exit_w["best_ask"] > exit_ask
                        or next_exit_w["best_bid"] >= exit_ask
                    )
                    # Also apply 50% queue haircut to exit
                    if conservative_filled and not deterministic_coin(next_exit_w["ts_ns"] + 1):
                        conservative_filled = False

                # ── (b) Moderate exit: at mid-price ──
                exit_mid = exit_w["mid"]

                # ── (c) Worst case exit: at best bid (market sell) ──
                exit_bid = exit_w["best_bid"]

                # Wall-clock time between entry signal and this exit window
                hold_seconds = (exit_w["ts_ns"] - entry_ts) / 1e9
                hold_windows = k - (i + 1)

                if conservative_filled or k == min(i + 11, len(windows) - 1):
                    # Compute P&L for all 3 scenarios
                    # Gross = (exit - entry) / mid_at_entry * 10000
                    gross_conservative = (exit_ask - entry_price) / entry_mid * 10000 if conservative_filled else np.nan
                    gross_moderate = (exit_mid - entry_price) / entry_mid * 10000
                    gross_worst = (exit_bid - entry_price) / entry_mid * 10000

                    net_conservative = gross_conservative - RT_COMMISSION_BPS if not np.isnan(gross_conservative) else np.nan
                    net_moderate = gross_moderate - RT_COMMISSION_BPS
                    net_worst = gross_worst - RT_COMMISSION_BPS

                    trade = {
                        "date": date_str,
                        "symbol": sym,
                        "entry_price": entry_price,
                        "entry_mid": entry_mid,
                        "entry_imbalance": w["l5_imbalance"],
                        "entry_imbalance_ppm": int(w["l5_imbalance"] * 1e6),
                        "hold_windows": hold_windows,
                        "hold_seconds": hold_seconds,
                        "exit_conservative_filled": conservative_filled,
                        "gross_conservative_bps": gross_conservative,
                        "net_conservative_bps": net_conservative,
                        "gross_moderate_bps": gross_moderate,
                        "net_moderate_bps": net_moderate,
                        "gross_worst_bps": gross_worst,
                        "net_worst_bps": net_worst,
                    }
                    all_trades.append(trade)

                    if not np.isnan(net_conservative):
                        ds["trades_conservative"].append(net_conservative)
                    ds["trades_moderate"].append(net_moderate)
                    ds["trades_worstcase"].append(net_worst)

                    exit_found = True
                    break

            if not exit_found:
                # Force exit at last checked window using worst-case
                pass

            i = k + 1 if exit_found else i + 2

    return all_trades, dict(daily_stats)


def report_results(all_trades: list, daily_stats: dict, sym: str):
    """Print comprehensive results for one symbol."""
    if not all_trades:
        print(f"\n{'='*70}")
        print(f"  {sym}: NO TRADES")
        return {}

    df = pd.DataFrame(all_trades)

    # Full sample vs ex-outlier
    full_mask = pd.Series([True] * len(df))
    exout_mask = df["date"] != OUTLIER_DATE

    results = {}
    for label, mask in [("Full Sample", full_mask), (f"Ex-{OUTLIER_DATE}", exout_mask)]:
        sub = df[mask]
        if len(sub) == 0:
            continue

        n = len(sub)
        n_days = sub["date"].nunique()

        # Conservative: only trades where exit actually filled
        cons = sub.dropna(subset=["net_conservative_bps"])
        n_cons = len(cons)

        stats = {}
        for scenario, col in [
            ("conservative", "net_conservative_bps"),
            ("moderate", "net_moderate_bps"),
            ("worst_case", "net_worst_bps"),
        ]:
            vals = sub[col].dropna().values
            if len(vals) == 0:
                stats[scenario] = {"mean": np.nan, "std": np.nan, "win": np.nan, "n": 0}
                continue
            stats[scenario] = {
                "mean": float(np.mean(vals)),
                "std": float(np.std(vals)),
                "win": float((vals > 0).mean()),
                "n": len(vals),
                "median": float(np.median(vals)),
                "fills_per_day": len(vals) / n_days if n_days > 0 else 0,
            }

        # Holding period stats
        hold_s = sub["hold_seconds"].values
        hold_w = sub["hold_windows"].values

        results[label] = {
            "n_trades_total": n,
            "n_days": n_days,
            "n_conservative_fills": n_cons,
            "conservative_fill_rate": n_cons / n if n > 0 else 0,
            "hold_seconds_mean": float(np.mean(hold_s)),
            "hold_seconds_median": float(np.median(hold_s)),
            "hold_windows_mean": float(np.mean(hold_w)),
            "scenarios": stats,
        }

    # Print
    print(f"\n{'='*70}")
    print(f"  SYMBOL: {sym}")
    print(f"  Imbalance threshold: {IMB_THRESH_RATIO} (ratio) / {IMB_THRESH_PPM} (ppm)")
    print(f"  Auction window: {AUCTION_WINDOW_S}s")
    print(f"  Queue haircut: {QUEUE_FILL_PROB*100:.0f}%")
    print(f"  RT commission: {RT_COMMISSION_BPS} bps")
    print(f"{'='*70}")

    for label, r in results.items():
        print(f"\n  ── {label} ──")
        print(f"  Trades: {r['n_trades_total']}, Days: {r['n_days']}, "
              f"Conservative exit fills: {r['n_conservative_fills']} ({r['conservative_fill_rate']:.0%})")
        print(f"  Hold: {r['hold_seconds_mean']:.1f}s mean / {r['hold_seconds_median']:.1f}s median "
              f"({r['hold_windows_mean']:.1f} windows mean)")

        print(f"\n  {'Scenario':<16s} | {'Mean bps':>9s} | {'Median':>8s} | {'Std':>8s} | {'Win%':>6s} | {'N':>5s} | {'Fills/day':>9s}")
        print(f"  {'-'*68}")
        for sc_name, sc in r["scenarios"].items():
            if sc["n"] == 0:
                print(f"  {sc_name:<16s} |       N/A |      N/A |      N/A |    N/A |     0 |       N/A")
            else:
                print(f"  {sc_name:<16s} | {sc['mean']:9.2f} | {sc['median']:8.2f} | {sc['std']:8.2f} | "
                      f"{sc['win']:5.1%} | {sc['n']:5d} | {sc['fills_per_day']:9.1f}")

    # Per-day breakdown
    print(f"\n  ── Daily Breakdown ({sym}) ──")
    print(f"  {'Date':<12s} | {'Cons_N':>6s} | {'Cons_Mean':>9s} | {'Mod_N':>5s} | {'Mod_Mean':>9s} | {'Worst_N':>7s} | {'Worst_Mean':>10s}")
    print(f"  {'-'*72}")
    for date_str in sorted(daily_stats.keys()):
        ds = daily_stats[date_str]
        cons_trades = ds["trades_conservative"]
        mod_trades = ds["trades_moderate"]
        worst_trades = ds["trades_worstcase"]

        cons_str = f"{np.mean(cons_trades):9.2f}" if cons_trades else "      N/A"
        mod_str = f"{np.mean(mod_trades):9.2f}" if mod_trades else "      N/A"
        worst_str = f"{np.mean(worst_trades):10.2f}" if worst_trades else "       N/A"

        print(f"  {date_str:<12s} | {len(cons_trades):6d} | {cons_str} | {len(mod_trades):5d} | {mod_str} | {len(worst_trades):7d} | {worst_str}")

    return results


# ════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 70)
    print("R31 Stage 1b: Corrected Auction MM Simulation")
    print("=" * 70)
    print(f"\nParameters:")
    print(f"  Imbalance threshold: {IMB_THRESH_RATIO} (ratio) = {IMB_THRESH_PPM} ppm")
    print(f"  Auction window: {AUCTION_WINDOW_S} seconds")
    print(f"  Queue fill probability: {QUEUE_FILL_PROB}")
    print(f"  RT commission: {RT_COMMISSION_BPS} bps")
    print(f"  Targets: {TARGETS}")
    print(f"  Outlier date: {OUTLIER_DATE}")

    all_results = {}

    for sym in TARGETS:
        print(f"\n>>> Processing {sym}...")
        trades, daily = simulate_auction_mm(sym)
        results = report_results(trades, daily, sym)
        all_results[sym] = {
            "trades": trades,
            "daily": daily,
            "results": results,
        }

    # ── Cross-symbol summary ──
    print("\n\n" + "=" * 70)
    print("  CROSS-SYMBOL SUMMARY")
    print("=" * 70)

    for sample_label in ["Full Sample", f"Ex-{OUTLIER_DATE}"]:
        print(f"\n  ── {sample_label} ──")
        print(f"  {'Symbol':<8s} | {'Cons Mean':>9s} | {'Cons N':>6s} | {'Mod Mean':>9s} | {'Mod N':>5s} | {'Worst Mean':>10s} | {'Worst N':>7s} | {'Days':>4s}")
        print(f"  {'-'*74}")

        for sym in TARGETS:
            r = all_results[sym]["results"].get(sample_label, {})
            if not r:
                print(f"  {sym:<8s} | {'N/A':>9s} | {'0':>6s} | {'N/A':>9s} | {'0':>5s} | {'N/A':>10s} | {'0':>7s} | {'0':>4s}")
                continue
            sc = r["scenarios"]
            cons = sc.get("conservative", {})
            mod = sc.get("moderate", {})
            worst = sc.get("worst_case", {})

            cons_m = f"{cons['mean']:9.2f}" if cons.get("n", 0) > 0 else "      N/A"
            mod_m = f"{mod['mean']:9.2f}" if mod.get("n", 0) > 0 else "      N/A"
            worst_m = f"{worst['mean']:10.2f}" if worst.get("n", 0) > 0 else "       N/A"

            print(f"  {sym:<8s} | {cons_m} | {cons.get('n',0):6d} | {mod_m} | {mod.get('n',0):5d} | "
                  f"{worst_m} | {worst.get('n',0):7d} | {r['n_days']:4d}")

    # ── VERDICT ──
    print("\n\n" + "=" * 70)
    print("  VERDICT: Is conservative exit scenario positive after all corrections?")
    print("=" * 70)

    for sym in TARGETS:
        r_full = all_results[sym]["results"].get("Full Sample", {})
        r_ex = all_results[sym]["results"].get(f"Ex-{OUTLIER_DATE}", {})

        cons_full = r_full.get("scenarios", {}).get("conservative", {})
        cons_ex = r_ex.get("scenarios", {}).get("conservative", {})
        mod_ex = r_ex.get("scenarios", {}).get("moderate", {})
        worst_ex = r_ex.get("scenarios", {}).get("worst_case", {})

        full_mean = cons_full.get("mean", np.nan)
        ex_mean = cons_ex.get("mean", np.nan)
        mod_mean = mod_ex.get("mean", np.nan)
        worst_mean = worst_ex.get("mean", np.nan)

        verdict = "UNKNOWN"
        if np.isnan(ex_mean) or cons_ex.get("n", 0) < 5:
            verdict = "INSUFFICIENT DATA"
        elif ex_mean > 0:
            verdict = "PROCEED (conservative positive)"
        elif not np.isnan(mod_mean) and mod_mean > 0:
            verdict = "MARGINAL (only moderate positive, needs aggressive exit)"
        else:
            verdict = "KILL (negative even at moderate exit)"

        print(f"  {sym}: cons_full={full_mean:+.2f}bps, cons_ex_outlier={ex_mean:+.2f}bps, "
              f"mod_ex={mod_mean:+.2f}bps, worst_ex={worst_mean:+.2f}bps → {verdict}")
