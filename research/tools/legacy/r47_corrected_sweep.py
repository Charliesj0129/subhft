"""R47 Parameter Sweep on corrected spread data.

Phase 1: Sweep spread_threshold_pts [1..7] with research PE/Queue gates
Phase 2: For best spread, sweep PE × Queue × max_pos

Usage:
    uv run python research/tools/r47_corrected_sweep.py
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

os.environ.setdefault("HFT_STRICT_PRICE_MODE", "0")

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging
logging.disable(logging.WARNING)

from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
from hftbacktest.order import GTC, LIMIT

TICK_SIZE = 1.0
LOT_SIZE = 1.0
PRICE_SCALE = 10_000
ELAPSE_NS = 100_000_000

DATA_DIR = _REPO_ROOT / "research" / "data" / "raw" / "txfd6"
DATA_FILES = sorted(DATA_DIR.glob("TXFD6_2026-0*_l2.hftbt.npz"))
OUT_DIR = _REPO_ROOT / "outputs" / "team_artifacts" / "alpha-research" / "R47_maker_pivot"

_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9
_IDX_L1_IMBALANCE_PPM = 10


def run_config(data_path: Path, spread_pts: int, pe_danger: float,
               queue_cancel: float, max_pos: int) -> dict:
    """Run one day with given config. Returns summary dict."""
    from hft_platform.contracts.strategy import IntentType, Side, TIF, OrderIntent
    from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
    from hft_platform.strategy.base import StrategyContext
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    date_str = data_path.stem.replace("TXFD6_", "").replace("_l2.hftbt", "")

    strategy = R47MakerStrategy(
        strategy_id="r47_sweep",
        pe_danger_threshold=pe_danger,
        pe_window=100,
        queue_cancel_threshold=queue_cancel,
        queue_ema_alpha=0.05,
        mfg_skew_z_threshold=100.0,  # disabled
        mfg_ema_alpha=0.01,
        spread_threshold_pts=spread_pts,
        toxicity_max=9999,  # disabled for sweep
        max_pos=max_pos,
    )

    asset = (
        BacktestAsset()
        .data([str(data_path)])
        .linear_asset(1.0)
        .constant_order_latency(47_000, 47_000)
        .power_prob_queue_model(3.0)
        .tick_size(TICK_SIZE)
        .lot_size(LOT_SIZE)
        .no_partial_fill_exchange()
    )
    hbt = HashMapMarketDepthBacktest([asset])

    positions = {"TXFD6": 0}
    intent_seq = [0]
    captured: list[OrderIntent] = []

    def factory(strategy_id, symbol, side, price, qty, tif, intent_type, **kw):
        intent_seq[0] += 1
        intent = OrderIntent(intent_id=intent_seq[0], strategy_id=strategy_id,
                             symbol=symbol, intent_type=intent_type,
                             side=side, price=price, qty=qty, tif=tif)
        captured.append(intent)
        return intent

    def scaler(symbol, price):
        from decimal import Decimal
        return price if isinstance(price, int) else int(Decimal(str(price)) * Decimal(PRICE_SCALE))

    ctx = StrategyContext(positions=positions, strategy_id=strategy.strategy_id,
                          intent_factory=factory, price_scaler=scaler)

    order_id = 0
    active_buy: int | None = None
    active_sell: int | None = None
    equity_curve: list[float] = []
    step = 0
    first_ts = last_ts = 0

    while hbt.elapse(ELAPSE_NS) == 0:
        dp = hbt.depth(0)
        bb, ba = dp.best_bid, dp.best_ask
        if bb != bb or ba != ba or bb <= 0 or ba >= 2147483647 or bb >= ba:
            continue

        ts = int(hbt.current_timestamp)
        if not first_ts:
            first_ts = ts
        last_ts = ts

        if active_buy is not None:
            hbt.cancel(0, active_buy, False)
            active_buy = None
        if active_sell is not None:
            hbt.cancel(0, active_sell, False)
            active_sell = None
        hbt.clear_inactive_orders(0)

        pos = int(hbt.position(0))
        positions["TXFD6"] = pos

        bid_qty = int(getattr(dp, "best_bid_qty", 0) or 0)
        ask_qty = int(getattr(dp, "best_ask_qty", 0) or 0)
        bid_s = int(round(bb * PRICE_SCALE))
        ask_s = int(round(ba * PRICE_SCALE))
        total = bid_qty + ask_qty
        imb = (bid_qty - ask_qty) / total if total > 0 else 0.0

        vals = [0] * 27
        vals[_IDX_BEST_BID] = bid_s
        vals[_IDX_BEST_ASK] = ask_s
        vals[_IDX_L1_BID_QTY] = bid_qty
        vals[_IDX_L1_ASK_QTY] = ask_qty
        vals[_IDX_L1_IMBALANCE_PPM] = int(imb * 1_000_000)
        fids = tuple(f"f{i}" for i in range(27))

        feat = FeatureUpdateEvent(
            symbol="TXFD6", ts=ts, local_ts=ts, seq=step,
            feature_set_id="lob_shared_v3", schema_version=3,
            changed_mask=0xFFFFFFFF, warmup_ready_mask=0xFFFFFFFF,
            quality_flags=0, feature_ids=fids, values=tuple(vals),
        )
        stats = LOBStatsEvent(
            symbol="TXFD6", ts=ts, imbalance=imb,
            best_bid=bid_s, best_ask=ask_s,
            bid_depth=bid_qty, ask_depth=ask_qty,
        )

        captured.clear()
        strategy.handle_event(ctx, feat)
        strategy.handle_event(ctx, stats)

        buy_done = sell_done = False
        for intent in captured:
            if intent.intent_type != IntentType.NEW:
                continue
            px = round(intent.price / PRICE_SCALE / TICK_SIZE) * TICK_SIZE
            if intent.side == Side.BUY and not buy_done:
                order_id += 1
                hbt.submit_buy_order(0, order_id, px, float(intent.qty), GTC, LIMIT, False)
                active_buy = order_id
                buy_done = True
            elif intent.side == Side.SELL and not sell_done:
                order_id += 1
                hbt.submit_sell_order(0, order_id, px, float(intent.qty), GTC, LIMIT, False)
                active_sell = order_id
                sell_done = True

        if step % 10 == 0:
            sv = hbt.state_values(0)
            equity_curve.append(sv.balance + pos * (bb + ba) / 2.0)
        step += 1

    sv = hbt.state_values(0)
    final_pos = int(hbt.position(0))
    final_mid = (bb + ba) / 2.0 if bb > 0 and ba < 2147483647 else 0.0
    pnl = sv.balance + final_pos * final_mid
    vol = int(sv.trading_volume)
    hbt.close()

    eq = np.array(equity_curve, dtype=np.float64) if equity_curve else np.array([0.0])
    if len(eq) >= 2:
        rets = np.diff(eq)
        cum = eq - eq[0]
        dd = float(np.max(np.maximum.accumulate(cum) - cum))
        std = float(np.std(rets))
        sharpe = float(np.mean(rets)) / std * math.sqrt(len(eq) * 252) if std > 1e-12 else 0.0
    else:
        dd = sharpe = 0.0

    hrs = (last_ts - first_ts) / 3.6e12 if last_ts > first_ts else 0.0

    return {
        "date": date_str,
        "pnl_pts": round(pnl, 2),
        "fills": vol,
        "max_dd": round(dd, 2),
        "sharpe": round(sharpe, 2),
        "pos": final_pos,
        "hours": round(hrs, 2),
        "spr_blk": getattr(strategy, "_spread_blocked", 0),
        "pe_blk": getattr(strategy, "_pe_blocked", 0),
        "q_sup": getattr(strategy, "_queue_suppressed", 0),
        "quotes": getattr(strategy, "_quotes_sent", 0),
    }


def sweep_config(spread_pts, pe_danger, queue_cancel, max_pos):
    """Run all days for one config. Returns aggregate."""
    results = []
    for dp in DATA_FILES:
        try:
            r = run_config(dp, spread_pts, pe_danger, queue_cancel, max_pos)
            results.append(r)
        except Exception as e:
            pass  # skip failed days

    if not results:
        return None

    total_pnl = sum(r["pnl_pts"] for r in results)
    total_fills = sum(r["fills"] for r in results)
    n = len(results)
    winning = sum(1 for r in results if r["pnl_pts"] > 0)

    return {
        "spread_pts": spread_pts,
        "pe_danger": pe_danger,
        "queue_cancel": queue_cancel,
        "max_pos": max_pos,
        "n_days": n,
        "total_pnl_pts": round(total_pnl, 2),
        "avg_pnl_day": round(total_pnl / n, 2),
        "total_fills": total_fills,
        "fills_per_day": round(total_fills / n, 1),
        "pnl_per_fill": round(total_pnl / total_fills, 3) if total_fills > 0 else 0,
        "winning_days": winning,
        "win_rate": round(winning / n, 3),
        "worst_dd": round(max(r["max_dd"] for r in results), 2),
        "per_day": results,
    }


def main():
    print(f"R47 Corrected Sweep — {len(DATA_FILES)} days, correct spread data")
    print("=" * 80)

    all_results = []

    # Phase 1: Spread threshold sweep (PE=0.55, Queue=0.7, max_pos=3)
    print("\n=== Phase 1: Spread Threshold Sweep ===")
    print(f"{'spr':>4} {'PnL':>10} {'fills':>6} {'pnl/fill':>9} {'win':>5} {'worst_dd':>9}")
    print("-" * 50)
    for spr in [1, 2, 3, 4, 5, 6, 7]:
        t0 = time.monotonic()
        r = sweep_config(spr, 0.55, 0.7, 3)
        dt = time.monotonic() - t0
        if r:
            all_results.append(r)
            print(f"{spr:>4} {r['total_pnl_pts']:>+10.0f} {r['total_fills']:>6} "
                  f"{r['pnl_per_fill']:>+9.3f} {r['win_rate']:>5.1%} "
                  f"{r['worst_dd']:>9.0f}  ({dt:.0f}s)")

    # Find best spread threshold
    best_spr = max(all_results, key=lambda x: x["total_pnl_pts"])["spread_pts"]
    print(f"\nBest spread threshold: {best_spr} pts")

    # Phase 2: PE × Queue × max_pos sweep at best spread
    print(f"\n=== Phase 2: PE × Queue × MaxPos at spread={best_spr} ===")
    pe_vals = [0.0, 0.45, 0.55, 0.65]     # 0.0 = disabled (H never < 0)
    q_vals = [1.0, 0.5, 0.7, 0.9]          # 1.0 = disabled
    mp_vals = [1, 2, 3, 5]

    print(f"{'PE':>5} {'Queue':>6} {'MP':>3} {'PnL':>10} {'fills':>6} {'pnl/fill':>9} {'win':>5} {'dd':>8}")
    print("-" * 60)
    for pe in pe_vals:
        for q in q_vals:
            for mp in mp_vals:
                if pe == 0.55 and q == 0.7 and mp == 3:
                    # Already ran in Phase 1
                    prev = next((x for x in all_results if x["spread_pts"] == best_spr), None)
                    if prev:
                        print(f"{pe:>5.2f} {q:>6.1f} {mp:>3} {prev['total_pnl_pts']:>+10.0f} "
                              f"{prev['total_fills']:>6} {prev['pnl_per_fill']:>+9.3f} "
                              f"{prev['win_rate']:>5.1%} {prev['worst_dd']:>8.0f}  (cached)")
                        continue
                t0 = time.monotonic()
                r = sweep_config(best_spr, pe, q, mp)
                dt = time.monotonic() - t0
                if r:
                    all_results.append(r)
                    print(f"{pe:>5.2f} {q:>6.1f} {mp:>3} {r['total_pnl_pts']:>+10.0f} "
                          f"{r['total_fills']:>6} {r['pnl_per_fill']:>+9.3f} "
                          f"{r['win_rate']:>5.1%} {r['worst_dd']:>8.0f}  ({dt:.0f}s)")

    # Summary: top 10 configs
    profitable = [r for r in all_results if r["total_pnl_pts"] > 0]
    profitable.sort(key=lambda x: x["total_pnl_pts"], reverse=True)

    print(f"\n=== TOP 10 CONFIGS ===")
    print(f"{'#':>2} {'spr':>4} {'PE':>5} {'Q':>5} {'MP':>3} {'PnL':>10} {'fills':>6} {'pnl/f':>8} {'win':>5} {'dd':>8}")
    print("-" * 65)
    for i, r in enumerate(profitable[:10]):
        print(f"{i+1:>2} {r['spread_pts']:>4} {r['pe_danger']:>5.2f} {r['queue_cancel']:>5.1f} "
              f"{r['max_pos']:>3} {r['total_pnl_pts']:>+10.0f} {r['total_fills']:>6} "
              f"{r['pnl_per_fill']:>+8.3f} {r['win_rate']:>5.1%} {r['worst_dd']:>8.0f}")

    if not profitable:
        print("  (no profitable configs found)")
        # Show least-bad
        all_results.sort(key=lambda x: x["total_pnl_pts"], reverse=True)
        print("\n  Least-bad configs:")
        for i, r in enumerate(all_results[:5]):
            print(f"  {i+1}. spr={r['spread_pts']} PE={r['pe_danger']:.2f} Q={r['queue_cancel']:.1f} "
                  f"MP={r['max_pos']} PnL={r['total_pnl_pts']:+.0f} fills={r['total_fills']}")

    # Save all results
    out_path = OUT_DIR / "corrected_sweep_results.json"
    with open(out_path, "w") as f:
        json.dump({"n_configs": len(all_results), "results": all_results}, f, indent=2)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
