"""Max Position Sensitivity Analysis — C3 Alternative Investigation.

T5 showed that R47's large drawdowns are driven by inventory exposure
(max_pos=3 on TXFD6). This script tests max_pos={1, 2, 3} to quantify
the PnL vs drawdown tradeoff, informing whether reducing max_pos is
a better risk control than a drawdown breaker.

Usage:
    uv run python research/alphas/r47_maker_pivot/explore_maxpos_sensitivity.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

os.environ.setdefault("HFT_STRICT_PRICE_MODE", "0")

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging
logging.disable(logging.WARNING)

from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
from hftbacktest.order import GTC, LIMIT

TICK_SIZE = 1.0
LOT_SIZE = 1.0
POINT_VALUE_NTD = 10
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


@dataclass
class DayResult:
    date: str
    max_pos: int
    total_pnl_pts: float
    total_fills: int
    max_drawdown: float
    sharpe: float


def run_one_day(data_path: Path, max_pos: int) -> DayResult:
    from hft_platform.contracts.strategy import IntentType, Side, OrderIntent
    from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
    from hft_platform.strategy.base import StrategyContext
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    date_str = data_path.stem.replace("TXFD6_", "").replace("_l2.hftbt", "")

    strategy = R47MakerStrategy(
        strategy_id="r47_maker",
        pe_safe_threshold=0.85,
        pe_danger_threshold=0.55,
        pe_window=100,
        queue_cancel_threshold=0.7,
        mfg_skew_z_threshold=2.0,
        spread_threshold_pts=1,
        toxicity_max=700,
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
        .partial_fill_exchange()
    )
    hbt = HashMapMarketDepthBacktest([asset])

    positions = {"TXFD6": 0}
    intent_seq = [0]
    captured_intents: list[OrderIntent] = []

    def intent_factory(strategy_id, symbol, side, price, qty, tif, intent_type, **kw):
        intent_seq[0] += 1
        intent = OrderIntent(
            intent_id=intent_seq[0], strategy_id=strategy_id, symbol=symbol,
            intent_type=intent_type, side=side, price=price, qty=qty, tif=tif,
        )
        captured_intents.append(intent)
        return intent

    def scale_price(symbol, price):
        from decimal import Decimal
        if isinstance(price, int):
            return price
        return int(Decimal(str(price)) * Decimal(PRICE_SCALE))

    ctx = StrategyContext(
        positions=positions, strategy_id=strategy.strategy_id,
        intent_factory=intent_factory, price_scaler=scale_price,
    )

    order_id = 0
    active_buy_id = None
    active_sell_id = None
    equity_curve: list[float] = []
    fill_count = 0
    prev_pos = 0
    step_count = 0
    best_bid = 0.0
    best_ask = 0.0

    while hbt.elapse(ELAPSE_NS) == 0:
        dp = hbt.depth(0)
        best_bid = dp.best_bid
        best_ask = dp.best_ask
        if best_bid != best_bid or best_ask != best_ask:
            continue
        if best_bid <= 0 or best_ask >= 2147483647 or best_bid >= best_ask:
            continue

        ts_ns = int(hbt.current_timestamp)

        if active_buy_id is not None:
            hbt.cancel(0, active_buy_id, False)
            active_buy_id = None
        if active_sell_id is not None:
            hbt.cancel(0, active_sell_id, False)
            active_sell_id = None
        hbt.clear_inactive_orders(0)

        cur_pos = int(hbt.position(0))
        if cur_pos != prev_pos:
            fill_count += abs(cur_pos - prev_pos)
        prev_pos = cur_pos

        sv = hbt.state_values(0)
        mid = (best_bid + best_ask) / 2.0
        equity_curve.append(sv.balance + cur_pos * mid)

        positions["TXFD6"] = cur_pos

        bid_qty = int(getattr(dp, "best_bid_qty", 0) or 0)
        ask_qty = int(getattr(dp, "best_ask_qty", 0) or 0)
        bid_scaled = int(round(best_bid * PRICE_SCALE))
        ask_scaled = int(round(best_ask * PRICE_SCALE))
        total_qty = bid_qty + ask_qty
        imbalance = (bid_qty - ask_qty) / total_qty if total_qty > 0 else 0.0

        lob_event = LOBStatsEvent(
            symbol="TXFD6", ts=ts_ns, imbalance=imbalance,
            best_bid=bid_scaled, best_ask=ask_scaled,
            bid_depth=bid_qty, ask_depth=ask_qty,
        )

        vals = [0] * 27
        vals[_IDX_BEST_BID] = bid_scaled
        vals[_IDX_BEST_ASK] = ask_scaled
        vals[_IDX_L1_BID_QTY] = bid_qty
        vals[_IDX_L1_ASK_QTY] = ask_qty
        imb_ppm = int((bid_qty - ask_qty) * 1_000_000 / total_qty) if total_qty > 0 else 0
        vals[_IDX_L1_IMBALANCE_PPM] = imb_ppm
        feature_ids = tuple(f"f{i}" for i in range(27))

        feature_event = FeatureUpdateEvent(
            symbol="TXFD6", ts=ts_ns, local_ts=ts_ns, seq=step_count,
            feature_set_id="lob_shared_v3", schema_version=3,
            changed_mask=0xFFFFFFFF, warmup_ready_mask=0xFFFFFFFF,
            quality_flags=0, feature_ids=feature_ids, values=tuple(vals),
        )

        captured_intents.clear()
        strategy.handle_event(ctx, feature_event)
        strategy.handle_event(ctx, lob_event)

        buy_submitted = False
        sell_submitted = False
        for intent in captured_intents:
            if intent.intent_type != IntentType.NEW:
                continue
            price_native = intent.price / PRICE_SCALE
            price_rounded = round(price_native / TICK_SIZE) * TICK_SIZE
            if intent.side == Side.BUY and not buy_submitted:
                order_id += 1
                hbt.submit_buy_order(0, order_id, price_rounded, float(intent.qty), GTC, LIMIT, False)
                active_buy_id = order_id
                buy_submitted = True
            elif intent.side == Side.SELL and not sell_submitted:
                order_id += 1
                hbt.submit_sell_order(0, order_id, price_rounded, float(intent.qty), GTC, LIMIT, False)
                active_sell_id = order_id
                sell_submitted = True

        step_count += 1

    hbt.close()

    eq_arr = np.array(equity_curve, dtype=np.float64) if equity_curve else np.array([0.0])
    pnl_arr = eq_arr - eq_arr[0]
    total_pnl = float(pnl_arr[-1])

    running_max = np.maximum.accumulate(pnl_arr)
    drawdowns = running_max - pnl_arr
    max_dd = float(np.max(drawdowns))

    if len(pnl_arr) >= 2:
        returns = np.diff(pnl_arr)
        std = float(np.std(returns))
        sharpe = float(np.mean(returns)) / std * math.sqrt(len(returns) * 252) if std > 1e-12 else 0.0
    else:
        sharpe = 0.0

    return DayResult(
        date=date_str, max_pos=max_pos,
        total_pnl_pts=round(total_pnl, 2), total_fills=fill_count,
        max_drawdown=round(max_dd, 2), sharpe=round(sharpe, 2),
    )


def main() -> None:
    if not DATA_FILES:
        print(f"ERROR: No data files found in {DATA_DIR}")
        sys.exit(1)

    print(f"\nMax Position Sensitivity — R47 Maker")
    print(f"Data: {len(DATA_FILES)} days, testing max_pos = 1, 2, 3")
    print("=" * 70)

    results_by_pos: dict[int, list[DayResult]] = {1: [], 2: [], 3: []}

    for max_pos in [1, 2, 3]:
        print(f"\n--- max_pos = {max_pos} ---")
        for data_path in DATA_FILES:
            t0 = time.monotonic()
            result = run_one_day(data_path, max_pos)
            elapsed = time.monotonic() - t0
            results_by_pos[max_pos].append(result)
            print(
                f"  {result.date}: PnL={result.total_pnl_pts:>+8.1f}, "
                f"fills={result.total_fills:>4}, maxDD={result.max_drawdown:>7.1f}, "
                f"Sharpe={result.sharpe:>6.2f}  ({elapsed:.1f}s)"
            )

    # Summary
    print("\n" + "=" * 70)
    print("AGGREGATE COMPARISON")
    print("=" * 70)

    summary = {}
    for max_pos, results in results_by_pos.items():
        total_pnl = sum(r.total_pnl_pts for r in results)
        total_fills = sum(r.total_fills for r in results)
        worst_dd = max(r.max_drawdown for r in results)
        avg_dd = sum(r.max_drawdown for r in results) / len(results)
        winning_days = sum(1 for r in results if r.total_pnl_pts > 0)
        worst_day = min(r.total_pnl_pts for r in results)
        best_day = max(r.total_pnl_pts for r in results)
        avg_sharpe = sum(r.sharpe for r in results) / len(results)

        summary[str(max_pos)] = {
            "max_pos": max_pos,
            "total_pnl_pts": round(total_pnl, 2),
            "avg_pnl_per_day": round(total_pnl / len(results), 2),
            "total_fills": total_fills,
            "winning_days": winning_days,
            "losing_days": len(results) - winning_days,
            "win_rate": round(winning_days / len(results), 3),
            "worst_day_pnl": round(worst_day, 2),
            "best_day_pnl": round(best_day, 2),
            "worst_max_dd": round(worst_dd, 2),
            "avg_max_dd": round(avg_dd, 2),
            "avg_sharpe": round(avg_sharpe, 2),
            "pnl_per_fill": round(total_pnl / total_fills, 3) if total_fills > 0 else 0,
        }

        print(f"\n  max_pos={max_pos}:")
        print(f"    Total PnL:    {total_pnl:>+8.1f} pts ({total_pnl * POINT_VALUE_NTD:>+10.0f} NTD)")
        print(f"    Avg PnL/day:  {total_pnl / len(results):>+8.1f} pts")
        print(f"    Win rate:     {winning_days}/{len(results)} ({winning_days/len(results):.0%})")
        print(f"    Worst day:    {worst_day:>+8.1f} pts")
        print(f"    Best day:     {best_day:>+8.1f} pts")
        print(f"    Worst DD:     {worst_dd:>8.1f} pts")
        print(f"    Avg DD:       {avg_dd:>8.1f} pts")
        print(f"    Total fills:  {total_fills}")
        print(f"    PnL/fill:     {total_pnl / total_fills:>+.3f} pts" if total_fills > 0 else "")
        print(f"    Avg Sharpe:   {avg_sharpe:.2f}")

    # Save
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "metadata": {
            "script": "explore_maxpos_sensitivity.py",
            "n_days": len(DATA_FILES),
            "max_pos_values": [1, 2, 3],
        },
        "summary": summary,
        "per_day": {
            str(mp): [
                {"date": r.date, "total_pnl_pts": r.total_pnl_pts,
                 "total_fills": r.total_fills, "max_drawdown": r.max_drawdown,
                 "sharpe": r.sharpe}
                for r in results
            ]
            for mp, results in results_by_pos.items()
        },
    }
    out_path = OUT_DIR / "maxpos_sensitivity.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
