"""C3b-B Stale Quote Suppression — A/B Backtest Comparison.

Compares R47 Maker with and without stale quote suppression across 12 days.

Treatment (C3b-B): When computed quote price == previously submitted price,
  DON'T cancel the existing order. Keep it resting to preserve queue position.
Baseline: Cancel and resubmit every step (standard R47 behavior).

The hypothesis: preserving queue position when price hasn't changed → more fills
  (better queue priority) → higher PnL.

Usage:
    uv run python research/alphas/r47_maker_pivot/explore_c3b_b_stale_suppression.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

os.environ.setdefault("HFT_STRICT_PRICE_MODE", "0")

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "src"))

# Suppress logging
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging
logging.disable(logging.WARNING)

from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
from hftbacktest.order import GTC, LIMIT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TICK_SIZE = 1.0
LOT_SIZE = 1.0
POINT_VALUE_NTD = 10
PRICE_SCALE = 10_000
ELAPSE_NS = 100_000_000  # 100ms

DATA_DIR = _REPO_ROOT / "research" / "data" / "raw" / "txfd6"
DATA_FILES = sorted(DATA_DIR.glob("TXFD6_2026-0*_l2.hftbt.npz"))
OUT_DIR = _REPO_ROOT / "outputs" / "team_artifacts" / "alpha-research" / "R51_optimal_execution"

_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9
_IDX_L1_IMBALANCE_PPM = 10


def _load_r47_class():
    from hft_platform.strategies.r47_maker import R47MakerStrategy
    return R47MakerStrategy


@dataclass
class DayResult:
    date: str
    mode: str  # "baseline" or "c3b_b"
    total_pnl_pts: float
    total_fills: int
    total_steps: int
    stale_suppressed: int
    quotes_sent: int
    elapsed_s: float


def run_day(data_path: Path, stale_suppression: bool) -> DayResult:
    """Run R47 on one day with or without stale quote suppression.

    Key difference in harness behavior:
    - baseline: cancel ALL orders every step, resubmit from strategy intents
    - c3b_b: only cancel an order if strategy emits a NEW intent at a DIFFERENT price.
             If strategy suppresses (same price), keep existing order resting.
    """
    from hft_platform.contracts.strategy import IntentType, Side, TIF, OrderIntent
    from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
    from hft_platform.strategy.base import StrategyContext

    date_str = data_path.stem.replace("TXFD6_", "").replace("_l2.hftbt", "")

    R47Cls = _load_r47_class()
    strategy = R47Cls(
        strategy_id="r47_maker",
        pe_danger_threshold=0.55,
        pe_window=100,
        queue_cancel_threshold=0.7,
        mfg_skew_z_threshold=2.0,
        spread_threshold_pts=1,
        toxicity_max=700,
        max_pos=3,
    )

    # For baseline mode, we reset _last_bid/ask_price to 0 each step in the
    # main loop so the stale suppression condition (price == last_price) never fires.

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

    def intent_factory(*, strategy_id, symbol, side, price, qty, tif, intent_type, **kw):
        intent_seq[0] += 1
        intent = OrderIntent(
            intent_id=intent_seq[0],
            strategy_id=strategy_id,
            symbol=symbol,
            intent_type=intent_type,
            side=side,
            price=price,
            qty=qty,
            tif=tif,
        )
        captured_intents.append(intent)
        return intent

    def scale_price(symbol, price):
        from decimal import Decimal
        if isinstance(price, int):
            return price
        return int(Decimal(str(price)) * Decimal(PRICE_SCALE))

    ctx = StrategyContext(
        positions=positions,
        strategy_id=strategy.strategy_id,
        intent_factory=intent_factory,
        price_scaler=scale_price,
    )

    mode_str = "c3b_b" if stale_suppression else "baseline"
    t0 = time.monotonic()

    order_id = 0
    active_buy_id: int | None = None
    active_sell_id: int | None = None
    active_buy_price: float = 0.0
    active_sell_price: float = 0.0

    fill_count = 0
    prev_pos = 0
    step_count = 0

    while hbt.elapse(ELAPSE_NS) == 0:
        dp = hbt.depth(0)
        best_bid = dp.best_bid
        best_ask = dp.best_ask

        if best_bid != best_bid or best_ask != best_ask:
            continue
        if best_bid <= 0 or best_ask >= 2147483647 or best_bid >= best_ask:
            continue

        ts_ns = int(hbt.current_timestamp)

        # Track fills via position change
        cur_pos = int(hbt.position(0))
        if cur_pos != prev_pos:
            fill_count += abs(cur_pos - prev_pos)
        prev_pos = cur_pos

        positions["TXFD6"] = cur_pos

        # Build events and dispatch
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
        # Baseline mode: reset last prices so stale suppression never fires
        if not stale_suppression:
            strategy._last_bid_price = 0
            strategy._last_ask_price = 0
        strategy.handle_event(ctx, feature_event)
        strategy.handle_event(ctx, lob_event)

        # --- Order management: baseline vs c3b_b ---
        if not stale_suppression:
            # BASELINE: Cancel all, resubmit everything from intents
            if active_buy_id is not None:
                hbt.cancel(0, active_buy_id, False)
                active_buy_id = None
            if active_sell_id is not None:
                hbt.cancel(0, active_sell_id, False)
                active_sell_id = None
            hbt.clear_inactive_orders(0)

            for intent in captured_intents:
                if intent.intent_type != IntentType.NEW:
                    continue
                price_native = intent.price / PRICE_SCALE
                price_rounded = round(price_native / TICK_SIZE) * TICK_SIZE

                if intent.side == Side.BUY:
                    order_id += 1
                    hbt.submit_buy_order(0, order_id, price_rounded, float(intent.qty), GTC, LIMIT, False)
                    active_buy_id = order_id
                    active_buy_price = price_rounded
                elif intent.side == Side.SELL:
                    order_id += 1
                    hbt.submit_sell_order(0, order_id, price_rounded, float(intent.qty), GTC, LIMIT, False)
                    active_sell_id = order_id
                    active_sell_price = price_rounded
        else:
            # C3b-B: Only cancel+resubmit when price changes.
            # If no intent emitted for a side (stale suppression), keep existing order.
            buy_intent = None
            sell_intent = None
            for intent in captured_intents:
                if intent.intent_type != IntentType.NEW:
                    continue
                if intent.side == Side.BUY:
                    buy_intent = intent
                elif intent.side == Side.SELL:
                    sell_intent = intent

            # If strategy emitted no intents at all (gate blocked), cancel everything
            if not captured_intents:
                if active_buy_id is not None:
                    hbt.cancel(0, active_buy_id, False)
                    active_buy_id = None
                    active_buy_price = 0.0
                if active_sell_id is not None:
                    hbt.cancel(0, active_sell_id, False)
                    active_sell_id = None
                    active_sell_price = 0.0
                hbt.clear_inactive_orders(0)
            else:
                # Handle buy side
                if buy_intent is not None:
                    price_native = buy_intent.price / PRICE_SCALE
                    price_rounded = round(price_native / TICK_SIZE) * TICK_SIZE
                    # New price -> cancel old, submit new
                    if active_buy_id is not None:
                        hbt.cancel(0, active_buy_id, False)
                    order_id += 1
                    hbt.submit_buy_order(0, order_id, price_rounded, float(buy_intent.qty), GTC, LIMIT, False)
                    active_buy_id = order_id
                    active_buy_price = price_rounded
                # else: no buy intent = stale suppression fired, keep existing order

                # Handle sell side
                if sell_intent is not None:
                    price_native = sell_intent.price / PRICE_SCALE
                    price_rounded = round(price_native / TICK_SIZE) * TICK_SIZE
                    if active_sell_id is not None:
                        hbt.cancel(0, active_sell_id, False)
                    order_id += 1
                    hbt.submit_sell_order(0, order_id, price_rounded, float(sell_intent.qty), GTC, LIMIT, False)
                    active_sell_id = order_id
                    active_sell_price = price_rounded
                # else: no sell intent = stale suppression fired, keep existing order

                hbt.clear_inactive_orders(0)

                # Check if our resting orders were filled (order no longer active)
                # hftbacktest handles this internally — if order was filled, it's gone
                # We just need to make sure we don't hold stale active_*_id references
                # This is handled by position tracking above

        step_count += 1

    hbt.close()
    elapsed = time.monotonic() - t0

    # Final PnL
    sv = hbt.state_values(0)
    final_pos = int(hbt.position(0))
    mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0
    total_pnl = sv.balance + final_pos * mid

    return DayResult(
        date=date_str,
        mode=mode_str,
        total_pnl_pts=round(total_pnl, 2),
        total_fills=fill_count,
        total_steps=step_count,
        stale_suppressed=strategy._stale_suppressed,
        quotes_sent=strategy._quotes_sent,
        elapsed_s=round(elapsed, 1),
    )


def main():
    if not DATA_FILES:
        print("ERROR: No TXFD6 data files found in", DATA_DIR)
        sys.exit(1)

    print(f"C3b-B Stale Quote Suppression A/B Backtest")
    print(f"Data files: {len(DATA_FILES)} days")
    print(f"{'='*80}")

    baseline_results: list[DayResult] = []
    treatment_results: list[DayResult] = []

    for i, data_path in enumerate(DATA_FILES):
        date_str = data_path.stem.replace("TXFD6_", "").replace("_l2.hftbt", "")
        print(f"\n[{i+1}/{len(DATA_FILES)}] {date_str}")

        # Run baseline (no stale suppression)
        print(f"  Running BASELINE...", end=" ", flush=True)
        baseline = run_day(data_path, stale_suppression=False)
        print(f"PnL={baseline.total_pnl_pts:+.1f} fills={baseline.total_fills} t={baseline.elapsed_s}s")
        baseline_results.append(baseline)

        # Run treatment (with stale suppression)
        print(f"  Running C3b-B...", end=" ", flush=True)
        treatment = run_day(data_path, stale_suppression=True)
        print(f"PnL={treatment.total_pnl_pts:+.1f} fills={treatment.total_fills} "
              f"stale_skip={treatment.stale_suppressed} t={treatment.elapsed_s}s")
        treatment_results.append(treatment)

        delta_pnl = treatment.total_pnl_pts - baseline.total_pnl_pts
        delta_fills = treatment.total_fills - baseline.total_fills
        print(f"  Delta: PnL={delta_pnl:+.1f} fills={delta_fills:+d}")

    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")

    total_baseline_pnl = sum(r.total_pnl_pts for r in baseline_results)
    total_treatment_pnl = sum(r.total_pnl_pts for r in treatment_results)
    total_baseline_fills = sum(r.total_fills for r in baseline_results)
    total_treatment_fills = sum(r.total_fills for r in treatment_results)
    total_stale_suppressed = sum(r.stale_suppressed for r in treatment_results)

    print(f"Baseline:  PnL={total_baseline_pnl:+.1f} pts  fills={total_baseline_fills}")
    print(f"C3b-B:     PnL={total_treatment_pnl:+.1f} pts  fills={total_treatment_fills}")
    print(f"Delta:     PnL={total_treatment_pnl - total_baseline_pnl:+.1f} pts  "
          f"fills={total_treatment_fills - total_baseline_fills:+d}")
    print(f"Stale suppressed: {total_stale_suppressed}")

    if total_baseline_fills > 0:
        fill_rate_change = (total_treatment_fills - total_baseline_fills) / total_baseline_fills * 100
        print(f"Fill rate change: {fill_rate_change:+.1f}%")
    if total_baseline_pnl != 0:
        pnl_change = (total_treatment_pnl - total_baseline_pnl) / abs(total_baseline_pnl) * 100
        print(f"PnL change: {pnl_change:+.1f}%")

    # Per-day comparison table
    print(f"\n{'Date':<12} {'Base PnL':>10} {'C3b PnL':>10} {'Delta':>10} "
          f"{'Base Fills':>11} {'C3b Fills':>10} {'Stale Skip':>11}")
    print("-" * 85)
    for b, t in zip(baseline_results, treatment_results):
        delta = t.total_pnl_pts - b.total_pnl_pts
        print(f"{b.date:<12} {b.total_pnl_pts:>+10.1f} {t.total_pnl_pts:>+10.1f} {delta:>+10.1f} "
              f"{b.total_fills:>11d} {t.total_fills:>10d} {t.stale_suppressed:>11d}")

    # Save results
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "experiment": "C3b-B Stale Quote Suppression",
        "date": "2026-04-09",
        "hypothesis": "Preserving queue position when price unchanged → more fills → higher PnL",
        "data_files": len(DATA_FILES),
        "baseline_config": {
            "stale_suppression": False,
            "description": "Cancel all orders every step, resubmit from strategy intents (standard R47)"
        },
        "treatment_config": {
            "stale_suppression": True,
            "description": "Keep existing order when strategy suppresses (same price). Only cancel+resubmit on price change."
        },
        "summary": {
            "baseline_total_pnl_pts": total_baseline_pnl,
            "treatment_total_pnl_pts": total_treatment_pnl,
            "pnl_delta_pts": round(total_treatment_pnl - total_baseline_pnl, 2),
            "pnl_change_pct": round((total_treatment_pnl - total_baseline_pnl) / abs(total_baseline_pnl) * 100, 2) if total_baseline_pnl != 0 else 0,
            "baseline_total_fills": total_baseline_fills,
            "treatment_total_fills": total_treatment_fills,
            "fill_delta": total_treatment_fills - total_baseline_fills,
            "fill_rate_change_pct": round((total_treatment_fills - total_baseline_fills) / total_baseline_fills * 100, 2) if total_baseline_fills > 0 else 0,
            "total_stale_suppressed": total_stale_suppressed,
        },
        "per_day": [
            {
                "date": b.date,
                "baseline_pnl_pts": b.total_pnl_pts,
                "treatment_pnl_pts": t.total_pnl_pts,
                "pnl_delta_pts": round(t.total_pnl_pts - b.total_pnl_pts, 2),
                "baseline_fills": b.total_fills,
                "treatment_fills": t.total_fills,
                "fill_delta": t.total_fills - b.total_fills,
                "stale_suppressed": t.stale_suppressed,
                "baseline_quotes_sent": b.quotes_sent,
                "treatment_quotes_sent": t.quotes_sent,
            }
            for b, t in zip(baseline_results, treatment_results)
        ],
    }

    out_path = OUT_DIR / "c3b_b_backtest_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
