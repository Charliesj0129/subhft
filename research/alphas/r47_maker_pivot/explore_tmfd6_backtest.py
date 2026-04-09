"""R47 Maker — TMFD6 Backtest on Actual Trade Instrument.

Tests the deployed R47 config on TMFD6 L2 data (the actual trade instrument)
with realistic latency (36ms). This resolves Challenger's C1 + C2 concerns.

Configs tested:
  1) deployed_36ms: spread>=5, max_pos=1, signals off, 36ms latency (production)
  2) deployed_spr4: spread>=4, max_pos=1, signals off, 36ms latency (if comm < 1.0)
  3) deployed_spr3: spread>=3, max_pos=1, signals off, 36ms latency (aggressive)
  4) research_36ms: spread>=1, max_pos=3, signals on, 36ms latency (comparison)

Usage:
    uv run python research/alphas/r47_maker_pivot/explore_tmfd6_backtest.py
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
    pe_danger_threshold: float
    queue_cancel_threshold: float
    mfg_skew_z_threshold: float
    spread_threshold_pts: int
    toxicity_max: int
    max_pos: int


@dataclass
class DayResult:
    config_name: str
    date: str
    total_pnl_pts: float
    total_fills: int
    max_drawdown: float
    final_position: int
    quotes_submitted: int
    avg_spread_pts: float
    pct_time_spread_gte_threshold: float


CONFIGS = [
    # Production config (FIXED): spread>=5, max_pos=1, all signals off
    # queue_cancel_threshold=1.0 to DISABLE (0.0 was a bug — it suppresses ALL quotes)
    RunConfig(
        name="deployed_spr5_36ms",
        latency_ns=36_000_000,
        pe_danger_threshold=0.0,
        queue_cancel_threshold=1.0,  # 1.0 = disabled (p_depl never > 1.0)
        mfg_skew_z_threshold=100.0,
        spread_threshold_pts=5,
        toxicity_max=9999,
        max_pos=1,
    ),
    # Sweet spot: spread>=4, max_pos=1
    RunConfig(
        name="deployed_spr4_36ms",
        latency_ns=36_000_000,
        pe_danger_threshold=0.0,
        queue_cancel_threshold=1.0,
        mfg_skew_z_threshold=100.0,
        spread_threshold_pts=4,
        toxicity_max=9999,
        max_pos=1,
    ),
    # Aggressive: spread>=3
    RunConfig(
        name="deployed_spr3_36ms",
        latency_ns=36_000_000,
        pe_danger_threshold=0.0,
        queue_cancel_threshold=1.0,
        mfg_skew_z_threshold=100.0,
        spread_threshold_pts=3,
        toxicity_max=9999,
        max_pos=1,
    ),
    # Research baseline: spread>=1, all signals on, max_pos=3
    RunConfig(
        name="research_spr1_36ms",
        latency_ns=36_000_000,
        pe_danger_threshold=0.55,
        queue_cancel_threshold=0.7,
        mfg_skew_z_threshold=2.0,
        spread_threshold_pts=1,
        toxicity_max=700,
        max_pos=3,
    ),
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
        .power_prob_queue_model(3.0)
        .tick_size(TICK_SIZE)
        .lot_size(LOT_SIZE)
        .partial_fill_exchange()
    )
    hbt = HashMapMarketDepthBacktest([asset])

    positions = {SYMBOL: 0}
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
    quotes_submitted = 0

    # Spread tracking
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

        positions[SYMBOL] = cur_pos

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
                quotes_submitted += 1
            elif intent.side == Side.SELL and not sell_submitted:
                order_id += 1
                hbt.submit_sell_order(0, order_id, price_rounded, float(intent.qty), GTC, LIMIT, False)
                active_sell_id = order_id
                sell_submitted = True
                quotes_submitted += 1

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
        config_name=cfg.name,
        date=date_str,
        total_pnl_pts=round(total_pnl, 2),
        total_fills=fill_count,
        max_drawdown=round(max_dd, 2),
        final_position=prev_pos,
        quotes_submitted=quotes_submitted,
        avg_spread_pts=round(avg_spread, 2),
        pct_time_spread_gte_threshold=round(pct_above, 2),
    )


def main() -> None:
    if not DATA_FILES:
        print(f"ERROR: No TMFD6 L2 data files found in {DATA_DIR}")
        sys.exit(1)

    n_days = len(DATA_FILES)
    n_configs = len(CONFIGS)
    print(f"\nR47 Maker — TMFD6 Backtest (Actual Trade Instrument)")
    print(f"Data: {n_days} days of TMFD6 L2")
    print(f"Configs: {n_configs}")
    print(f"Total runs: {n_days * n_configs}")
    print("=" * 100)

    results: dict[str, list[DayResult]] = {c.name: [] for c in CONFIGS}
    t_total = time.monotonic()

    for cfg in CONFIGS:
        print(f"\n{'='*100}")
        print(f"CONFIG: {cfg.name} (latency={cfg.latency_ns/1e6:.0f}ms, spr>={cfg.spread_threshold_pts}, max_pos={cfg.max_pos})")
        print(f"{'='*100}")

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
            results[cfg.name].append(result)
            fill_rate = result.total_fills / result.quotes_submitted * 100 if result.quotes_submitted > 0 else 0
            print(
                f"  {result.date}: PnL={result.total_pnl_pts:>+8.1f} pts, "
                f"fills={result.total_fills:>4}, quotes={result.quotes_submitted:>5}, "
                f"fill%={fill_rate:>5.1f}%, maxDD={result.max_drawdown:>7.1f}, "
                f"avg_spr={result.avg_spread_pts:>5.1f}, "
                f"spr>={cfg.spread_threshold_pts}:{result.pct_time_spread_gte_threshold:>5.1f}%  "
                f"({elapsed:.1f}s)"
            )

    total_elapsed = time.monotonic() - t_total

    # ── Aggregate ────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("AGGREGATE COMPARISON — TMFD6 (actual trade instrument)")
    print("=" * 100)

    summary = {}
    for cfg_name, day_results in results.items():
        if not day_results:
            continue
        total_pnl = sum(r.total_pnl_pts for r in day_results)
        total_fills = sum(r.total_fills for r in day_results)
        total_quotes = sum(r.quotes_submitted for r in day_results)
        worst_dd = max(r.max_drawdown for r in day_results)
        winning = sum(1 for r in day_results if r.total_pnl_pts > 0)
        worst_day = min(r.total_pnl_pts for r in day_results)
        best_day = max(r.total_pnl_pts for r in day_results)
        fill_rate = total_fills / total_quotes * 100 if total_quotes > 0 else 0
        avg_spread = sum(r.avg_spread_pts for r in day_results) / len(day_results)
        avg_pct_above = sum(r.pct_time_spread_gte_threshold for r in day_results) / len(day_results)

        # Daily PnL stats
        daily_pnls = [r.total_pnl_pts for r in day_results]
        mean_daily = np.mean(daily_pnls)
        std_daily = np.std(daily_pnls, ddof=1) if len(daily_pnls) > 1 else 0
        t_stat = mean_daily / (std_daily / math.sqrt(len(daily_pnls))) if std_daily > 0 else 0

        summary[cfg_name] = {
            "total_pnl_pts": round(total_pnl, 2),
            "total_pnl_ntd": round(total_pnl * POINT_VALUE_NTD, 0),
            "avg_pnl_per_day": round(float(mean_daily), 2),
            "std_pnl_per_day": round(float(std_daily), 2),
            "t_statistic": round(float(t_stat), 3),
            "total_fills": total_fills,
            "total_quotes": total_quotes,
            "fill_rate_pct": round(fill_rate, 3),
            "pnl_per_fill": round(total_pnl / total_fills, 3) if total_fills > 0 else 0,
            "winning_days": winning,
            "losing_days": len(day_results) - winning,
            "n_days": len(day_results),
            "win_rate": round(winning / len(day_results), 3),
            "worst_day": round(worst_day, 2),
            "best_day": round(best_day, 2),
            "worst_max_dd": round(worst_dd, 2),
            "avg_spread_pts": round(avg_spread, 2),
            "avg_pct_time_above_threshold": round(avg_pct_above, 2),
            "daily_pnl": [round(r.total_pnl_pts, 2) for r in day_results],
        }

        print(f"\n  {cfg_name}:")
        print(f"    Total PnL:    {total_pnl:>+10.1f} pts ({total_pnl * POINT_VALUE_NTD:>+12.0f} NTD)")
        print(f"    Avg PnL/day:  {mean_daily:>+10.1f} pts (std={std_daily:.1f}, t={t_stat:.3f})")
        print(f"    Win rate:     {winning}/{len(day_results)} ({winning/len(day_results):.0%})")
        print(f"    Total fills:  {total_fills:>10}")
        print(f"    Total quotes: {total_quotes:>10}")
        print(f"    Fill rate:    {fill_rate:>10.2f}%")
        if total_fills > 0:
            print(f"    PnL/fill:     {total_pnl / total_fills:>+10.3f} pts")
        else:
            print(f"    PnL/fill:     N/A (0 fills)")
        print(f"    Worst day:    {worst_day:>+10.1f} pts")
        print(f"    Best day:     {best_day:>+10.1f} pts")
        print(f"    Worst DD:     {worst_dd:>10.1f} pts")
        print(f"    Avg spread:   {avg_spread:>10.2f} pts")
        print(f"    % time spr>threshold: {avg_pct_above:>6.1f}%")

    # ── Spread distribution ──────────────────────────────────────────
    print("\n" + "=" * 100)
    print("SPREAD AVAILABILITY (% of time TMFD6 spread >= threshold)")
    print("=" * 100)
    for cfg_name, day_results in results.items():
        if not day_results:
            continue
        print(f"\n  {cfg_name}:")
        for r in day_results:
            bar = "#" * int(r.pct_time_spread_gte_threshold / 2)
            print(f"    {r.date}: {r.pct_time_spread_gte_threshold:>6.1f}% {bar}")

    print(f"\nTotal elapsed: {total_elapsed:.0f}s")

    # ── Save JSON ────────────────────────────────────────────────────
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "metadata": {
            "script": "explore_tmfd6_backtest.py",
            "instrument": "TMFD6",
            "n_days": n_days,
            "data_source": "ClickHouse hft.market_data via ch_batch_export.py",
            "configs": {c.name: {
                "latency_ms": c.latency_ns / 1e6,
                "spread_pts": c.spread_threshold_pts,
                "max_pos": c.max_pos,
                "signals_enabled": c.pe_danger_threshold > 0,
            } for c in CONFIGS},
        },
        "summary": summary,
        "per_day": {
            cfg_name: [
                {
                    "date": r.date,
                    "total_pnl_pts": r.total_pnl_pts,
                    "total_fills": r.total_fills,
                    "quotes_submitted": r.quotes_submitted,
                    "max_drawdown": r.max_drawdown,
                    "final_position": r.final_position,
                    "avg_spread_pts": r.avg_spread_pts,
                    "pct_time_spread_gte_threshold": r.pct_time_spread_gte_threshold,
                }
                for r in day_results
            ]
            for cfg_name, day_results in results.items()
        },
    }

    out_path = OUT_DIR / "tmfd6_backtest.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
