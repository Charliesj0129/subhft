"""C3 Drawdown Breaker — Intraday PnL Trajectory Analysis.

For each of the 12 backtest days, compute fill-by-fill cumulative PnL
and analyze drawdown patterns to validate C3 (Drawdown Circuit Breaker).

Outputs:
  - Per-day: cumulative PnL curve, max drawdown from peak, threshold crossings
  - Walk-forward: calibrate hard stop on days 1-6, evaluate on days 7-12
  - False positive: how often breaker fires on winning days

Usage:
    uv run python research/alphas/r47_maker_pivot/explore_c3_pnl.py
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
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
OUT_DIR = _REPO_ROOT / "outputs" / "team_artifacts" / "alpha-research" / "R47_maker_pivot"

_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9
_IDX_L1_IMBALANCE_PPM = 10


def _load_r47_class():
    # Use deployed r47_maker.py (impl.py has known bugs — T9 pending)
    from hft_platform.strategies.r47_maker import R47MakerStrategy
    return R47MakerStrategy


@dataclass
class FillRecord:
    """A single fill event."""
    ts_ns: int
    side: str  # "buy" or "sell"
    price: float
    qty: int
    position_after: int
    cumulative_pnl: float


@dataclass
class DayPnLAnalysis:
    """Intraday PnL trajectory analysis for one day."""
    date: str
    total_pnl_pts: float
    total_fills: int
    max_drawdown_from_peak: float
    peak_pnl_at_max_dd: float
    trough_pnl_at_max_dd: float
    max_dd_fill_index: int
    crosses_minus_500: int
    crosses_minus_1500: int
    is_winning_day: bool
    # Trajectory shape analysis
    final_third_pnl: float  # PnL accumulated in last 1/3 of fills
    trajectory_shape: str  # "slide", "v_shape", "w_shape", "grind_up", "grind_down"
    # Fill-by-fill PnL curve (sampled at key points for JSON)
    pnl_curve_sampled: list[float]
    fill_timestamps_sampled: list[int]


def run_day_with_fill_tracking(data_path: Path) -> DayPnLAnalysis:
    """Run R47 on one day, tracking every fill for PnL analysis."""
    from hft_platform.contracts.strategy import IntentType, Side, TIF, OrderIntent
    from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
    from hft_platform.strategy.base import StrategyContext

    date_str = data_path.stem.replace("TXFD6_", "").replace("_l2.hftbt", "")

    R47Cls = _load_r47_class()
    strategy = R47Cls(
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

    print(f"  {date_str}: running fill-tracking simulation...")
    t0 = time.monotonic()

    order_id = 0
    active_buy_id: int | None = None
    active_sell_id: int | None = None

    # Equity tracking — sample every step for proper intraday PnL curve
    equity_curve: list[float] = []  # mark-to-market equity at each step
    equity_timestamps: list[int] = []
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

        # Cancel previous orders
        if active_buy_id is not None:
            hbt.cancel(0, active_buy_id, False)
            active_buy_id = None
        if active_sell_id is not None:
            hbt.cancel(0, active_sell_id, False)
            active_sell_id = None
        hbt.clear_inactive_orders(0)

        # Track fills via position change
        cur_pos = int(hbt.position(0))
        if cur_pos != prev_pos:
            fill_count += abs(cur_pos - prev_pos)
        prev_pos = cur_pos

        # Record mark-to-market equity every step
        sv = hbt.state_values(0)
        mid = (best_bid + best_ask) / 2.0
        equity = sv.balance + cur_pos * mid
        equity_curve.append(equity)
        equity_timestamps.append(ts_ns)

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
    elapsed = time.monotonic() - t0

    # Analyze the equity curve (convert to PnL relative to start)
    if not equity_curve:
        equity_curve = [0.0]
        equity_timestamps = [0]

    eq_arr = np.array(equity_curve, dtype=np.float64)
    pnl_arr = eq_arr - eq_arr[0]  # PnL relative to session start
    total_pnl_pts = float(pnl_arr[-1])
    n_fills = fill_count
    n_steps = len(pnl_arr)

    # Max drawdown from peak
    running_max = np.maximum.accumulate(pnl_arr)
    drawdowns = running_max - pnl_arr
    max_dd_idx = int(np.argmax(drawdowns))
    max_dd = float(drawdowns[max_dd_idx])
    peak_at_dd = float(running_max[max_dd_idx])
    trough_at_dd = float(pnl_arr[max_dd_idx])

    # Threshold crossings (on full equity curve)
    crosses_500 = 0
    crosses_1500 = 0
    below_500 = False
    below_1500 = False
    for v in pnl_arr:
        if v < -500 and not below_500:
            crosses_500 += 1
            below_500 = True
        elif v >= -500:
            below_500 = False

        if v < -1500 and not below_1500:
            crosses_1500 += 1
            below_1500 = True
        elif v >= -1500:
            below_1500 = False

    # Trajectory shape classification (using equity curve steps)
    if n_steps >= 10:
        third = n_steps // 3
        first_third_pnl = float(pnl_arr[third])
        second_third_pnl = float(pnl_arr[2 * third])
        final_pnl = float(pnl_arr[-1])
        last_third_pnl = final_pnl - second_third_pnl

        min_pnl = float(np.min(pnl_arr))
        min_idx = int(np.argmin(pnl_arr))

        if final_pnl > 0:
            if min_pnl < -200 and min_idx < 2 * n_steps // 3:
                shape = "v_shape"
            else:
                shape = "grind_up"
        else:
            if min_idx < 2 * n_steps // 3 and final_pnl > min_pnl + 200:
                shape = "v_shape"
            elif pnl_arr[-1] < pnl_arr[n_steps // 2]:
                shape = "slide"
            else:
                shape = "grind_down"
    else:
        last_third_pnl = float(pnl_arr[-1]) if len(pnl_arr) > 0 else 0.0
        shape = "insufficient_data"

    # Sample PnL curve for JSON (max 500 points to capture shape)
    if n_steps <= 500:
        sampled_pnl = [round(float(v), 2) for v in pnl_arr]
        sampled_ts = [int(t) for t in equity_timestamps]
    else:
        indices = np.linspace(0, n_steps - 1, 500, dtype=int)
        sampled_pnl = [round(float(pnl_arr[i]), 2) for i in indices]
        sampled_ts = [int(equity_timestamps[i]) for i in indices]

    result = DayPnLAnalysis(
        date=date_str,
        total_pnl_pts=round(total_pnl_pts, 2),
        total_fills=fill_count,
        max_drawdown_from_peak=round(max_dd, 2),
        peak_pnl_at_max_dd=round(peak_at_dd, 2),
        trough_pnl_at_max_dd=round(trough_at_dd, 2),
        max_dd_fill_index=max_dd_idx,
        crosses_minus_500=crosses_500,
        crosses_minus_1500=crosses_1500,
        is_winning_day=total_pnl_pts > 0,
        final_third_pnl=round(last_third_pnl, 2),
        trajectory_shape=shape,
        pnl_curve_sampled=sampled_pnl,
        fill_timestamps_sampled=sampled_ts,
    )

    print(
        f"  {date_str}: PnL={total_pnl_pts:+.1f} pts, fills={fill_count}, "
        f"maxDD={max_dd:.1f}, x500={crosses_500}, x1500={crosses_1500}, "
        f"shape={shape}, elapsed={elapsed:.1f}s"
    )

    return result


def simulate_breaker(pnl_curve: list[float], hard_stop: float, cooldown_fills: int = 0) -> dict:
    """Simulate a drawdown breaker on a PnL curve.

    Returns dict with:
      - triggered: bool
      - trigger_fill_index: int or None
      - pnl_at_trigger: float
      - pnl_saved: float (difference between actual final PnL and PnL at trigger)
      - would_have_missed: float (PnL accumulated after trigger if positive recovery)
    """
    arr = np.array(pnl_curve, dtype=np.float64)
    triggered = False
    trigger_idx = None
    pnl_at_trigger = 0.0

    for i, v in enumerate(arr):
        if v < hard_stop:
            triggered = True
            trigger_idx = i
            pnl_at_trigger = float(v)
            break

    if not triggered:
        return {
            "triggered": False,
            "trigger_fill_index": None,
            "pnl_at_trigger": None,
            "pnl_saved": 0.0,
            "would_have_missed_recovery": 0.0,
            "final_pnl_actual": round(float(arr[-1]), 2),
            "final_pnl_with_breaker": round(float(arr[-1]), 2),
        }

    final_actual = float(arr[-1])
    # After breaker fires, strategy stops quoting.
    # With cooldown, it resumes after cooldown_fills worth of fills
    # For simplicity: hard stop = stop all quoting for rest of day
    final_with_breaker = pnl_at_trigger  # flat from trigger point

    # If cooldown mode: resume after cooldown_fills
    if cooldown_fills > 0 and trigger_idx + cooldown_fills < len(arr):
        resume_idx = trigger_idx + cooldown_fills
        # PnL from resume to end (relative — we're flat during cooldown, so
        # the post-cooldown PnL is the curve from resume onwards minus the
        # resume point, added to our stopped PnL)
        post_cooldown_pnl = float(arr[-1]) - float(arr[resume_idx])
        final_with_breaker = pnl_at_trigger + post_cooldown_pnl

    recovery_after_trigger = final_actual - pnl_at_trigger
    pnl_saved = final_with_breaker - final_actual

    return {
        "triggered": True,
        "trigger_fill_index": trigger_idx,
        "pnl_at_trigger": round(pnl_at_trigger, 2),
        "pnl_saved": round(pnl_saved, 2),
        "would_have_missed_recovery": round(max(0, recovery_after_trigger), 2),
        "final_pnl_actual": round(final_actual, 2),
        "final_pnl_with_breaker": round(final_with_breaker, 2),
    }


def main() -> None:
    if not DATA_FILES:
        print(f"ERROR: No data files found in {DATA_DIR}")
        sys.exit(1)

    print(f"\nC3 Drawdown Breaker — Intraday PnL Analysis")
    print(f"Data: {len(DATA_FILES)} days of TXFD6 L2")
    print("=" * 60)

    all_results: list[DayPnLAnalysis] = []
    for data_path in DATA_FILES:
        try:
            result = run_day_with_fill_tracking(data_path)
            all_results.append(result)
        except Exception as exc:
            import traceback
            print(f"  FAILED: {data_path.name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    if not all_results:
        print("ERROR: No results generated")
        sys.exit(1)

    # Sort by date
    all_results.sort(key=lambda r: r.date)

    # ── Analysis ──────────────────────────────────────────────────────

    print("\n" + "=" * 60)
    print("PER-DAY SUMMARY")
    print("=" * 60)
    print(f"{'Date':<12} {'PnL':>8} {'Fills':>6} {'MaxDD':>8} {'x500':>5} {'x1500':>6} {'Shape':<12}")
    print("-" * 60)
    for r in all_results:
        print(
            f"{r.date:<12} {r.total_pnl_pts:>+8.1f} {r.total_fills:>6} "
            f"{r.max_drawdown_from_peak:>8.1f} {r.crosses_minus_500:>5} "
            f"{r.crosses_minus_1500:>6} {r.trajectory_shape:<12}"
        )

    # ── Walk-Forward Analysis ─────────────────────────────────────────
    print("\n" + "=" * 60)
    print("WALK-FORWARD ANALYSIS (calibrate on days 1-6, test on days 7-12)")
    print("=" * 60)

    n = len(all_results)
    split = min(6, n // 2)
    train_days = all_results[:split]
    test_days = all_results[split:]

    # Calibrate: worst drawdown in training set
    train_max_dd = max(r.max_drawdown_from_peak for r in train_days)
    # Set hard stop at negative of worst training drawdown (conservative)
    hard_stop_calibrated = -train_max_dd
    # Also test a few fixed thresholds
    thresholds = [-500, -1000, -1500, hard_stop_calibrated]
    thresholds = sorted(set(round(t, 1) for t in thresholds))

    print(f"\nTraining days: {[r.date for r in train_days]}")
    print(f"Test days: {[r.date for r in test_days]}")
    print(f"Worst training drawdown: {train_max_dd:.1f} pts")
    print(f"Calibrated hard stop: {hard_stop_calibrated:.1f} pts")

    wf_results = {}
    for threshold in thresholds:
        print(f"\n--- Hard Stop = {threshold:.0f} pts ---")
        train_total = 0.0
        test_total = 0.0
        train_triggers = 0
        test_triggers = 0
        false_positives_train = 0
        false_positives_test = 0

        for r in train_days:
            sim = simulate_breaker(r.pnl_curve_sampled, threshold)
            pnl_with = sim["final_pnl_with_breaker"]
            train_total += pnl_with
            if sim["triggered"]:
                train_triggers += 1
                if r.is_winning_day:
                    false_positives_train += 1
            print(
                f"  [TRAIN] {r.date}: actual={r.total_pnl_pts:+.1f}, "
                f"w/breaker={pnl_with:+.1f}, saved={sim['pnl_saved']:+.1f}, "
                f"triggered={sim['triggered']}"
            )

        for r in test_days:
            sim = simulate_breaker(r.pnl_curve_sampled, threshold)
            pnl_with = sim["final_pnl_with_breaker"]
            test_total += pnl_with
            if sim["triggered"]:
                test_triggers += 1
                if r.is_winning_day:
                    false_positives_test += 1
            print(
                f"  [TEST]  {r.date}: actual={r.total_pnl_pts:+.1f}, "
                f"w/breaker={pnl_with:+.1f}, saved={sim['pnl_saved']:+.1f}, "
                f"triggered={sim['triggered']}"
            )

        actual_train = sum(r.total_pnl_pts for r in train_days)
        actual_test = sum(r.total_pnl_pts for r in test_days)
        print(f"\n  TRAIN: actual={actual_train:+.1f}, w/breaker={train_total:+.1f}, "
              f"improvement={train_total - actual_train:+.1f}, triggers={train_triggers}, "
              f"false_pos={false_positives_train}")
        print(f"  TEST:  actual={actual_test:+.1f}, w/breaker={test_total:+.1f}, "
              f"improvement={test_total - actual_test:+.1f}, triggers={test_triggers}, "
              f"false_pos={false_positives_test}")

        wf_results[str(round(threshold))] = {
            "threshold": round(threshold, 1),
            "train_actual": round(actual_train, 2),
            "train_with_breaker": round(train_total, 2),
            "train_improvement": round(train_total - actual_train, 2),
            "train_triggers": train_triggers,
            "train_false_positives": false_positives_train,
            "test_actual": round(actual_test, 2),
            "test_with_breaker": round(test_total, 2),
            "test_improvement": round(test_total - actual_test, 2),
            "test_triggers": test_triggers,
            "test_false_positives": false_positives_test,
        }

    # ── False Positive Analysis ───────────────────────────────────────
    print("\n" + "=" * 60)
    print("FALSE POSITIVE ANALYSIS (breaker on winning days)")
    print("=" * 60)

    winning_days = [r for r in all_results if r.is_winning_day]
    losing_days = [r for r in all_results if not r.is_winning_day]

    fp_analysis = {}
    for threshold in [-500, -1000, -1500]:
        triggered_winners = 0
        total_pnl_lost_on_winners = 0.0
        for r in winning_days:
            sim = simulate_breaker(r.pnl_curve_sampled, threshold)
            if sim["triggered"]:
                triggered_winners += 1
                total_pnl_lost_on_winners += sim["pnl_saved"]  # negative = lost PnL

        saved_on_losers = 0.0
        triggered_losers = 0
        for r in losing_days:
            sim = simulate_breaker(r.pnl_curve_sampled, threshold)
            if sim["triggered"]:
                triggered_losers += 1
                saved_on_losers += sim["pnl_saved"]  # positive = saved PnL

        fp_rate = triggered_winners / len(winning_days) if winning_days else 0.0
        tp_rate = triggered_losers / len(losing_days) if losing_days else 0.0

        fp_analysis[str(threshold)] = {
            "threshold": threshold,
            "winning_days": len(winning_days),
            "losing_days": len(losing_days),
            "false_positive_rate": round(fp_rate, 3),
            "true_positive_rate": round(tp_rate, 3),
            "pnl_lost_on_false_positives": round(total_pnl_lost_on_winners, 2),
            "pnl_saved_on_true_positives": round(saved_on_losers, 2),
            "net_impact": round(saved_on_losers + total_pnl_lost_on_winners, 2),
        }
        print(
            f"  Threshold {threshold}: FP={triggered_winners}/{len(winning_days)} "
            f"({fp_rate:.0%}), TP={triggered_losers}/{len(losing_days)} ({tp_rate:.0%}), "
            f"net={saved_on_losers + total_pnl_lost_on_winners:+.1f} pts"
        )

    # ── 03-31 Deep Dive ───────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("03-31 DEEP DIVE (worst day)")
    print("=" * 60)

    day_0331 = next((r for r in all_results if "03-31" in r.date), None)
    deep_dive_0331 = {}
    if day_0331:
        curve = np.array(day_0331.pnl_curve_sampled, dtype=np.float64)
        n = len(curve)

        # Find min point
        min_idx = int(np.argmin(curve))
        min_pnl = float(curve[min_idx])

        # After minimum, did it recover?
        post_min_max = float(np.max(curve[min_idx:])) if min_idx < n - 1 else min_pnl
        recovery = post_min_max - min_pnl
        final = float(curve[-1])

        # Classify
        if recovery > abs(min_pnl) * 0.3:
            slide_or_v = "v_shape" if final > min_pnl + 200 else "partial_v"
        else:
            slide_or_v = "continued_slide"

        deep_dive_0331 = {
            "total_fills": day_0331.total_fills,
            "total_pnl": round(float(curve[-1]), 2),
            "min_pnl": round(min_pnl, 2),
            "min_pnl_fill_index": min_idx,
            "min_pnl_position_pct": round(min_idx / max(n - 1, 1) * 100, 1),
            "post_min_recovery": round(recovery, 2),
            "trajectory_classification": slide_or_v,
            "max_drawdown": round(day_0331.max_drawdown_from_peak, 2),
            "crosses_500": day_0331.crosses_minus_500,
            "crosses_1500": day_0331.crosses_minus_1500,
        }
        print(f"  Total fills: {day_0331.total_fills}")
        print(f"  Min PnL: {min_pnl:.1f} at fill {min_idx} ({min_idx/max(n-1,1)*100:.0f}% through day)")
        print(f"  Post-min recovery: {recovery:.1f} pts")
        print(f"  Final PnL: {final:.1f} pts")
        print(f"  Classification: {slide_or_v}")
    else:
        print("  WARNING: 03-31 data not found in results")

    # ── Build output JSON ─────────────────────────────────────────────
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "metadata": {
            "script": "explore_c3_pnl.py",
            "n_days": len(all_results),
            "dates": [r.date for r in all_results],
            "config": {
                "queue_model": "PowerProbQueueModel(3.0)",
                "latency_us": 47,
                "elapse_ns": ELAPSE_NS,
                "max_pos": 3,
            },
        },
        "per_day": [
            {
                "date": r.date,
                "total_pnl_pts": r.total_pnl_pts,
                "total_fills": r.total_fills,
                "is_winning_day": r.is_winning_day,
                "max_drawdown_from_peak": r.max_drawdown_from_peak,
                "peak_pnl_at_max_dd": r.peak_pnl_at_max_dd,
                "trough_pnl_at_max_dd": r.trough_pnl_at_max_dd,
                "max_dd_fill_index": r.max_dd_fill_index,
                "crosses_minus_500": r.crosses_minus_500,
                "crosses_minus_1500": r.crosses_minus_1500,
                "trajectory_shape": r.trajectory_shape,
                "final_third_pnl": r.final_third_pnl,
                "pnl_curve": r.pnl_curve_sampled,
            }
            for r in all_results
        ],
        "walk_forward": wf_results,
        "false_positive_analysis": fp_analysis,
        "deep_dive_0331": deep_dive_0331,
    }

    out_path = OUT_DIR / "c3_intraday_pnl_analysis.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to: {out_path}")
    print("Done.")


if __name__ == "__main__":
    main()
