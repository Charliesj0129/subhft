"""run_mm_backtest.py — End-to-end MM strategy backtest on real L2 data.

Pipeline:
1. Load L1 data → precompute alpha features (vectorized, ~seconds)
2. Save features to .npz
3. Load L2 hftbacktest data
4. Run ToxicityAwareMM via HftBacktestAdapter (elapse mode, 100ms)
5. Report PnL, Sharpe, drawdown, fill statistics

Usage::

    python research/tools/run_mm_backtest.py \
        --symbol TXFC6 \
        --l1-data research/data/raw/txfc6/TXFC6_2026-03-03_l1.npy \
        --l2-data research/data/raw/txfc6/TXFC6_2026-03-03_l2.hftbt.npz \
        --tick-size 1.0

    # Multi-day batch
    python research/tools/run_mm_backtest.py \
        --symbol TXFC6 \
        --l1-dir research/data/raw/txfc6 \
        --tick-size 1.0
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from structlog import get_logger

logger = get_logger("run_mm_backtest")


def _precompute_features(l1_path: str) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Load L1 data and precompute all MM features."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backtest.feature_precompute import precompute_all_mm_features

    data = np.load(l1_path)
    logger.info("l1_loaded", path=l1_path, rows=len(data))

    t0 = time.monotonic()
    ts, feats, names = precompute_all_mm_features(data)
    elapsed = time.monotonic() - t0
    logger.info("features_precomputed", n_features=len(names), elapsed_s=f"{elapsed:.2f}")
    return ts, feats, names


def run_single_day(
    *,
    symbol: str,
    l1_path: str,
    l2_path: str,
    tick_size: float = 1.0,
    max_position: int = 5,
    base_half_spread: float = 2.0,
    latency_us: int = 100,
    elapse_ns: int = 100_000_000,
) -> dict:
    """Run one day of MM backtesting.

    Returns dict with PnL, fills, and equity curve metrics.
    """
    from hft_platform.backtest.adapter import HftBacktestAdapter
    from hft_platform.strategies.toxicity_aware_mm import ToxicityAwareMM

    # 1. Precompute features
    ts, feats, names = _precompute_features(l1_path)

    # 2. Create strategy
    strategy = ToxicityAwareMM(
        feature_timestamps=ts,
        feature_array=feats,
        feature_names=names,
        symbol=symbol,
        tick_size=tick_size,
        max_position=max_position,
        base_half_spread_ticks=base_half_spread,
        requote_interval_ns=elapse_ns,
    )

    # 3. Create adapter
    # price_scale=1 for futures (L2 prices are in raw points, not scaled)
    adapter = HftBacktestAdapter(
        strategy=strategy,
        asset_symbol=symbol,
        data_path=l2_path,
        latency_us=latency_us,
        price_scale=1,
        tick_size=tick_size,
        lot_size=1.0,
        tick_mode="elapse",
        elapse_ns=elapse_ns,
        feature_array_source=(ts, feats),
        initial_balance=10_000_000.0,
    )

    # 4. Run
    logger.info("backtest_start", symbol=symbol, l2_path=l2_path)
    t0 = time.monotonic()
    adapter.run()
    elapsed = time.monotonic() - t0
    logger.info("backtest_done", elapsed_s=f"{elapsed:.1f}")

    # 5. Extract results
    equity = np.array(adapter.equity_values) if hasattr(adapter, "equity_values") else np.array([])

    result = {
        "symbol": symbol,
        "l1_path": l1_path,
        "l2_path": l2_path,
        "elapsed_s": elapsed,
        "n_features": len(names),
        "feature_names": names,
    }

    if len(equity) > 1:
        returns = np.diff(equity) / (np.abs(equity[:-1]) + 1e-8)
        result["final_equity"] = float(equity[-1])
        result["pnl"] = float(equity[-1] - equity[0])
        result["max_drawdown"] = float(np.min(equity - np.maximum.accumulate(equity)))
        result["sharpe"] = float(
            np.mean(returns) / (np.std(returns) + 1e-12) * np.sqrt(len(returns))
        ) if len(returns) > 10 else 0.0
        result["n_equity_samples"] = len(equity)
    else:
        result["final_equity"] = 0.0
        result["pnl"] = 0.0
        result["max_drawdown"] = 0.0
        result["sharpe"] = 0.0
        result["n_equity_samples"] = 0

    logger.info(
        "backtest_result",
        pnl=result["pnl"],
        sharpe=result["sharpe"],
        drawdown=result["max_drawdown"],
    )
    return result


def run_multi_day(
    *,
    symbol: str,
    l1_dir: str,
    tick_size: float = 1.0,
    max_position: int = 5,
    base_half_spread: float = 2.0,
    latency_us: int = 100,
    elapse_ns: int = 100_000_000,
    out_path: str | None = None,
) -> list[dict]:
    """Run MM backtest across all available days for a symbol."""
    base = Path(l1_dir)

    # Find matching L1/L2 pairs
    l1_files = sorted(base.glob(f"{symbol}_*_l1.npy"))
    l1_files = [f for f in l1_files if "all" not in f.name]

    results = []
    for l1_f in l1_files:
        date = l1_f.stem.split("_")[1]
        l2_f = base / f"{symbol}_{date}_l2.hftbt.npz"
        if not l2_f.exists():
            logger.warning("l2_missing", date=date, expected=str(l2_f))
            continue

        logger.info("running_day", date=date)
        try:
            result = run_single_day(
                symbol=symbol,
                l1_path=str(l1_f),
                l2_path=str(l2_f),
                tick_size=tick_size,
                max_position=max_position,
                base_half_spread=base_half_spread,
                latency_us=latency_us,
                elapse_ns=elapse_ns,
            )
            result["date"] = date
            results.append(result)
        except Exception as e:
            logger.error("day_failed", date=date, error=str(e))
            results.append({"date": date, "error": str(e)})

    # Summary
    successful = [r for r in results if "pnl" in r]
    if successful:
        total_pnl = sum(r["pnl"] for r in successful)
        sharpes = [r["sharpe"] for r in successful if r["sharpe"] != 0]
        avg_sharpe = np.mean(sharpes) if sharpes else 0
        logger.info(
            "multi_day_summary",
            days=len(successful),
            total_pnl=total_pnl,
            avg_sharpe=avg_sharpe,
        )

    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(
            json.dumps(results, indent=2, default=str), encoding="utf-8"
        )
        logger.info("results_saved", path=out_path)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ToxicityAwareMM backtest")
    parser.add_argument("--symbol", required=True, help="Symbol (e.g., TXFC6)")
    parser.add_argument("--l1-data", default=None, help="Single-day L1 .npy path")
    parser.add_argument("--l2-data", default=None, help="Single-day L2 .npz path")
    parser.add_argument("--l1-dir", default=None, help="Directory with per-day L1+L2 files")
    parser.add_argument("--tick-size", type=float, default=1.0, help="Tick size")
    parser.add_argument("--max-position", type=int, default=5, help="Max position")
    parser.add_argument("--base-spread", type=float, default=2.0, help="Base half-spread in ticks")
    parser.add_argument("--latency-us", type=int, default=100, help="Order latency μs")
    parser.add_argument("--elapse-ns", type=int, default=100_000_000, help="Elapse interval ns")
    parser.add_argument("--out", default=None, help="Output JSON path")
    args = parser.parse_args()

    if args.l1_dir:
        run_multi_day(
            symbol=args.symbol,
            l1_dir=args.l1_dir,
            tick_size=args.tick_size,
            max_position=args.max_position,
            base_half_spread=args.base_spread,
            latency_us=args.latency_us,
            elapse_ns=args.elapse_ns,
            out_path=args.out,
        )
    elif args.l1_data and args.l2_data:
        result = run_single_day(
            symbol=args.symbol,
            l1_path=args.l1_data,
            l2_path=args.l2_data,
            tick_size=args.tick_size,
            max_position=args.max_position,
            base_half_spread=args.base_spread,
            latency_us=args.latency_us,
            elapse_ns=args.elapse_ns,
        )
        if args.out:
            Path(args.out).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(json.dumps(result, indent=2, default=str))
    else:
        parser.error("Provide either --l1-dir or both --l1-data and --l2-data")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
