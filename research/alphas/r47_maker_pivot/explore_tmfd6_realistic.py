"""R47 Maker — Realistic TMFD6 Backtest.

Fixes the critical backtest-vs-live inconsistency:
  OLD: cancel + resubmit EVERY 100ms → order always at back of queue → massive adverse selection
  NEW: only cancel + resubmit when quote price CHANGES → preserves queue priority

Also tests PowerProbQueueModel sensitivity (param 1.0, 2.0, 3.0).

Usage:
    uv run python research/alphas/r47_maker_pivot/explore_tmfd6_realistic.py
"""

from __future__ import annotations

import gc
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
ELAPSE_NS = 100_000_000  # 100ms

DATA_DIR = _REPO_ROOT / "research" / "data" / "raw" / "tmfd6"
DATA_FILES = sorted(DATA_DIR.glob("TMFD6_2026-0*_l2.hftbt.npz"))
OUT_DIR = _REPO_ROOT / "outputs" / "team_artifacts" / "alpha-research" / "R47_maker_pivot"

_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9
_IDX_L1_IMBALANCE_PPM = 10

SYMBOL = "TMFD6"


@dataclass
class RunConfig:
    name: str
    latency_ns: int
    queue_model_power: float
    spread_threshold_pts: int
    max_pos: int
    # Strategy params
    pe_danger_threshold: float = 0.0
    queue_cancel_threshold: float = 1.0  # 1.0 = disabled (NOT 0.0!)
    mfg_skew_z_threshold: float = 100.0
    toxicity_max: int = 9999
    # Harness behavior
    preserve_queue_priority: bool = True  # NEW: only cancel when price changes


@dataclass
class DayResult:
    config_name: str
    date: str
    total_pnl_pts: float
    total_fills: int
    max_drawdown: float
    final_position: int
    quotes_submitted: int
    cancels: int
    price_changes: int
    avg_spread_pts: float
    pct_time_spread_gte_threshold: float


CONFIGS = [
    # === Core comparison: queue priority ON vs OFF (qm3, spr4) ===
    RunConfig(name="spr4_qm3_pq", latency_ns=36_000_000, queue_model_power=3.0,
              spread_threshold_pts=4, max_pos=1, preserve_queue_priority=True),
    RunConfig(name="spr4_qm3_NO_pq", latency_ns=36_000_000, queue_model_power=3.0,
              spread_threshold_pts=4, max_pos=1, preserve_queue_priority=False),

    # === Queue model sensitivity (spr4, pq=True) ===
    RunConfig(name="spr4_qm1_pq", latency_ns=36_000_000, queue_model_power=1.0,
              spread_threshold_pts=4, max_pos=1, preserve_queue_priority=True),

    # === Best candidate: spr3 + qm3 + pq ===
    RunConfig(name="spr3_qm3_pq", latency_ns=36_000_000, queue_model_power=3.0,
              spread_threshold_pts=3, max_pos=1, preserve_queue_priority=True),
]


def run_one_day(data_path: Path, cfg: RunConfig) -> DayResult:
    from hft_platform.contracts.strategy import IntentType, Side, OrderIntent
    from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
    from hft_platform.strategy.base import StrategyContext
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    date_str = data_path.stem.replace("TMFD6_", "").replace("_l2.hftbt", "")

    strategy = R47MakerStrategy(
        strategy_id="r47_maker",
        pe_danger_threshold=cfg.pe_danger_threshold,
        pe_window=100,
        queue_cancel_threshold=cfg.queue_cancel_threshold,
        mfg_skew_z_threshold=cfg.mfg_skew_z_threshold,
        spread_threshold_pts=cfg.spread_threshold_pts,
        toxicity_max=cfg.toxicity_max,
        max_pos=cfg.max_pos,
    )

    asset = (
        BacktestAsset()
        .data([str(data_path)])
        .linear_asset(1.0)
        .constant_order_latency(cfg.latency_ns, cfg.latency_ns)
        .power_prob_queue_model(cfg.queue_model_power)
        .tick_size(TICK_SIZE)
        .lot_size(LOT_SIZE)
        .partial_fill_exchange()
    )
    hbt = HashMapMarketDepthBacktest([asset])

    positions = {SYMBOL: 0}
    intent_seq = [0]
    captured_intents: list[OrderIntent] = []

    def intent_factory(**kw: object) -> OrderIntent:
        intent_seq[0] += 1
        intent = OrderIntent(intent_id=intent_seq[0], **kw)
        captured_intents.append(intent)
        return intent

    def scale_price(symbol: str, price: object) -> int:
        from decimal import Decimal
        if isinstance(price, int):
            return price
        return int(Decimal(str(price)) * Decimal(PRICE_SCALE))

    ctx = StrategyContext(
        positions=positions, strategy_id=strategy.strategy_id,
        intent_factory=intent_factory, price_scaler=scale_price,
    )

    order_id = 0
    active_buy_id: int | None = None
    active_sell_id: int | None = None
    active_buy_price: float = 0.0  # Track active order prices for queue priority
    active_sell_price: float = 0.0
    equity_curve: list[float] = []
    fill_count = 0
    prev_pos = 0
    step_count = 0
    quotes_submitted = 0
    cancel_count = 0
    price_change_count = 0
    spread_sum = 0.0
    spread_count = 0
    spread_above_threshold = 0

    while hbt.elapse(ELAPSE_NS) == 0:
        dp = hbt.depth(0)
        best_bid = dp.best_bid
        best_ask = dp.best_ask
        if best_bid != best_bid or best_ask != best_ask:
            continue
        if best_bid <= 0 or best_ask >= 2147483647 or best_bid >= best_ask:
            continue

        ts_ns = int(hbt.current_timestamp)
        spread_pts = best_ask - best_bid
        spread_sum += spread_pts
        spread_count += 1
        if spread_pts >= cfg.spread_threshold_pts:
            spread_above_threshold += 1

        # Track fills via position change
        cur_pos = int(hbt.position(0))
        if cur_pos != prev_pos:
            fill_count += abs(cur_pos - prev_pos)
        prev_pos = cur_pos

        sv = hbt.state_values(0)
        mid = (best_bid + best_ask) / 2.0
        equity_curve.append(sv.balance + cur_pos * mid)
        positions[SYMBOL] = cur_pos

        # Build events for strategy
        bid_qty = int(getattr(dp, "best_bid_qty", 0) or 0)
        ask_qty = int(getattr(dp, "best_ask_qty", 0) or 0)
        bid_scaled = int(round(best_bid * PRICE_SCALE))
        ask_scaled = int(round(best_ask * PRICE_SCALE))
        total_qty = bid_qty + ask_qty
        imbalance = (bid_qty - ask_qty) / total_qty if total_qty > 0 else 0.0

        lob_event = LOBStatsEvent(
            symbol=SYMBOL, ts=ts_ns, imbalance=imbalance,
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
            symbol=SYMBOL, ts=ts_ns, local_ts=ts_ns, seq=step_count,
            feature_set_id="lob_shared_v3", schema_version=3,
            changed_mask=0xFFFFFFFF, warmup_ready_mask=0xFFFFFFFF,
            quality_flags=0, feature_ids=feature_ids, values=tuple(vals),
        )

        # Get strategy's desired quotes
        captured_intents.clear()
        strategy.handle_event(ctx, feature_event)
        strategy.handle_event(ctx, lob_event)

        # Extract desired buy/sell prices from intents
        desired_buy_price: float | None = None
        desired_sell_price: float | None = None
        for intent in captured_intents:
            if intent.intent_type != IntentType.NEW:
                continue
            price_native = intent.price / PRICE_SCALE
            price_rounded = round(price_native / TICK_SIZE) * TICK_SIZE
            if intent.side == Side.BUY and desired_buy_price is None:
                desired_buy_price = price_rounded
            elif intent.side == Side.SELL and desired_sell_price is None:
                desired_sell_price = price_rounded

        # === Order management with queue priority preservation ===
        if cfg.preserve_queue_priority:
            # BUY side
            if desired_buy_price is not None:
                if active_buy_id is not None and active_buy_price != desired_buy_price:
                    # Price changed → cancel and resubmit
                    hbt.cancel(0, active_buy_id, False)
                    active_buy_id = None
                    cancel_count += 1
                    price_change_count += 1
                if active_buy_id is None:
                    order_id += 1
                    hbt.submit_buy_order(0, order_id, desired_buy_price, 1.0, GTC, LIMIT, False)
                    active_buy_id = order_id
                    active_buy_price = desired_buy_price
                    quotes_submitted += 1
                # else: same price → keep order, preserve queue priority
            else:
                # Strategy doesn't want to quote buy → cancel if active
                if active_buy_id is not None:
                    hbt.cancel(0, active_buy_id, False)
                    active_buy_id = None
                    cancel_count += 1

            # SELL side
            if desired_sell_price is not None:
                if active_sell_id is not None and active_sell_price != desired_sell_price:
                    hbt.cancel(0, active_sell_id, False)
                    active_sell_id = None
                    cancel_count += 1
                    price_change_count += 1
                if active_sell_id is None:
                    order_id += 1
                    hbt.submit_sell_order(0, order_id, desired_sell_price, 1.0, GTC, LIMIT, False)
                    active_sell_id = order_id
                    active_sell_price = desired_sell_price
                    quotes_submitted += 1
            else:
                if active_sell_id is not None:
                    hbt.cancel(0, active_sell_id, False)
                    active_sell_id = None
                    cancel_count += 1
        else:
            # OLD behavior: cancel + resubmit every cycle (no queue priority)
            if active_buy_id is not None:
                hbt.cancel(0, active_buy_id, False)
                active_buy_id = None
                cancel_count += 1
            if active_sell_id is not None:
                hbt.cancel(0, active_sell_id, False)
                active_sell_id = None
                cancel_count += 1

            if desired_buy_price is not None:
                order_id += 1
                hbt.submit_buy_order(0, order_id, desired_buy_price, 1.0, GTC, LIMIT, False)
                active_buy_id = order_id
                active_buy_price = desired_buy_price
                quotes_submitted += 1
            if desired_sell_price is not None:
                order_id += 1
                hbt.submit_sell_order(0, order_id, desired_sell_price, 1.0, GTC, LIMIT, False)
                active_sell_id = order_id
                active_sell_price = desired_sell_price
                quotes_submitted += 1

        hbt.clear_inactive_orders(0)

        # Check if we got filled (position changed) → need to reset that side
        new_pos = int(hbt.position(0))
        if new_pos != cur_pos:
            # A fill happened — the filled order is now inactive
            if new_pos > cur_pos:
                # Buy fill
                active_buy_id = None
                active_buy_price = 0.0
            else:
                # Sell fill
                active_sell_id = None
                active_sell_price = 0.0

        step_count += 1

    hbt.close()

    eq_arr = np.array(equity_curve, dtype=np.float64) if equity_curve else np.array([0.0])
    pnl_arr = eq_arr - eq_arr[0]
    total_pnl = float(pnl_arr[-1])
    running_max = np.maximum.accumulate(pnl_arr)
    drawdowns = running_max - pnl_arr
    max_dd = float(np.max(drawdowns))
    avg_spread = spread_sum / spread_count if spread_count > 0 else 0.0
    pct_above = spread_above_threshold / spread_count * 100 if spread_count > 0 else 0.0

    return DayResult(
        config_name=cfg.name, date=date_str,
        total_pnl_pts=round(total_pnl, 2), total_fills=fill_count,
        max_drawdown=round(max_dd, 2), final_position=int(hbt.position(0)) if equity_curve else 0,
        quotes_submitted=quotes_submitted, cancels=cancel_count,
        price_changes=price_change_count,
        avg_spread_pts=round(avg_spread, 2),
        pct_time_spread_gte_threshold=round(pct_above, 2),
    )


def main() -> None:
    if not DATA_FILES:
        print(f"ERROR: No TMFD6 L2 data in {DATA_DIR}")
        sys.exit(1)

    n_days = len(DATA_FILES)
    print(f"\nR47 Maker — Realistic TMFD6 Backtest (Queue Priority Preservation)")
    print(f"Data: {n_days} days, {len(CONFIGS)} configs, {n_days * len(CONFIGS)} total runs")
    print("=" * 110)

    results: dict[str, list[DayResult]] = {c.name: [] for c in CONFIGS}
    t_total = time.monotonic()

    for cfg in CONFIGS:
        print(f"\n{'='*110}")
        pq = "PQ" if cfg.preserve_queue_priority else "NO_PQ"
        print(f"CONFIG: {cfg.name} (36ms, spr>={cfg.spread_threshold_pts}, mp={cfg.max_pos}, qm={cfg.queue_model_power}, {pq})")
        print(f"{'='*110}")

        for data_path in DATA_FILES:
            t0 = time.monotonic()
            try:
                result = run_one_day(data_path, cfg)
            except Exception as exc:
                import traceback
                print(f"  FAILED {data_path.name}: {exc}")
                traceback.print_exc()
                continue
            elapsed = time.monotonic() - t0
            fill_rate = result.total_fills / result.quotes_submitted * 100 if result.quotes_submitted > 0 else 0
            cancel_rate = result.cancels / max(result.quotes_submitted, 1)
            print(
                f"  {result.date}: PnL={result.total_pnl_pts:>+9.1f} pts, "
                f"fills={result.total_fills:>5}, quotes={result.quotes_submitted:>6}, "
                f"fill%={fill_rate:>5.1f}%, cancels={result.cancels:>6}, "
                f"px_chg={result.price_changes:>5}, "
                f"avg_spr={result.avg_spread_pts:>4.1f}  ({elapsed:.1f}s)"
            )
            results[cfg.name].append(result)
            gc.collect()  # Prevent OOM from hftbacktest residual allocations

    total_elapsed = time.monotonic() - t_total

    # ── Aggregate ────────────────────────────────────────────────────
    print("\n" + "=" * 110)
    print("AGGREGATE COMPARISON")
    print("=" * 110)

    summary = {}
    for cfg_name, day_results in results.items():
        if not day_results:
            continue
        total_pnl = sum(r.total_pnl_pts for r in day_results)
        total_fills = sum(r.total_fills for r in day_results)
        total_quotes = sum(r.quotes_submitted for r in day_results)
        total_cancels = sum(r.cancels for r in day_results)
        worst_dd = max(r.max_drawdown for r in day_results)
        winning = sum(1 for r in day_results if r.total_pnl_pts > 0)
        worst_day = min(r.total_pnl_pts for r in day_results)
        best_day = max(r.total_pnl_pts for r in day_results)
        avg_spread = sum(r.avg_spread_pts for r in day_results) / len(day_results)

        daily_pnls = [r.total_pnl_pts for r in day_results]
        mean_daily = float(np.mean(daily_pnls))
        std_daily = float(np.std(daily_pnls, ddof=1)) if len(daily_pnls) > 1 else 0
        t_stat = mean_daily / (std_daily / math.sqrt(len(daily_pnls))) if std_daily > 0 else 0

        summary[cfg_name] = {
            "total_pnl_pts": round(total_pnl, 2),
            "total_pnl_ntd": round(total_pnl * POINT_VALUE_NTD, 0),
            "avg_pnl_per_day": round(mean_daily, 2),
            "std_pnl_per_day": round(std_daily, 2),
            "t_statistic": round(t_stat, 3),
            "total_fills": total_fills,
            "total_quotes": total_quotes,
            "total_cancels": total_cancels,
            "pnl_per_fill": round(total_pnl / total_fills, 3) if total_fills > 0 else 0,
            "winning_days": winning,
            "n_days": len(day_results),
            "win_rate": round(winning / len(day_results), 3),
            "worst_day": round(worst_day, 2),
            "best_day": round(best_day, 2),
            "worst_max_dd": round(worst_dd, 2),
            "avg_spread": round(avg_spread, 2),
            "daily_pnl": [round(r.total_pnl_pts, 2) for r in day_results],
        }

        pnl_fill = total_pnl / total_fills if total_fills > 0 else 0
        print(f"\n  {cfg_name}:")
        print(f"    PnL:      {total_pnl:>+10.1f} pts ({total_pnl * POINT_VALUE_NTD:>+10.0f} NTD)")
        print(f"    Avg/day:  {mean_daily:>+10.1f} pts (std={std_daily:.1f}, t={t_stat:.3f})")
        print(f"    Win:      {winning}/{len(day_results)} ({winning/len(day_results):.0%})")
        print(f"    Fills:    {total_fills:>10}  quotes: {total_quotes:>10}  cancels: {total_cancels:>10}")
        print(f"    PnL/fill: {pnl_fill:>+10.3f} pts")
        print(f"    Worst:    {worst_day:>+10.1f} pts  Best: {best_day:>+10.1f} pts  MaxDD: {worst_dd:.1f}")
        print(f"    Spread:   {avg_spread:>10.2f} pts")

    # ── Key comparison: PQ vs NO_PQ ──────────────────────────────────
    if "spr4_qm2_pq" in summary and "spr4_qm2_NO_pq" in summary:
        print("\n" + "=" * 110)
        print("QUEUE PRIORITY IMPACT (spr4, qm2)")
        print("=" * 110)
        pq = summary["spr4_qm2_pq"]
        no_pq = summary["spr4_qm2_NO_pq"]
        print(f"  WITH queue priority:    PnL={pq['total_pnl_pts']:>+10.1f}, fills={pq['total_fills']:>6}, PnL/fill={pq['pnl_per_fill']:>+.3f}, cancels={pq['total_cancels']:>8}")
        print(f"  WITHOUT queue priority: PnL={no_pq['total_pnl_pts']:>+10.1f}, fills={no_pq['total_fills']:>6}, PnL/fill={no_pq['pnl_per_fill']:>+.3f}, cancels={no_pq['total_cancels']:>8}")
        delta = pq['total_pnl_pts'] - no_pq['total_pnl_pts']
        print(f"  DELTA:                  {delta:>+10.1f} pts ({delta * POINT_VALUE_NTD:>+10.0f} NTD)")

    print(f"\nTotal elapsed: {total_elapsed:.0f}s")

    # ── Save ─────────────────────────────────────────────────────────
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "metadata": {
            "script": "explore_tmfd6_realistic.py",
            "instrument": "TMFD6",
            "n_days": n_days,
            "key_fix": "preserve_queue_priority: only cancel when quote price changes",
            "configs": {c.name: {
                "latency_ms": c.latency_ns / 1e6,
                "queue_model_power": c.queue_model_power,
                "spread_pts": c.spread_threshold_pts,
                "max_pos": c.max_pos,
                "preserve_queue": c.preserve_queue_priority,
            } for c in CONFIGS},
        },
        "summary": summary,
        "per_day": {
            cfg_name: [
                {
                    "date": r.date, "total_pnl_pts": r.total_pnl_pts,
                    "total_fills": r.total_fills, "quotes_submitted": r.quotes_submitted,
                    "cancels": r.cancels, "price_changes": r.price_changes,
                    "max_drawdown": r.max_drawdown, "final_position": r.final_position,
                    "avg_spread_pts": r.avg_spread_pts,
                }
                for r in day_results
            ]
            for cfg_name, day_results in results.items()
        },
    }
    out_path = OUT_DIR / "tmfd6_realistic.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
