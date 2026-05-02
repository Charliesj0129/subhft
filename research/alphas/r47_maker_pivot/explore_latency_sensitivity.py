"""R47 Maker — Latency Sensitivity Analysis.

Runs the SAME strategy on the SAME data with different order latency assumptions:
  A) 47us  (original backtest — internal system latency)
  B) 36ms  (Shioaji P95 broker RTT — realistic)

Two config variants:
  1) Research params (all signals active, spread=1, max_pos=3)
  2) Deployed params (all signals off, spread=5, max_pos=1, cross-instrument disabled for apples-to-apples)

Usage:
    uv run python research/alphas/r47_maker_pivot/explore_latency_sensitivity.py
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

DATA_DIR = _REPO_ROOT / "research" / "data" / "raw" / "txfd6"
DATA_FILES = sorted(DATA_DIR.glob("TXFD6_2026-0*_l2.hftbt.npz"))
OUT_DIR = _REPO_ROOT / "outputs" / "team_artifacts" / "alpha-research" / "R47_maker_pivot"

_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9
_IDX_L1_IMBALANCE_PPM = 10


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


CONFIGS = [
    # A1: Original backtest (47us, research params)
    RunConfig(
        name="research_47us",
        latency_ns=47_000,
        pe_danger_threshold=0.55,
        queue_cancel_threshold=0.7,
        mfg_skew_z_threshold=2.0,
        spread_threshold_pts=1,
        toxicity_max=700,
        max_pos=3,
    ),
    # A2: Realistic latency (36ms, research params)
    RunConfig(
        name="research_36ms",
        latency_ns=36_000_000,
        pe_danger_threshold=0.55,
        queue_cancel_threshold=0.7,
        mfg_skew_z_threshold=2.0,
        spread_threshold_pts=1,
        toxicity_max=700,
        max_pos=3,
    ),
    # B1: Deployed config with 47us (baseline for deployed)
    RunConfig(
        name="deployed_47us",
        latency_ns=47_000,
        pe_danger_threshold=0.0,
        queue_cancel_threshold=0.0,
        mfg_skew_z_threshold=100.0,
        spread_threshold_pts=5,
        toxicity_max=9999,
        max_pos=1,
    ),
    # B2: Deployed config with 36ms (production-realistic)
    RunConfig(
        name="deployed_36ms",
        latency_ns=36_000_000,
        pe_danger_threshold=0.0,
        queue_cancel_threshold=0.0,
        mfg_skew_z_threshold=100.0,
        spread_threshold_pts=5,
        toxicity_max=9999,
        max_pos=1,
    ),
]


def run_one_day(data_path: Path, cfg: RunConfig) -> DayResult:
    from hft_platform.contracts.strategy import IntentType, Side, OrderIntent
    from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
    from hft_platform.strategy.base import StrategyContext
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    date_str = data_path.stem.replace("TXFD6_", "").replace("_l2.hftbt", "")

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
    quotes_submitted = 0

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

    return DayResult(
        config_name=cfg.name,
        date=date_str,
        total_pnl_pts=round(total_pnl, 2),
        total_fills=fill_count,
        max_drawdown=round(max_dd, 2),
        final_position=prev_pos,
        quotes_submitted=quotes_submitted,
    )


def main() -> None:
    if not DATA_FILES:
        print(f"ERROR: No data files found in {DATA_DIR}")
        sys.exit(1)

    n_days = len(DATA_FILES)
    n_configs = len(CONFIGS)
    print(f"\nR47 Maker — Latency Sensitivity Analysis")
    print(f"Data: {n_days} days of TXFD6 L2")
    print(f"Configs: {n_configs} ({', '.join(c.name for c in CONFIGS)})")
    print(f"Total runs: {n_days * n_configs}")
    print("=" * 80)

    results: dict[str, list[DayResult]] = {c.name: [] for c in CONFIGS}
    t_total = time.monotonic()

    for cfg in CONFIGS:
        print(f"\n{'='*80}")
        print(f"CONFIG: {cfg.name} (latency={cfg.latency_ns/1e6:.3f}ms, spr>={cfg.spread_threshold_pts}, max_pos={cfg.max_pos})")
        print(f"{'='*80}")

        for data_path in DATA_FILES:
            t0 = time.monotonic()
            result = run_one_day(data_path, cfg)
            elapsed = time.monotonic() - t0
            results[cfg.name].append(result)
            fill_rate = result.total_fills / result.quotes_submitted * 100 if result.quotes_submitted > 0 else 0
            print(
                f"  {result.date}: PnL={result.total_pnl_pts:>+8.1f} pts, "
                f"fills={result.total_fills:>4}, quotes={result.quotes_submitted:>5}, "
                f"fill_rate={fill_rate:>5.2f}%, maxDD={result.max_drawdown:>7.1f}, "
                f"final_pos={result.final_position:>+2}  ({elapsed:.1f}s)"
            )

    total_elapsed = time.monotonic() - t_total

    # ── Comparison Tables ────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("AGGREGATE COMPARISON")
    print("=" * 80)

    summary = {}
    for cfg_name, day_results in results.items():
        total_pnl = sum(r.total_pnl_pts for r in day_results)
        total_fills = sum(r.total_fills for r in day_results)
        total_quotes = sum(r.quotes_submitted for r in day_results)
        worst_dd = max(r.max_drawdown for r in day_results)
        winning = sum(1 for r in day_results if r.total_pnl_pts > 0)
        worst_day = min(r.total_pnl_pts for r in day_results)
        best_day = max(r.total_pnl_pts for r in day_results)
        fill_rate = total_fills / total_quotes * 100 if total_quotes > 0 else 0

        summary[cfg_name] = {
            "total_pnl_pts": round(total_pnl, 2),
            "total_pnl_ntd": round(total_pnl * POINT_VALUE_NTD, 0),
            "avg_pnl_per_day": round(total_pnl / len(day_results), 2),
            "total_fills": total_fills,
            "total_quotes": total_quotes,
            "fill_rate_pct": round(fill_rate, 3),
            "pnl_per_fill": round(total_pnl / total_fills, 3) if total_fills > 0 else 0,
            "winning_days": winning,
            "losing_days": len(day_results) - winning,
            "win_rate": round(winning / len(day_results), 3),
            "worst_day": round(worst_day, 2),
            "best_day": round(best_day, 2),
            "worst_max_dd": round(worst_dd, 2),
        }

        print(f"\n  {cfg_name}:")
        print(f"    Total PnL:    {total_pnl:>+10.1f} pts ({total_pnl * POINT_VALUE_NTD:>+12.0f} NTD)")
        print(f"    Avg PnL/day:  {total_pnl / len(day_results):>+10.1f} pts")
        print(f"    Win rate:     {winning}/{len(day_results)} ({winning/len(day_results):.0%})")
        print(f"    Total fills:  {total_fills:>10}")
        print(f"    Total quotes: {total_quotes:>10}")
        print(f"    Fill rate:    {fill_rate:>10.2f}%")
        print(f"    PnL/fill:     {total_pnl / total_fills:>+10.3f} pts" if total_fills > 0 else "    PnL/fill:     N/A")
        print(f"    Worst day:    {worst_day:>+10.1f} pts")
        print(f"    Best day:     {best_day:>+10.1f} pts")
        print(f"    Worst DD:     {worst_dd:>10.1f} pts")

    # ── Latency Delta ────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("LATENCY DELTA (47us -> 36ms)")
    print("=" * 80)

    for prefix in ["research", "deployed"]:
        fast = f"{prefix}_47us"
        slow = f"{prefix}_36ms"
        if fast in summary and slow in summary:
            pnl_fast = summary[fast]["total_pnl_pts"]
            pnl_slow = summary[slow]["total_pnl_pts"]
            fills_fast = summary[fast]["total_fills"]
            fills_slow = summary[slow]["total_fills"]
            delta_pnl = pnl_slow - pnl_fast
            delta_pct = delta_pnl / abs(pnl_fast) * 100 if pnl_fast != 0 else float("inf")
            delta_fills = fills_slow - fills_fast
            delta_fills_pct = delta_fills / fills_fast * 100 if fills_fast > 0 else 0

            print(f"\n  {prefix.upper()} config:")
            print(f"    PnL @ 47us:   {pnl_fast:>+10.1f} pts")
            print(f"    PnL @ 36ms:   {pnl_slow:>+10.1f} pts")
            print(f"    Delta:        {delta_pnl:>+10.1f} pts ({delta_pct:>+.1f}%)")
            print(f"    Fills @ 47us: {fills_fast:>10}")
            print(f"    Fills @ 36ms: {fills_slow:>10}")
            print(f"    Fill delta:   {delta_fills:>+10} ({delta_fills_pct:>+.1f}%)")

    # ── Per-Day Delta ────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("PER-DAY LATENCY DELTA (research config)")
    print("=" * 80)
    print(f"{'Date':<12} {'47us PnL':>10} {'36ms PnL':>10} {'Delta':>10} {'47us Fills':>10} {'36ms Fills':>10}")
    print("-" * 62)

    for i in range(n_days):
        r_fast = results["research_47us"][i]
        r_slow = results["research_36ms"][i]
        delta = r_slow.total_pnl_pts - r_fast.total_pnl_pts
        print(
            f"{r_fast.date:<12} {r_fast.total_pnl_pts:>+10.1f} {r_slow.total_pnl_pts:>+10.1f} "
            f"{delta:>+10.1f} {r_fast.total_fills:>10} {r_slow.total_fills:>10}"
        )

    if "deployed_47us" in results and "deployed_36ms" in results:
        print(f"\n{'Date':<12} {'47us PnL':>10} {'36ms PnL':>10} {'Delta':>10} {'47us Fills':>10} {'36ms Fills':>10}")
        print("-" * 62)
        print("(deployed config)")
        for i in range(n_days):
            r_fast = results["deployed_47us"][i]
            r_slow = results["deployed_36ms"][i]
            delta = r_slow.total_pnl_pts - r_fast.total_pnl_pts
            print(
                f"{r_fast.date:<12} {r_fast.total_pnl_pts:>+10.1f} {r_slow.total_pnl_pts:>+10.1f} "
                f"{delta:>+10.1f} {r_fast.total_fills:>10} {r_slow.total_fills:>10}"
            )

    print(f"\nTotal elapsed: {total_elapsed:.0f}s")

    # ── Save JSON ────────────────────────────────────────────────────
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "metadata": {
            "script": "explore_latency_sensitivity.py",
            "n_days": n_days,
            "configs": {c.name: {
                "latency_ns": c.latency_ns,
                "latency_ms": c.latency_ns / 1e6,
                "pe_danger": c.pe_danger_threshold,
                "queue_cancel": c.queue_cancel_threshold,
                "mfg_z": c.mfg_skew_z_threshold,
                "spread_pts": c.spread_threshold_pts,
                "tox_max": c.toxicity_max,
                "max_pos": c.max_pos,
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
                }
                for r in day_results
            ]
            for cfg_name, day_results in results.items()
        },
        "latency_delta": {},
    }

    for prefix in ["research", "deployed"]:
        fast = f"{prefix}_47us"
        slow = f"{prefix}_36ms"
        if fast in summary and slow in summary:
            output["latency_delta"][prefix] = {
                "pnl_47us": summary[fast]["total_pnl_pts"],
                "pnl_36ms": summary[slow]["total_pnl_pts"],
                "pnl_delta": round(summary[slow]["total_pnl_pts"] - summary[fast]["total_pnl_pts"], 2),
                "pnl_delta_pct": round(
                    (summary[slow]["total_pnl_pts"] - summary[fast]["total_pnl_pts"])
                    / abs(summary[fast]["total_pnl_pts"]) * 100, 1
                ) if summary[fast]["total_pnl_pts"] != 0 else None,
                "fills_47us": summary[fast]["total_fills"],
                "fills_36ms": summary[slow]["total_fills"],
                "fills_delta_pct": round(
                    (summary[slow]["total_fills"] - summary[fast]["total_fills"])
                    / summary[fast]["total_fills"] * 100, 1
                ) if summary[fast]["total_fills"] > 0 else None,
            }

    out_path = OUT_DIR / "latency_sensitivity.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
