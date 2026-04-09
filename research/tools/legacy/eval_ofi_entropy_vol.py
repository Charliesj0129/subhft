"""eval_ofi_entropy_vol.py — Re-evaluate ofi_entropy as a volatility-timing signal.

ofi_entropy predicts return MAGNITUDE (not direction), so standard IC with
signed returns is expected to be ~0.  This script tests correlation with
|forward_return| and realized_vol_20 instead.

Metrics computed per symbol:
  1. corr(signal, |fwd_ret|)       — primary vol-timing IC
  2. corr(signal, realized_vol_20) — correlation with rolling realized vol
  3. conditional |ret| ratio       — mean |ret| when signal > P80 vs < P20
  4. corr(pi_concentration, |fwd_ret|) — secondary stationary dist feature
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Ensure project root on path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from research.alphas.ofi_entropy.impl import OfiEntropyAlpha
from research.backtest.parquet_signal_runner import load_golden_parquet

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CH_TO_PLATFORM_DIVISOR: int = 100  # CH x1,000,000 -> platform x10,000
_SYMBOLS: list[str] = ["2881", "1301", "2330", "1216"]
_WARMUP: int = 200
_REALIZED_VOL_WINDOW: int = 20


# ---------------------------------------------------------------------------
# Per-symbol evaluation
# ---------------------------------------------------------------------------
def evaluate_symbol(symbol: str) -> dict[str, float | str]:
    """Run ofi_entropy on golden ticks for *symbol* and compute vol-timing metrics."""
    df = load_golden_parquet(symbol)
    tick_df = df[df["type"] == "Tick"].copy()
    if len(tick_df) == 0:
        return {"symbol": symbol, "error": "no ticks"}

    tick_df.sort_values("exch_ts", inplace=True)
    tick_df.reset_index(drop=True, inplace=True)

    n = len(tick_df)
    prices_platform = (tick_df["price_scaled"].values.astype(np.int64) // _CH_TO_PLATFORM_DIVISOR)
    volumes = tick_df["volume"].values.astype(np.int64)

    alpha = OfiEntropyAlpha(warmup_ticks=_WARMUP)

    signals = np.empty(n, dtype=np.float64)
    pi_conc = np.empty(n, dtype=np.float64)

    for i in range(n):
        price_val = int(prices_platform[i])
        vol_val = int(volumes[i])
        if price_val <= 0:
            signals[i] = signals[i - 1] if i > 0 else 0.0
            pi_conc[i] = pi_conc[i - 1] if i > 0 else 0.0
            continue
        signals[i] = float(alpha.update(price=price_val, volume=vol_val))
        pi_conc[i] = alpha.get_pi_concentration()

    # Mid prices as float for return computation
    mid_prices = prices_platform.astype(np.float64)
    for i in range(1, n):
        if mid_prices[i] <= 0.0:
            mid_prices[i] = mid_prices[i - 1]

    # Forward returns and absolute forward returns
    fwd_ret = np.diff(mid_prices) / np.where(mid_prices[:-1] != 0, mid_prices[:-1], 1.0)
    abs_fwd_ret = np.abs(fwd_ret)

    # Realized vol (next 20 ticks rolling std of |ret|, shifted back)
    realized_vol_20 = pd.Series(abs_fwd_ret).rolling(_REALIZED_VOL_WINDOW).std().shift(-_REALIZED_VOL_WINDOW).values

    # Valid range: after warmup, before end (fwd_ret is n-1 long)
    valid = slice(_WARMUP, len(fwd_ret))
    sig_v = signals[valid]
    abs_ret_v = abs_fwd_ret[valid]
    rvol_v = realized_vol_20[valid]
    pi_v = pi_conc[valid]

    # Remove NaN from realized_vol for that correlation
    rvol_mask = ~np.isnan(rvol_v)

    # 1. corr(signal, |fwd_ret|)
    if len(sig_v) > 2 and np.std(sig_v) > 0 and np.std(abs_ret_v) > 0:
        ic_abs = float(np.corrcoef(sig_v, abs_ret_v)[0, 1])
    else:
        ic_abs = 0.0

    # 2. corr(signal, realized_vol_20)
    if rvol_mask.sum() > 2 and np.std(sig_v[rvol_mask]) > 0 and np.std(rvol_v[rvol_mask]) > 0:
        ic_rvol = float(np.corrcoef(sig_v[rvol_mask], rvol_v[rvol_mask])[0, 1])
    else:
        ic_rvol = 0.0

    # 3. Conditional |return| ratio: P80 vs P20
    if len(sig_v) > 10:
        q80 = float(np.percentile(sig_v, 80))
        q20 = float(np.percentile(sig_v, 20))
        high_mask = sig_v > q80
        low_mask = sig_v < q20
        mean_high = float(np.mean(abs_ret_v[high_mask])) if high_mask.sum() > 0 else 0.0
        mean_low = float(np.mean(abs_ret_v[low_mask])) if low_mask.sum() > 0 else 0.0
        cond_ratio = mean_high / mean_low if mean_low > 0 else 0.0
    else:
        mean_high = mean_low = cond_ratio = 0.0

    # 4. corr(pi_concentration, |fwd_ret|)
    if len(pi_v) > 2 and np.std(pi_v) > 0 and np.std(abs_ret_v) > 0:
        ic_pi = float(np.corrcoef(pi_v, abs_ret_v)[0, 1])
    else:
        ic_pi = 0.0

    # Summary stats
    n_valid = len(sig_v)
    sig_mean = float(np.mean(sig_v))
    sig_std = float(np.std(sig_v))
    nonzero_pct = float(np.count_nonzero(sig_v)) / max(n_valid, 1) * 100.0

    return {
        "symbol": symbol,
        "n_ticks": n,
        "n_valid": n_valid,
        "sig_mean": sig_mean,
        "sig_std": sig_std,
        "nonzero_pct": nonzero_pct,
        "ic_abs_ret": ic_abs,
        "ic_rvol20": ic_rvol,
        "mean_ret_high": mean_high,
        "mean_ret_low": mean_low,
        "cond_ratio": cond_ratio,
        "ic_pi_conc": ic_pi,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    results: list[dict] = []
    for sym in _SYMBOLS:
        print(f"Processing {sym}...", flush=True)
        try:
            r = evaluate_symbol(sym)
            results.append(r)
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({"symbol": sym, "error": str(e)})

    # Print results table
    print()
    print("=" * 120)
    print("OFI ENTROPY — VOLATILITY-TIMING EVALUATION (target = |forward_return|)")
    print("=" * 120)

    header = (
        f"{'Symbol':<8} {'Ticks':>8} {'Valid':>8} {'Sig Mean':>9} {'Sig Std':>8} "
        f"{'NZ %':>6} {'IC|ret|':>8} {'IC rvol':>8} "
        f"{'|ret|>P80':>10} {'|ret|<P20':>10} {'Ratio':>7} {'IC pi':>8}"
    )
    print(header)
    print("-" * 120)

    for r in results:
        if "error" in r:
            print(f"{r['symbol']:<8} ERROR: {r['error']}")
            continue
        print(
            f"{r['symbol']:<8} {r['n_ticks']:>8} {r['n_valid']:>8} "
            f"{r['sig_mean']:>9.5f} {r['sig_std']:>8.5f} "
            f"{r['nonzero_pct']:>6.1f} {r['ic_abs_ret']:>8.4f} {r['ic_rvol20']:>8.4f} "
            f"{r['mean_ret_high']:>10.6f} {r['mean_ret_low']:>10.6f} {r['cond_ratio']:>7.3f} "
            f"{r['ic_pi_conc']:>8.4f}"
        )

    print("=" * 120)
    print()

    # Aggregate
    valid_results = [r for r in results if "error" not in r]
    if valid_results:
        avg_ic_abs = np.mean([r["ic_abs_ret"] for r in valid_results])
        avg_ic_rvol = np.mean([r["ic_rvol20"] for r in valid_results])
        avg_ratio = np.mean([r["cond_ratio"] for r in valid_results])
        avg_ic_pi = np.mean([r["ic_pi_conc"] for r in valid_results])

        print("AGGREGATE (mean across symbols):")
        print(f"  IC(signal, |fwd_ret|)        = {avg_ic_abs:+.4f}")
        print(f"  IC(signal, realized_vol_20)   = {avg_ic_rvol:+.4f}")
        print(f"  Conditional |ret| ratio P80/P20 = {avg_ratio:.3f}")
        print(f"  IC(pi_concentration, |fwd_ret|) = {avg_ic_pi:+.4f}")
        print()
        print("Interpretation:")
        print("  IC > 0   : signal predicts higher absolute returns (vol-timing works)")
        print("  Ratio > 1: high-signal regimes have larger |returns| than low-signal")
        print("  IC ~ 0   : no vol-timing power detected")
    print()


if __name__ == "__main__":
    main()
