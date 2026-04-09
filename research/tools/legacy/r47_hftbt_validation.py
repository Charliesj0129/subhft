"""R47 Maker Pivot — hftbacktest validation with PowerProbQueueModel.

Validates that the R47MakerStrategy has positive expected PnL under
realistic queue position simulation using the direct hftbacktest API.

Two modes:
  1. Signal-gated (R47 full): PE + Queue Survival + MFG inventory skew
  2. Naive baseline: symmetric quotes, no signal gating

Usage:
    uv run python research/tools/r47_hftbt_validation.py
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

os.environ.setdefault("HFT_STRICT_PRICE_MODE", "0")

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

# Suppress structlog
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging
logging.disable(logging.WARNING)

from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
from hftbacktest.order import GTC, LIMIT

print_log = print

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
OUT_DIR = _REPO_ROOT / "outputs" / "team_artifacts" / "alpha-research" / "R47_maker_pivot"

_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9
_IDX_L1_IMBALANCE_PPM = 10


def _load_r47_class():
    impl_path = _REPO_ROOT / "research" / "alphas" / "r47_maker_pivot" / "impl.py"
    spec = importlib.util.spec_from_file_location("r47_impl", str(impl_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.R47MakerStrategy


@dataclass
class DayResult:
    date: str
    mode: str
    total_pnl_pts: float
    total_pnl_ntd: float
    buy_fills: int
    sell_fills: int
    total_fills: int
    win_rate: float
    max_drawdown_pts: float
    sharpe: float
    duration_hours: float
    fill_rate_per_hour: float
    final_position: int = 0
    pe_blocked: int = 0
    queue_suppressed: int = 0
    mfg_skewed: int = 0
    spread_blocked: int = 0
    toxicity_blocked: int = 0
    quotes_sent: int = 0


def run_one_day(data_path: Path, mode: str = "r47") -> DayResult:
    """Run MM strategy on one day using direct hftbacktest API."""
    from hft_platform.contracts.strategy import IntentType, Side, TIF, OrderIntent
    from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
    from hft_platform.strategy.base import StrategyContext

    date_str = data_path.stem.replace("TXFD6_", "").replace("_l2.hftbt", "")

    # Load strategy
    if mode == "r47":
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
    else:
        from hft_platform.strategies.simple_mm import SimpleMarketMaker
        strategy = SimpleMarketMaker(strategy_id="naive_mm", max_pos=3)

    # Create hftbacktest instance
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

    # Create StrategyContext
    positions = {"TXFD6": 0}
    intent_seq = [0]
    captured_intents: list[OrderIntent] = []

    def intent_factory(strategy_id, symbol, side, price, qty, tif, intent_type, **kw):
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

    print_log(f"  [{mode}] {date_str}: starting simulation...")
    t0 = time.monotonic()

    # State tracking
    order_id = 0
    active_buy_id: int | None = None
    active_sell_id: int | None = None

    # Equity curve (sampled every 10 steps = 1s)
    equity_curve: list[float] = []
    equity_ts_list: list[int] = []
    step_count = 0
    first_ts = 0
    last_ts = 0

    while hbt.elapse(ELAPSE_NS) == 0:
        dp = hbt.depth(0)
        best_bid = dp.best_bid
        best_ask = dp.best_ask

        if best_bid != best_bid or best_ask != best_ask:
            continue
        if best_bid <= 0 or best_ask >= 2147483647 or best_bid >= best_ask:
            continue

        ts_ns = int(hbt.current_timestamp)
        if first_ts == 0:
            first_ts = ts_ns
        last_ts = ts_ns

        # Cancel previous orders before computing new quotes
        if active_buy_id is not None:
            hbt.cancel(0, active_buy_id, False)
            active_buy_id = None
        if active_sell_id is not None:
            hbt.cancel(0, active_sell_id, False)
            active_sell_id = None
        hbt.clear_inactive_orders(0)

        # Sync position
        pos = int(hbt.position(0))
        positions["TXFD6"] = pos

        # Resolve L1 quantities
        bid_qty = int(getattr(dp, "best_bid_qty", 0) or 0)
        ask_qty = int(getattr(dp, "best_ask_qty", 0) or 0)

        # Build scaled prices for strategy
        bid_scaled = int(round(best_bid * PRICE_SCALE))
        ask_scaled = int(round(best_ask * PRICE_SCALE))

        total_qty = bid_qty + ask_qty
        imbalance = (bid_qty - ask_qty) / total_qty if total_qty > 0 else 0.0

        # Build events
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

        # Dispatch events to strategy
        captured_intents.clear()
        strategy.handle_event(ctx, feature_event)
        strategy.handle_event(ctx, lob_event)

        # Execute intents: submit at most 1 buy + 1 sell
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

        # Record equity every 10 steps (~1s)
        if step_count % 10 == 0:
            sv = hbt.state_values(0)
            mid = (best_bid + best_ask) / 2.0
            equity = sv.balance + pos * mid
            equity_curve.append(equity)
            equity_ts_list.append(ts_ns)

        step_count += 1

    # Final state
    sv = hbt.state_values(0)
    final_pos = int(hbt.position(0))
    final_mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask < 2147483647 else 0.0
    total_pnl_pts = sv.balance + final_pos * final_mid
    total_pnl_ntd = total_pnl_pts * POINT_VALUE_NTD
    buy_fills_count = 0
    sell_fills_count = 0
    num_trades = int(sv.num_trades)
    # hftbacktest num_trades counts individual fills; approximate buy/sell split
    # We can't get exact split from state_values alone, but can estimate
    # from trading_volume and position
    trading_vol = int(sv.trading_volume)
    if final_pos >= 0:
        buy_fills_count = (trading_vol + final_pos) // 2
        sell_fills_count = trading_vol - buy_fills_count
    else:
        sell_fills_count = (trading_vol - final_pos) // 2
        buy_fills_count = trading_vol - sell_fills_count
    total_fills = int(trading_vol)

    hbt.close()
    elapsed = time.monotonic() - t0

    # Equity curve analysis
    eq_arr = np.array(equity_curve, dtype=np.float64) if equity_curve else np.array([0.0])
    if len(eq_arr) >= 2:
        returns = np.diff(eq_arr)
        cumulative = eq_arr - eq_arr[0]
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = running_max - cumulative
        max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
        win_rate = float(np.mean(returns > 0)) if len(returns) > 0 else 0.0
        if np.std(returns) > 1e-12:
            samples_per_year = len(eq_arr) * 252
            sharpe = float(np.mean(returns)) / float(np.std(returns)) * math.sqrt(samples_per_year)
        else:
            sharpe = 0.0
    else:
        max_dd = 0.0
        win_rate = 0.0
        sharpe = 0.0

    duration_ns = last_ts - first_ts if last_ts > first_ts else 0
    duration_hours = duration_ns / 3.6e12
    fill_rate = total_fills / duration_hours if duration_hours > 0 else 0.0

    # Strategy counters
    pe_blocked = getattr(strategy, "_pe_blocked", 0)
    queue_suppressed = getattr(strategy, "_queue_suppressed", 0)
    mfg_skewed = getattr(strategy, "_mfg_skewed", 0)
    spread_blocked = getattr(strategy, "_spread_blocked", 0)
    toxicity_blocked = getattr(strategy, "_toxicity_blocked", 0)
    quotes_sent = getattr(strategy, "_quotes_sent", 0)

    result = DayResult(
        date=date_str,
        mode=mode,
        total_pnl_pts=round(total_pnl_pts, 2),
        total_pnl_ntd=round(total_pnl_ntd, 0),
        buy_fills=buy_fills_count,
        sell_fills=sell_fills_count,
        total_fills=total_fills,
        win_rate=round(win_rate, 4),
        max_drawdown_pts=round(max_dd, 2),
        sharpe=round(sharpe, 2),
        duration_hours=round(duration_hours, 2),
        fill_rate_per_hour=round(fill_rate, 1),
        final_position=final_pos,
        pe_blocked=pe_blocked,
        queue_suppressed=queue_suppressed,
        mfg_skewed=mfg_skewed,
        spread_blocked=spread_blocked,
        toxicity_blocked=toxicity_blocked,
        quotes_sent=quotes_sent,
    )

    print_log(
        f"  [{mode}] {date_str}: PnL={result.total_pnl_pts:+.1f} pts "
        f"({result.total_pnl_ntd:+.0f} NTD), fills={total_fills} "
        f"(B:{buy_fills_count}/S:{sell_fills_count}), win={result.win_rate:.1%}, "
        f"dd={max_dd:.1f}, Sharpe={sharpe:.2f}, pos={final_pos}, "
        f"steps={step_count}, elapsed={elapsed:.1f}s"
    )

    return result


def main() -> None:
    if not DATA_FILES:
        print_log(f"ERROR: No data files found in {DATA_DIR}")
        sys.exit(1)

    print_log(f"\nR47 Maker Pivot -- hftbacktest Validation")
    print_log(f"Data: {len(DATA_FILES)} days, Queue: PowerProbQueueModel(3.0)")
    print_log(f"Latency: 47us, Elapse: 100ms, MaxPos: 3")
    print_log("=" * 60)

    all_results: list[DayResult] = []
    for data_path in DATA_FILES:
        for mode_str in ("r47", "naive"):
            try:
                result = run_one_day(data_path, mode=mode_str)
                all_results.append(result)
            except Exception as exc:
                import traceback
                print_log(f"  [{mode_str}] FAILED: {data_path.name}: {type(exc).__name__}: {exc}")
                traceback.print_exc()

    r47_results = [r for r in all_results if r.mode == "r47"]
    naive_results = [r for r in all_results if r.mode == "naive"]

    def aggregate(results: list[DayResult]) -> dict:
        if not results:
            return {"error": "no results"}
        total_pnl = sum(r.total_pnl_pts for r in results)
        total_fills = sum(r.total_fills for r in results)
        total_hours = sum(r.duration_hours for r in results)
        return {
            "n_days": len(results),
            "total_pnl_pts": round(total_pnl, 2),
            "total_pnl_ntd": round(total_pnl * POINT_VALUE_NTD, 0),
            "avg_pnl_pts_per_day": round(total_pnl / len(results), 2),
            "total_fills": total_fills,
            "buy_fills": sum(r.buy_fills for r in results),
            "sell_fills": sum(r.sell_fills for r in results),
            "avg_win_rate": round(float(np.mean([r.win_rate for r in results])), 4),
            "worst_max_dd_pts": round(max(r.max_drawdown_pts for r in results), 2),
            "avg_sharpe": round(float(np.mean([r.sharpe for r in results])), 2),
            "total_hours": round(total_hours, 2),
            "fill_rate_per_hour": round(total_fills / total_hours, 1) if total_hours > 0 else 0.0,
        }

    r47_agg = aggregate(r47_results)
    naive_agg = aggregate(naive_results)

    print_log("\n" + "=" * 60)
    print_log("AGGREGATE RESULTS")
    print_log("=" * 60)

    print_log("\n--- R47 Signal-Gated ---")
    for k, v in r47_agg.items():
        print_log(f"  {k}: {v}")

    print_log("\n--- Naive Symmetric MM ---")
    for k, v in naive_agg.items():
        print_log(f"  {k}: {v}")

    if r47_results:
        print_log("\n--- R47 Signal Gate Activity ---")
        for r in r47_results:
            print_log(
                f"  {r.date}: PE_blk={r.pe_blocked}, Q_sup={r.queue_suppressed}, "
                f"MFG_skew={r.mfg_skewed}, spr_blk={r.spread_blocked}, "
                f"tox_blk={r.toxicity_blocked}, quotes={r.quotes_sent}"
            )

    if isinstance(r47_agg.get("total_pnl_pts"), (int, float)) and isinstance(naive_agg.get("total_pnl_pts"), (int, float)):
        r47_pnl = r47_agg["total_pnl_pts"]
        naive_pnl = naive_agg["total_pnl_pts"]
        improvement = r47_pnl - naive_pnl
        print_log(f"\n--- Signal Gating Improvement ---")
        print_log(f"  R47 - Naive = {improvement:+.2f} pts ({improvement * POINT_VALUE_NTD:+.0f} NTD)")

    # Save JSON
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_data = {
        "r47_aggregate": r47_agg,
        "naive_aggregate": naive_agg,
        "r47_per_day": [
            {k: getattr(r, k) for k in [
                "date", "total_pnl_pts", "total_pnl_ntd", "total_fills", "buy_fills",
                "sell_fills", "win_rate", "max_drawdown_pts", "sharpe", "duration_hours",
                "fill_rate_per_hour", "final_position", "pe_blocked", "queue_suppressed",
                "mfg_skewed", "spread_blocked", "toxicity_blocked", "quotes_sent",
            ]}
            for r in r47_results
        ],
        "naive_per_day": [
            {k: getattr(r, k) for k in [
                "date", "total_pnl_pts", "total_pnl_ntd", "total_fills", "buy_fills",
                "sell_fills", "win_rate", "max_drawdown_pts", "sharpe", "duration_hours",
                "final_position",
            ]}
            for r in naive_results
        ],
        "config": {
            "queue_model": "PowerProbQueueModel(3.0)",
            "latency_us": 47, "elapse_ns": ELAPSE_NS,
            "tick_size": TICK_SIZE, "max_pos": 3, "price_scale": PRICE_SCALE,
            "maker_fee": 0.0, "taker_fee": 0.0, "partial_fill": True,
        },
    }
    json_path = OUT_DIR / "hftbt_validation_results.json"
    with open(json_path, "w") as f:
        json.dump(out_data, f, indent=2)

    # Save markdown
    md_path = OUT_DIR / "hftbt_validation_results.md"
    with open(md_path, "w") as f:
        f.write("# R47 Maker Pivot -- hftbacktest Validation Results\n\n")
        f.write("## Configuration\n\n")
        f.write("- Queue model: PowerProbQueueModel(3.0)\n")
        f.write("- Latency: 47us (conservative P95)\n")
        f.write("- Tick mode: elapse @ 100ms\n")
        f.write("- Max position: 3 lots\n")
        f.write("- Fees: 0 (TAIFEX no maker/taker)\n")
        f.write("- Partial fill: enabled\n")
        f.write(f"- Data: {len(DATA_FILES)} days of TXFD6 L2\n\n")

        for label, agg in [("R47 Signal-Gated", r47_agg), ("Naive Symmetric MM", naive_agg)]:
            f.write(f"## {label} Results\n\n| Metric | Value |\n|--------|-------|\n")
            for k, v in agg.items():
                f.write(f"| {k} | {v} |\n")
            f.write("\n")

        f.write("## Per-Day R47 Details\n\n")
        f.write("| Date | PnL (pts) | PnL (NTD) | Fills (B/S) | Win% | MaxDD | Sharpe | Pos |\n")
        f.write("|------|-----------|-----------|-------------|------|-------|--------|-----|\n")
        for r in r47_results:
            f.write(
                f"| {r.date} | {r.total_pnl_pts:+.1f} | {r.total_pnl_ntd:+.0f} | "
                f"{r.total_fills} ({r.buy_fills}/{r.sell_fills}) | {r.win_rate:.1%} | "
                f"{r.max_drawdown_pts:.1f} | {r.sharpe:.2f} | {r.final_position} |\n"
            )

        f.write("\n## Per-Day Naive Details\n\n")
        f.write("| Date | PnL (pts) | PnL (NTD) | Fills (B/S) | Win% | MaxDD | Sharpe | Pos |\n")
        f.write("|------|-----------|-----------|-------------|------|-------|--------|-----|\n")
        for r in naive_results:
            f.write(
                f"| {r.date} | {r.total_pnl_pts:+.1f} | {r.total_pnl_ntd:+.0f} | "
                f"{r.total_fills} ({r.buy_fills}/{r.sell_fills}) | {r.win_rate:.1%} | "
                f"{r.max_drawdown_pts:.1f} | {r.sharpe:.2f} | {r.final_position} |\n"
            )

        if r47_results:
            f.write("\n## R47 Signal Gate Activity\n\n")
            f.write("| Date | PE Blocked | Q Suppressed | MFG Skewed | Spread Blocked | Quotes |\n")
            f.write("|------|------------|--------------|------------|----------------|--------|\n")
            for r in r47_results:
                f.write(
                    f"| {r.date} | {r.pe_blocked} | {r.queue_suppressed} | "
                    f"{r.mfg_skewed} | {r.spread_blocked} | {r.quotes_sent} |\n"
                )

        if isinstance(r47_agg.get("total_pnl_pts"), (int, float)) and isinstance(naive_agg.get("total_pnl_pts"), (int, float)):
            improvement = r47_agg["total_pnl_pts"] - naive_agg["total_pnl_pts"]
            f.write(f"\n## Signal Gating Improvement\n\n")
            f.write(f"- R47 - Naive = **{improvement:+.2f} pts** ({improvement * POINT_VALUE_NTD:+.0f} NTD)\n")

    print_log(f"\nResults: {json_path}")
    print_log(f"Report:  {md_path}")


if __name__ == "__main__":
    main()
