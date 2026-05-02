"""C3b-B Stale Quote Suppression — A/B Backtest.

Runs 12-day A/B comparison:
  A (baseline): R47 production strategy, no stale suppression
  B (treatment): R47 + stale quote suppression (skip re-quoting at same price)

Primary metric: PnL-per-fill (must improve, not just total PnL).

Usage:
    uv run python research/alphas/r47_maker_pivot/c3b_b_stale_suppression_ab.py
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

from hft_platform.contracts.strategy import IntentType, Side, OrderIntent
from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
from hft_platform.strategy.base import StrategyContext
from hft_platform.strategies.r47_maker import R47MakerStrategy

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


class R47StaleSuppressionStrategy(R47MakerStrategy):
    """R47 with stale quote suppression.

    When stale_suppression_enabled=True, skip re-quoting at the same price
    on each side. Reset stale price tracking when any gate suppresses.
    """

    def __init__(self, *, stale_suppression_enabled: bool = False, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._stale_suppression_enabled = stale_suppression_enabled
        self._last_bid_price: int = 0
        self._last_ask_price: int = 0
        self._stale_suppressed: int = 0

    def on_stats(self, event: LOBStatsEvent) -> None:
        """Override to add stale suppression and gate-reset tracking."""
        symbol = event.symbol
        self._stats_count += 1

        # Validity guard
        if (event.mid_price_x2 is None or event.spread_scaled is None
                or event.mid_price_x2 <= 0 or event.spread_scaled <= 0):
            return

        # Spread gate
        if event.spread_scaled < self._spread_thresh_scaled:
            self._spread_blocked += 1
            self._last_bid_price = 0
            self._last_ask_price = 0
            return

        # Toxicity gate
        features = self._feature_cache.get(symbol)
        if features and len(features) > 21:  # _IDX_TOXICITY_EMA50_X1000
            toxicity = int(features[21])
            if toxicity > self._toxicity_max:
                self._toxicity_blocked += 1
                self._last_bid_price = 0
                self._last_ask_price = 0
                return

        # D1: PE Regime Gate
        pe = self._get_pe(symbol)
        if pe.warmed_up:
            h = pe.h
            if h < self._pe_danger:
                self._pe_blocked += 1
                self._last_bid_price = 0
                self._last_ask_price = 0
                return

        # Generate quotes with stale suppression
        self._generate_quotes_stale(symbol, event, pe)

    def _generate_quotes_stale(self, symbol: str, event: LOBStatsEvent, pe: object) -> None:
        """Quote generation with optional stale suppression."""
        mfg = self._get_mfg(symbol)

        # MFG widening (same as parent)
        mfg_widen_bid = 0
        mfg_widen_ask = 0
        if mfg.warmed_up and mfg.capitulation_z > self._mfg_skew_z_thresh:
            tick_size = max(1, event.spread_scaled * 50 // 100)
            skew_mult = min(3, int(mfg.capitulation_z - self._mfg_skew_z_thresh + 1))
            widen_amount = tick_size * skew_mult
            if mfg.flow_direction > 0:
                mfg_widen_ask = widen_amount
            elif mfg.flow_direction < 0:
                mfg_widen_bid = widen_amount
            self._mfg_skewed += 1

        mid_price_x2 = event.mid_price_x2
        spread_scaled = event.spread_scaled
        exec_sym = self._exec_symbol(symbol)
        pos = self.position(exec_sym)

        imbalance_adj = int(event.imbalance * spread_scaled * 20 * 2 // 100)
        micro_price_x2 = mid_price_x2 + imbalance_adj

        tick_size_scaled = max(1, spread_scaled * 50 // 100)
        skew_x2 = -(pos * tick_size_scaled * 2) // 5
        fair_value_x2 = micro_price_x2 + skew_x2

        half_spread_scaled = max(1, spread_scaled // 2)
        pe_width_mult = 2 if (hasattr(pe, 'warmed_up') and pe.warmed_up and pe.h < 0.70) else 1
        base_width = max(tick_size_scaled, half_spread_scaled) * pe_width_mult

        bid_width = base_width + mfg_widen_bid
        ask_width = base_width + mfg_widen_ask

        bid_price_scaled = (fair_value_x2 - bid_width * 2) // 2
        ask_price_scaled = (fair_value_x2 + ask_width * 2) // 2

        max_pos = self._max_pos

        # D2 suppression check
        suppress_bid = self._suppress_bid
        suppress_ask = self._suppress_ask

        # Stale suppression: skip if price unchanged since last quote
        if self._stale_suppression_enabled:
            if not suppress_bid and pos < max_pos:
                if bid_price_scaled == self._last_bid_price:
                    self._stale_suppressed += 1
                    suppress_bid = True
            if not suppress_ask and pos > -max_pos:
                if ask_price_scaled == self._last_ask_price:
                    self._stale_suppressed += 1
                    suppress_ask = True

        if pos < max_pos and not suppress_bid:
            self.buy(exec_sym, bid_price_scaled, 1)
            self._last_bid_price = bid_price_scaled
        if pos > -max_pos and not suppress_ask:
            self.sell(exec_sym, ask_price_scaled, 1)
            self._last_ask_price = ask_price_scaled

        self._quotes_sent += 1


@dataclass
class DayResult:
    date: str
    mode: str  # "baseline" or "treatment"
    total_pnl_pts: float
    total_fills: int
    max_drawdown: float
    pe_blocked: int
    queue_suppressed: int
    stale_suppressed: int
    quotes_sent: int
    pnl_per_fill: float


def run_one_day(data_path: Path, stale_suppression: bool) -> DayResult:
    """Run one day with or without stale suppression."""
    date_str = data_path.stem.replace("TXFD6_", "").replace("_l2.hftbt", "")
    mode = "treatment" if stale_suppression else "baseline"

    strategy = R47StaleSuppressionStrategy(
        stale_suppression_enabled=stale_suppression,
        strategy_id="r47_maker",
        pe_safe_threshold=0.85,
        pe_danger_threshold=0.55,
        pe_window=100,
        queue_cancel_threshold=0.7,
        mfg_skew_z_threshold=2.0,
        spread_threshold_pts=1,
        toxicity_max=700,
        max_pos=3,
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

    pnl_per_fill = total_pnl / fill_count if fill_count > 0 else 0.0

    return DayResult(
        date=date_str,
        mode=mode,
        total_pnl_pts=round(total_pnl, 2),
        total_fills=fill_count,
        max_drawdown=round(max_dd, 2),
        pe_blocked=strategy._pe_blocked,
        queue_suppressed=strategy._queue_suppressed,
        stale_suppressed=strategy._stale_suppressed,
        quotes_sent=strategy._quotes_sent,
        pnl_per_fill=round(pnl_per_fill, 4),
    )


def main() -> None:
    if not DATA_FILES:
        print(f"ERROR: No data files found in {DATA_DIR}")
        sys.exit(1)

    print(f"\nC3b-B Stale Quote Suppression — A/B Backtest")
    print(f"Data: {len(DATA_FILES)} days of TXFD6 L2")
    print(f"Queue model: PowerProbQueueModel(3.0)")
    print("=" * 90)

    # Step 1: Run day 1 baseline to validate
    print("\n--- Step 1: Baseline validation on day 1 ---")
    t0 = time.monotonic()
    day1_result = run_one_day(DATA_FILES[0], stale_suppression=False)
    elapsed = time.monotonic() - t0
    print(
        f"  {day1_result.date}: PnL={day1_result.total_pnl_pts:+.1f}, "
        f"fills={day1_result.total_fills}, pe_blocked={day1_result.pe_blocked}, "
        f"queue_suppressed={day1_result.queue_suppressed} ({elapsed:.1f}s)"
    )
    print(f"  Reference: corrected_baseline day 1 = +685.5 pts, 193 fills, pe_blocked=432,966")

    # Step 2: Full 12-day A/B
    print("\n--- Step 2: Full 12-day A/B comparison ---")
    baseline_results: list[DayResult] = []
    treatment_results: list[DayResult] = []

    for data_path in DATA_FILES:
        date_str = data_path.stem.replace("TXFD6_", "").replace("_l2.hftbt", "")

        # Baseline
        t0 = time.monotonic()
        base = run_one_day(data_path, stale_suppression=False)
        e_base = time.monotonic() - t0
        baseline_results.append(base)

        # Treatment
        t0 = time.monotonic()
        treat = run_one_day(data_path, stale_suppression=True)
        e_treat = time.monotonic() - t0
        treatment_results.append(treat)

        delta_pnl = treat.total_pnl_pts - base.total_pnl_pts
        delta_ppf = treat.pnl_per_fill - base.pnl_per_fill
        print(
            f"  {date_str}: base={base.total_pnl_pts:>+8.1f} ({base.total_fills} fills, "
            f"ppf={base.pnl_per_fill:+.3f}) | treat={treat.total_pnl_pts:>+8.1f} "
            f"({treat.total_fills} fills, ppf={treat.pnl_per_fill:+.3f}) | "
            f"delta={delta_pnl:+.1f} dppf={delta_ppf:+.4f} stale={treat.stale_suppressed} "
            f"({e_base:.0f}s+{e_treat:.0f}s)"
        )

    # Step 3: Summary
    print("\n" + "=" * 90)
    print("SUMMARY")
    print("=" * 90)

    base_total_pnl = sum(r.total_pnl_pts for r in baseline_results)
    treat_total_pnl = sum(r.total_pnl_pts for r in treatment_results)
    base_total_fills = sum(r.total_fills for r in baseline_results)
    treat_total_fills = sum(r.total_fills for r in treatment_results)
    base_total_ppf = base_total_pnl / base_total_fills if base_total_fills > 0 else 0
    treat_total_ppf = treat_total_pnl / treat_total_fills if treat_total_fills > 0 else 0
    total_stale = sum(r.stale_suppressed for r in treatment_results)

    base_wins = sum(1 for r in baseline_results if r.total_pnl_pts > 0)
    treat_wins = sum(1 for r in treatment_results if r.total_pnl_pts > 0)

    print(f"\n{'Metric':<25} {'Baseline':>12} {'Treatment':>12} {'Delta':>12}")
    print("-" * 65)
    print(f"{'Total PnL (pts)':<25} {base_total_pnl:>+12.1f} {treat_total_pnl:>+12.1f} {treat_total_pnl - base_total_pnl:>+12.1f}")
    print(f"{'Total Fills':<25} {base_total_fills:>12d} {treat_total_fills:>12d} {treat_total_fills - base_total_fills:>12d}")
    print(f"{'PnL per Fill (pts)':<25} {base_total_ppf:>+12.4f} {treat_total_ppf:>+12.4f} {treat_total_ppf - base_total_ppf:>+12.4f}")
    print(f"{'Winning Days':<25} {base_wins:>12d} {treat_wins:>12d} {treat_wins - base_wins:>+12d}")
    print(f"{'Stale Suppressed':<25} {'N/A':>12} {total_stale:>12d}")

    print(f"\n{'Per-Day PnL Comparison':}")
    print(f"{'Date':<12} {'Base PnL':>10} {'Treat PnL':>10} {'Delta':>8} {'Base PPF':>10} {'Treat PPF':>10} {'dPPF':>8} {'Stale':>8}")
    print("-" * 78)
    for b, t in zip(baseline_results, treatment_results):
        print(
            f"{b.date:<12} {b.total_pnl_pts:>+10.1f} {t.total_pnl_pts:>+10.1f} "
            f"{t.total_pnl_pts - b.total_pnl_pts:>+8.1f} {b.pnl_per_fill:>+10.4f} "
            f"{t.pnl_per_fill:>+10.4f} {t.pnl_per_fill - b.pnl_per_fill:>+8.4f} "
            f"{t.stale_suppressed:>8d}"
        )

    # Step 4: Save results
    output = {
        "metadata": {
            "script": "c3b_b_stale_suppression_ab.py",
            "n_days": len(DATA_FILES),
            "dates": [r.date for r in baseline_results],
            "config": {
                "queue_model": "PowerProbQueueModel(3.0)",
                "latency_ns": 47_000,
                "elapse_ns": ELAPSE_NS,
                "max_pos": 3,
                "pe_danger_threshold": 0.55,
                "queue_cancel_threshold": 0.7,
                "spread_threshold_pts": 1,
                "toxicity_max": 700,
            },
        },
        "summary": {
            "baseline": {
                "total_pnl_pts": round(base_total_pnl, 2),
                "total_fills": base_total_fills,
                "pnl_per_fill": round(base_total_ppf, 4),
                "winning_days": base_wins,
            },
            "treatment": {
                "total_pnl_pts": round(treat_total_pnl, 2),
                "total_fills": treat_total_fills,
                "pnl_per_fill": round(treat_total_ppf, 4),
                "winning_days": treat_wins,
                "total_stale_suppressed": total_stale,
            },
            "delta": {
                "pnl_pts": round(treat_total_pnl - base_total_pnl, 2),
                "fills": treat_total_fills - base_total_fills,
                "pnl_per_fill": round(treat_total_ppf - base_total_ppf, 4),
            },
        },
        "per_day": [
            {
                "date": b.date,
                "baseline_pnl": b.total_pnl_pts,
                "baseline_fills": b.total_fills,
                "baseline_pnl_per_fill": b.pnl_per_fill,
                "baseline_pe_blocked": b.pe_blocked,
                "baseline_queue_suppressed": b.queue_suppressed,
                "treatment_pnl": t.total_pnl_pts,
                "treatment_fills": t.total_fills,
                "treatment_pnl_per_fill": t.pnl_per_fill,
                "treatment_pe_blocked": t.pe_blocked,
                "treatment_queue_suppressed": t.queue_suppressed,
                "treatment_stale_suppressed": t.stale_suppressed,
                "delta_pnl": round(t.total_pnl_pts - b.total_pnl_pts, 2),
                "delta_pnl_per_fill": round(t.pnl_per_fill - b.pnl_per_fill, 4),
            }
            for b, t in zip(baseline_results, treatment_results)
        ],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "c3b_b_rerun_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to: {out_path}")
    print("Done.")


if __name__ == "__main__":
    main()
