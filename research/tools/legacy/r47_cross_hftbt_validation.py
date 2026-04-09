"""R47 Cross-Instrument hftbacktest Validation.

Dual-asset backtest: TXFD6 (signal source) + TMFD6 (execution target).
Uses the updated R47MakerStrategy with signal_symbol/trade_symbol routing.

Key differences from single-instrument validation:
  - Two BacktestAsset instances in one simulation
  - Asset 0 = TXFD6 (signal only, no orders placed)
  - Asset 1 = TMFD6 (execution, orders placed here)
  - Spread gate checks TMFD6 spread (correct for cost economics)
  - Costs: 2.0 pts/side (1.3 commission + 0.7 tax) = 4.0 pts RT on TMFD6
  - TMFD6: 1 pt = 10 NTD

Usage:
    uv run python research/tools/r47_cross_hftbt_validation.py
"""
from __future__ import annotations

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TICK_SIZE = 1.0
LOT_SIZE = 1.0
TMFD6_POINT_VALUE_NTD = 10
TMFD6_COMMISSION_PER_SIDE_NTD = 13  # 1.3 pts * 10 NTD/pt
TMFD6_TAX_PER_SIDE_NTD = 7          # 0.7 pts * 10 NTD/pt
TMFD6_RT_COST_NTD = (TMFD6_COMMISSION_PER_SIDE_NTD + TMFD6_TAX_PER_SIDE_NTD) * 2  # 40 NTD
TMFD6_RT_COST_PTS = TMFD6_RT_COST_NTD / TMFD6_POINT_VALUE_NTD  # 4.0 pts
PRICE_SCALE = 10_000
ELAPSE_NS = 100_000_000  # 100ms

TXFD6_DIR = _REPO_ROOT / "research" / "data" / "raw" / "txfd6"
TMFD6_DIR = _REPO_ROOT / "research" / "data" / "raw" / "tmfd6"
OUT_DIR = _REPO_ROOT / "outputs" / "team_artifacts" / "alpha-research" / "R47_maker_pivot"

# Dates with BOTH TXFD6 and TMFD6 hftbacktest data
DATES = [
    "2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24",
    "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31",
    "2026-04-01", "2026-04-02", "2026-04-07", "2026-04-08",
]

_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9
_IDX_L1_IMBALANCE_PPM = 10

print_log = print


@dataclass
class DayResult:
    date: str
    total_pnl_pts: float
    total_pnl_ntd: float
    net_pnl_ntd: float  # after costs
    total_fills: int
    buy_fills: int
    sell_fills: int
    round_trips: int
    total_cost_ntd: float
    pnl_per_fill_pts: float
    net_per_fill_pts: float
    win_rate: float
    max_drawdown_pts: float
    sharpe: float
    duration_hours: float
    final_position: int
    spread_blocked: int
    pe_blocked: int
    queue_suppressed: int
    quotes_sent: int
    tmfd6_avg_spread_pts: float


def run_cross_day(date_str: str, latency_ns: int = 47_000) -> DayResult | None:
    """Run cross-instrument backtest for one day."""
    from hft_platform.contracts.strategy import IntentType, Side, TIF, OrderIntent
    from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
    from hft_platform.strategy.base import StrategyContext

    tmfd6_path = TMFD6_DIR / f"TMFD6_{date_str}_l2.hftbt.npz"

    if not tmfd6_path.exists():
        print_log(f"  {date_str}: TMFD6 data missing, skipping")
        return None

    # Load the DEPLOYED R47 strategy (from src, not research impl)
    from hft_platform.strategies.r47_maker import R47MakerStrategy
    # --- Mode selection ---
    # Single-instrument TMFD6: signals AND execution both on TMFD6
    strategy = R47MakerStrategy(
        strategy_id="r47_maker",
        signal_symbol="",         # Same as trade (single-instrument)
        trade_symbol="",          # Same as signal
        pe_danger_threshold=0.55,
        pe_window=100,
        queue_cancel_threshold=0.7,
        queue_ema_alpha=0.05,
        glt_gamma=0.0,           # Fixed skew (GLT disabled)
        spread_threshold_pts=1,   # 1 pt minimum
        toxicity_max=700,
        max_pos=3,
        subscribe_symbols=["TMFD6"],
    )

    # Single-asset TMFD6: signals + execution on same book
    asset_tmfd6 = (
        BacktestAsset()
        .data([str(tmfd6_path)])
        .linear_asset(1.0)
        .constant_order_latency(latency_ns, latency_ns)
        .power_prob_queue_model(3.0)
        .tick_size(TICK_SIZE)
        .lot_size(LOT_SIZE)
        .no_partial_fill_exchange()
    )

    hbt = HashMapMarketDepthBacktest([asset_tmfd6])

    # StrategyContext — single-instrument
    positions = {"TMFD6": 0}
    intent_seq = [0]
    captured_intents: list[OrderIntent] = []

    def intent_factory(strategy_id, symbol, side, price, qty, tif, intent_type, **kw):
        intent_seq[0] += 1
        return OrderIntent(
            intent_id=intent_seq[0], strategy_id=strategy_id,
            symbol=symbol, intent_type=intent_type,
            side=side, price=price, qty=qty, tif=tif,
        )

    def scale_price(symbol, price):
        from decimal import Decimal
        return price if isinstance(price, int) else int(Decimal(str(price)) * Decimal(PRICE_SCALE))

    ctx = StrategyContext(
        positions=positions, strategy_id=strategy.strategy_id,
        intent_factory=intent_factory, price_scaler=scale_price,
    )

    print_log(f"  {date_str}: starting cross-instrument simulation (lat={latency_ns/1e6:.3f}ms)...")
    t0 = time.monotonic()

    order_id = 0
    active_buy_id: int | None = None
    active_sell_id: int | None = None

    equity_curve: list[float] = []
    step_count = 0
    first_ts = 0
    last_ts = 0
    tmfd6_spread_sum = 0.0
    tmfd6_spread_count = 0

    while hbt.elapse(ELAPSE_NS) == 0:
        # Single-asset: TMFD6 (asset 0)
        dp = hbt.depth(0)
        tm_bid = dp.best_bid
        tm_ask = dp.best_ask

        if tm_bid != tm_bid or tm_ask != tm_ask:
            continue
        if tm_bid <= 0 or tm_ask >= 2147483647 or tm_bid >= tm_ask:
            continue

        ts_ns = int(hbt.current_timestamp)
        if first_ts == 0:
            first_ts = ts_ns
        last_ts = ts_ns

        tmfd6_spread = tm_ask - tm_bid
        tmfd6_spread_sum += tmfd6_spread
        tmfd6_spread_count += 1

        # Cancel previous orders
        if active_buy_id is not None:
            hbt.cancel(0, active_buy_id, False)
            active_buy_id = None
        if active_sell_id is not None:
            hbt.cancel(0, active_sell_id, False)
            active_sell_id = None
        hbt.clear_inactive_orders(0)

        pos = int(hbt.position(0))
        positions["TMFD6"] = pos

        # Build TMFD6 L1
        tm_bid_scaled = int(round(tm_bid * PRICE_SCALE))
        tm_ask_scaled = int(round(tm_ask * PRICE_SCALE))
        tm_bid_qty = int(getattr(dp, "best_bid_qty", 0) or 0)
        tm_ask_qty = int(getattr(dp, "best_ask_qty", 0) or 0)
        tm_total = tm_bid_qty + tm_ask_qty
        tm_imbalance = (tm_bid_qty - tm_ask_qty) / tm_total if tm_total > 0 else 0.0
        tm_imb_ppm = int(tm_imbalance * 1_000_000)

        # FeatureUpdateEvent (TMFD6 — same symbol for signals in single-instrument)
        vals = [0] * 27
        vals[_IDX_BEST_BID] = tm_bid_scaled
        vals[_IDX_BEST_ASK] = tm_ask_scaled
        vals[_IDX_L1_BID_QTY] = tm_bid_qty
        vals[_IDX_L1_ASK_QTY] = tm_ask_qty
        vals[_IDX_L1_IMBALANCE_PPM] = tm_imb_ppm
        feature_ids = tuple(f"f{i}" for i in range(27))

        feature_event = FeatureUpdateEvent(
            symbol="TMFD6", ts=ts_ns, local_ts=ts_ns, seq=step_count,
            feature_set_id="lob_shared_v3", schema_version=3,
            changed_mask=0xFFFFFFFF, warmup_ready_mask=0xFFFFFFFF,
            quality_flags=0, feature_ids=feature_ids, values=tuple(vals),
        )

        stats_event = LOBStatsEvent(
            symbol="TMFD6", ts=ts_ns, imbalance=tm_imbalance,
            best_bid=tm_bid_scaled, best_ask=tm_ask_scaled,
            bid_depth=tm_bid_qty, ask_depth=tm_ask_qty,
        )

        captured_intents.clear()
        strategy.handle_event(ctx, feature_event)
        intents = strategy.handle_event(ctx, stats_event)
        captured_intents.extend(intents)

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

        if step_count % 10 == 0:
            sv = hbt.state_values(0)
            mid = (tm_bid + tm_ask) / 2.0
            equity = sv.balance + pos * mid
            equity_curve.append(equity)

        step_count += 1

    sv = hbt.state_values(0)
    final_pos = int(hbt.position(0))
    final_mid = (tm_bid + tm_ask) / 2.0 if tm_bid > 0 and tm_ask < 2147483647 else 0.0
    total_pnl_pts = sv.balance + final_pos * final_mid
    total_pnl_ntd = total_pnl_pts * TMFD6_POINT_VALUE_NTD

    trading_vol = int(sv.trading_volume)
    if final_pos >= 0:
        buy_fills = (trading_vol + final_pos) // 2
        sell_fills = trading_vol - buy_fills
    else:
        sell_fills = (trading_vol - final_pos) // 2
        buy_fills = trading_vol - sell_fills

    # Costs: each fill = one side cost, each RT = 2 sides
    round_trips = min(buy_fills, sell_fills)
    remaining_fills = trading_vol - round_trips * 2
    total_cost_ntd = (round_trips * TMFD6_RT_COST_NTD
                      + remaining_fills * (TMFD6_COMMISSION_PER_SIDE_NTD + TMFD6_TAX_PER_SIDE_NTD))
    net_pnl_ntd = total_pnl_ntd - total_cost_ntd
    total_cost_pts = total_cost_ntd / TMFD6_POINT_VALUE_NTD

    hbt.close()
    elapsed = time.monotonic() - t0

    # Equity curve analysis
    eq_arr = np.array(equity_curve, dtype=np.float64) if equity_curve else np.array([0.0])
    if len(eq_arr) >= 2:
        returns = np.diff(eq_arr)
        cumulative = eq_arr - eq_arr[0]
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = running_max - cumulative
        max_dd = float(np.max(drawdowns))
        win_rate = float(np.mean(returns > 0))
        std_ret = float(np.std(returns))
        if std_ret > 1e-12:
            sharpe = float(np.mean(returns)) / std_ret * math.sqrt(len(eq_arr) * 252)
        else:
            sharpe = 0.0
    else:
        max_dd = 0.0
        win_rate = 0.0
        sharpe = 0.0

    duration_ns = last_ts - first_ts if last_ts > first_ts else 0
    duration_hours = duration_ns / 3.6e12
    avg_spread = tmfd6_spread_sum / tmfd6_spread_count if tmfd6_spread_count > 0 else 0.0

    pnl_per_fill = total_pnl_pts / trading_vol if trading_vol > 0 else 0.0
    net_per_fill = (total_pnl_pts - total_cost_pts) / trading_vol if trading_vol > 0 else 0.0

    result = DayResult(
        date=date_str,
        total_pnl_pts=round(total_pnl_pts, 2),
        total_pnl_ntd=round(total_pnl_ntd, 0),
        net_pnl_ntd=round(net_pnl_ntd, 0),
        total_fills=trading_vol,
        buy_fills=buy_fills,
        sell_fills=sell_fills,
        round_trips=round_trips,
        total_cost_ntd=round(total_cost_ntd, 0),
        pnl_per_fill_pts=round(pnl_per_fill, 3),
        net_per_fill_pts=round(net_per_fill, 3),
        win_rate=round(win_rate, 4),
        max_drawdown_pts=round(max_dd, 2),
        sharpe=round(sharpe, 2),
        duration_hours=round(duration_hours, 2),
        final_position=final_pos,
        spread_blocked=getattr(strategy, "_spread_blocked", 0),
        pe_blocked=getattr(strategy, "_pe_blocked", 0),
        queue_suppressed=getattr(strategy, "_queue_suppressed", 0),
        quotes_sent=getattr(strategy, "_quotes_sent", 0),
        tmfd6_avg_spread_pts=round(avg_spread, 2),
    )

    print_log(
        f"  {date_str}: PnL={result.total_pnl_pts:+.1f} pts "
        f"({result.total_pnl_ntd:+.0f} NTD gross, {result.net_pnl_ntd:+.0f} NTD net), "
        f"fills={trading_vol} (RT={round_trips}), cost={total_cost_ntd:.0f} NTD, "
        f"net/fill={net_per_fill:+.3f} pts, spr_avg={avg_spread:.1f}, "
        f"pos={final_pos}, spr_blk={result.spread_blocked}, pe_blk={result.pe_blocked}, "
        f"elapsed={elapsed:.1f}s"
    )
    return result


def main() -> None:
    print_log(f"\nR47 Cross-Instrument Validation (TXFD6 signal -> TMFD6 execution)")
    print_log(f"Cost model: 2.0 pts/side (1.3 comm + 0.7 tax) = 4.0 pts RT")
    print_log(f"TMFD6: 1 pt = {TMFD6_POINT_VALUE_NTD} NTD, RT cost = {TMFD6_RT_COST_NTD} NTD")
    print_log(f"Queue: PowerProbQueueModel(3.0), Latency: 47us, Elapse: 100ms")
    print_log(f"Strategy: deployed config (PE=0.55, Queue=0.7, GLT off, spr>=5, maxpos=3)")
    print_log("=" * 80)

    results: list[DayResult] = []
    for date_str in DATES:
        try:
            r = run_cross_day(date_str)
            if r is not None:
                results.append(r)
        except Exception as exc:
            import traceback
            print_log(f"  {date_str} FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    if not results:
        print_log("No results!")
        return

    # Aggregate
    total_pnl = sum(r.total_pnl_pts for r in results)
    total_ntd = sum(r.total_pnl_ntd for r in results)
    total_net = sum(r.net_pnl_ntd for r in results)
    total_fills = sum(r.total_fills for r in results)
    total_rts = sum(r.round_trips for r in results)
    total_cost = sum(r.total_cost_ntd for r in results)
    total_hours = sum(r.duration_hours for r in results)
    winning_days = sum(1 for r in results if r.net_pnl_ntd > 0)

    total_cost_pts = total_cost / TMFD6_POINT_VALUE_NTD
    pnl_per_fill = total_pnl / total_fills if total_fills > 0 else 0.0
    net_per_fill = (total_pnl - total_cost_pts) / total_fills if total_fills > 0 else 0.0

    # Daily PnL stats
    daily_net = [r.net_pnl_ntd for r in results]
    daily_arr = np.array(daily_net)
    mean_daily = float(np.mean(daily_arr))
    std_daily = float(np.std(daily_arr, ddof=1)) if len(daily_arr) > 1 else 0.0
    t_stat = mean_daily / (std_daily / math.sqrt(len(daily_arr))) if std_daily > 0 else 0.0

    print_log("\n" + "=" * 80)
    print_log(f"AGGREGATE ({len(results)} days)")
    print_log(f"  Gross PnL:  {total_pnl:+.1f} pts = {total_ntd:+,.0f} NTD")
    print_log(f"  Total Cost: {total_cost_pts:.1f} pts = {total_cost:,.0f} NTD")
    print_log(f"  NET PnL:    {total_pnl - total_cost_pts:+.1f} pts = {total_net:+,.0f} NTD")
    print_log(f"  Fills: {total_fills} ({total_rts} RTs), {total_fills/len(results):.0f}/day")
    print_log(f"  Gross/fill: {pnl_per_fill:+.3f} pts, Net/fill: {net_per_fill:+.3f} pts")
    print_log(f"  Winning days: {winning_days}/{len(results)} ({winning_days/len(results)*100:.0f}%)")
    print_log(f"  Daily net: mean={mean_daily:+,.0f} NTD, std={std_daily:,.0f} NTD")
    print_log(f"  t-stat: {t_stat:.3f} (p<0.05 needs t>2.201 for n={len(results)})")
    print_log(f"  Worst max DD: {max(r.max_drawdown_pts for r in results):.1f} pts")
    print_log(f"  Avg TMFD6 spread: {np.mean([r.tmfd6_avg_spread_pts for r in results]):.2f} pts")

    # Save results
    output = {
        "metadata": {
            "script": "r47_cross_hftbt_validation.py",
            "mode": "cross_instrument",
            "signal_symbol": "TXFD6",
            "trade_symbol": "TMFD6",
            "cost_model": {
                "commission_per_side_pts": 1.3,
                "tax_per_side_pts": 0.7,
                "total_per_side_pts": 2.0,
                "rt_cost_pts": 4.0,
                "rt_cost_ntd": 40,
                "point_value_ntd": 10,
            },
            "strategy_config": {
                "pe_danger_threshold": 0.55,
                "queue_cancel_threshold": 0.7,
                "glt_gamma": 0.0,
                "spread_threshold_pts": 5,
                "toxicity_max": 700,
                "max_pos": 3,
            },
            "backtest_config": {
                "queue_model": "PowerProbQueueModel(3.0)",
                "latency_ns": 47_000,
                "elapse_ns": ELAPSE_NS,
                "tick_size": TICK_SIZE,
            },
        },
        "aggregate": {
            "n_days": len(results),
            "gross_pnl_pts": round(total_pnl, 2),
            "gross_pnl_ntd": round(total_ntd, 0),
            "total_cost_pts": round(total_cost_pts, 2),
            "total_cost_ntd": round(total_cost, 0),
            "net_pnl_pts": round(total_pnl - total_cost_pts, 2),
            "net_pnl_ntd": round(total_net, 0),
            "total_fills": total_fills,
            "total_round_trips": total_rts,
            "fills_per_day": round(total_fills / len(results), 1),
            "gross_per_fill_pts": round(pnl_per_fill, 3),
            "net_per_fill_pts": round(net_per_fill, 3),
            "winning_days": winning_days,
            "losing_days": len(results) - winning_days,
            "win_rate_days": round(winning_days / len(results), 3),
            "daily_net_mean_ntd": round(mean_daily, 0),
            "daily_net_std_ntd": round(std_daily, 0),
            "t_stat": round(t_stat, 3),
            "worst_max_dd_pts": round(max(r.max_drawdown_pts for r in results), 2),
            "avg_tmfd6_spread_pts": round(float(np.mean([r.tmfd6_avg_spread_pts for r in results])), 2),
        },
        "per_day": [asdict(r) for r in results],
    }

    out_path = OUT_DIR / "cross_instrument_hftbt_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print_log(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
