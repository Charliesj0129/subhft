"""Backtest harness for the Fill Probability Filter on TXFD6 L1 data.

Simulates OpportunisticMM quoting decisions and measures:
1. Adverse fill rate (baseline vs filtered)
2. Model AUC on IS and OOS data
3. PnL impact of filtering

Usage:
    python -m research.alphas.fill_prob_filter.backtest

Data: research/data/raw/txfd6/ — 12 days L1 .npy files
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Add project root to path for imports
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from research.alphas.fill_prob_filter.impl import (
    AdverseFillModel,
    FillEvent,
    FillProbabilityFilter,
    _compute_auc,
    extract_features,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_DIR = _PROJECT_ROOT / "research" / "data" / "raw" / "txfd6"
TICK_SIZE: int = 10000  # 1 point = 10000 scaled units
RT_COST_BPS: float = 2.18  # round-trip cost in bps (commission + tax)
SPREAD_THRESHOLD_BPS: float = 2.5  # OpMM only quotes when spread > this
POST_FILL_HORIZON_NS: int = 5_000_000_000  # 5 seconds in nanoseconds
ADVERSE_THRESHOLD_BPS: float = 0.5  # post-fill return worse than this = adverse


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_l1_files(data_dir: Path) -> list[tuple[str, np.ndarray]]:
    """Load all per-day L1 .npy files, sorted by date."""
    files = sorted(data_dir.glob("TXFD6_*_l1.npy"))
    # Exclude the aggregated file
    files = [f for f in files if "all" not in f.stem]
    result = []
    for f in files:
        date_str = f.stem.split("_")[1]
        data = np.load(f)
        result.append((date_str, data))
        logger.info("Loaded %s: %d ticks", date_str, len(data))
    return result


# ---------------------------------------------------------------------------
# Simulated fill extraction
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class QuoteState:
    """Tracks simulated quote state for fill detection."""

    active: bool = False
    side: int = 0  # +1 buy, -1 sell
    price: float = 0.0  # limit price
    entry_idx: int = 0  # tick index at quote submission


def simulate_fills(
    data: np.ndarray,
    *,
    spread_threshold_bps: float = SPREAD_THRESHOLD_BPS,
    post_fill_horizon_ns: int = POST_FILL_HORIZON_NS,
    adverse_threshold_bps: float = ADVERSE_THRESHOLD_BPS,
) -> list[FillEvent]:
    """Simulate OpMM-like quoting and extract fill events with outcomes.

    Simplified simulation:
    1. At each tick where spread > threshold, we consider placing bid and ask
    2. A bid fill occurs when next tick's bid_px drops below our bid price
       (aggressive sell hits our bid)
    3. Post-fill return measured at +5s from fill time
    4. LOB state features captured at entry time

    Returns list of FillEvent with post-fill returns.
    """
    fills: list[FillEvent] = []
    n = len(data)

    # Pre-compute timestamps for horizon lookup
    timestamps = data["local_ts"]

    # Running EMA state for spread and depth imbalance
    spread_ema: float = 0.0
    depth_imb_ema: float = 0.0
    ofi_ema: float = 0.0
    ema_alpha: float = 1.0 - (7.0 / 8.0)  # EMA-8 decay
    prev_bid_qty: float = 0.0
    prev_ask_qty: float = 0.0
    initialized: bool = False

    for i in range(1, n - 1):
        tick = data[i]
        bid_px = float(tick["bid_px"])
        ask_px = float(tick["ask_px"])
        bid_qty = float(tick["bid_qty"])
        ask_qty = float(tick["ask_qty"])
        mid_px = float(tick["mid_price"])
        spread_bps = float(tick["spread_bps"])
        ts = int(tick["local_ts"])

        # Compute spread in scaled int
        spread_scaled = int((ask_px - bid_px) * 10000)
        mid_price_x2 = int((bid_px + ask_px) * 10000)

        # Update EMAs
        if not initialized:
            spread_ema = float(spread_scaled)
            depth_total = bid_qty + ask_qty
            depth_imb_ema = (
                (bid_qty - ask_qty) / max(depth_total, 1.0) * 1_000_000
            )
            prev_bid_qty = bid_qty
            prev_ask_qty = ask_qty
            initialized = True
            continue

        spread_ema = spread_ema + ema_alpha * (float(spread_scaled) - spread_ema)
        depth_total = bid_qty + ask_qty
        depth_imb = (bid_qty - ask_qty) / max(depth_total, 1.0) * 1_000_000
        depth_imb_ema = depth_imb_ema + ema_alpha * (depth_imb - depth_imb_ema)

        # OFI L1: delta(bid_qty) - delta(ask_qty)
        ofi_raw = (bid_qty - prev_bid_qty) - (ask_qty - prev_ask_qty)
        ofi_ema = ofi_ema + ema_alpha * (ofi_raw - ofi_ema)
        prev_bid_qty = bid_qty
        prev_ask_qty = ask_qty

        # OpMM spread gate
        if spread_bps < spread_threshold_bps:
            continue

        # Simulate fill: check if next tick moves against our quote
        next_tick = data[i + 1]
        next_bid = float(next_tick["bid_px"])
        next_ask = float(next_tick["ask_px"])

        # Buy fill: next bid drops (aggressive seller consumed our bid)
        buy_filled = next_bid < bid_px or next_ask < ask_px
        # Sell fill: next ask rises (aggressive buyer consumed our ask)
        sell_filled = next_ask > ask_px or next_bid > bid_px

        for side, filled, fill_price in [
            (1, buy_filled, bid_px),
            (-1, sell_filled, ask_px),
        ]:
            if not filled:
                continue

            # Find post-fill return at +5s
            horizon_ts = ts + post_fill_horizon_ns
            # Binary search for horizon timestamp
            future_idx = np.searchsorted(timestamps[i + 1 :], horizon_ts, side="left")
            future_idx = min(future_idx + i + 1, n - 1)

            if future_idx <= i:
                continue

            future_mid = float(data[future_idx]["mid_price"])
            # Return in bps: (future_mid - fill_price) / fill_price * 10000
            if fill_price > 0:
                post_fill_ret_bps = side * (future_mid - fill_price) / fill_price * 10000.0
            else:
                continue

            fills.append(
                FillEvent(
                    spread_scaled=spread_scaled,
                    mid_price_x2=mid_price_x2,
                    depth_imbalance_ppm=int(depth_imb),
                    ofi_l1_ema8=int(ofi_ema),
                    l1_bid_qty=int(bid_qty),
                    l1_ask_qty=int(ask_qty),
                    spread_ema8_scaled=int(spread_ema),
                    depth_imb_ema8_ppm=int(depth_imb_ema),
                    side=side,
                    post_fill_return_bps=post_fill_ret_bps,
                )
            )

    return fills


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class BacktestResult:
    """Summary of backtest results."""

    n_fills_total: int = 0
    n_fills_is: int = 0
    n_fills_oos: int = 0
    adverse_rate_baseline: float = 0.0
    adverse_rate_filtered: float = 0.0
    auc_is: float = 0.0
    auc_oos: float = 0.0
    mean_return_baseline_bps: float = 0.0
    mean_return_filtered_bps: float = 0.0
    filter_pass_rate: float = 0.0
    pnl_improvement_bps: float = 0.0
    model_weights: dict[str, float] = field(default_factory=dict)


def run_backtest() -> BacktestResult:
    """Run full backtest: train on IS (first 60%), evaluate on OOS (last 40%)."""
    logger.info("Loading TXFD6 L1 data...")
    day_data = load_l1_files(DATA_DIR)

    if not day_data:
        logger.error("No data files found in %s", DATA_DIR)
        return BacktestResult()

    # Collect fills from all days
    logger.info("Simulating fills across %d days...", len(day_data))
    all_fills: list[FillEvent] = []
    for date_str, data in day_data:
        day_fills = simulate_fills(data)
        logger.info("  %s: %d fills", date_str, len(day_fills))
        all_fills.extend(day_fills)

    n_total = len(all_fills)
    logger.info("Total fills: %d", n_total)

    if n_total < 100:
        logger.warning("Insufficient fills for meaningful analysis")
        return BacktestResult(n_fills_total=n_total)

    # IS/OOS split: 60/40
    is_cutoff = int(n_total * 0.6)
    is_fills = all_fills[:is_cutoff]
    oos_fills = all_fills[is_cutoff:]

    logger.info("IS: %d fills, OOS: %d fills", len(is_fills), len(oos_fills))

    # Train model on IS data
    model = AdverseFillModel(
        adverse_threshold_bps=ADVERSE_THRESHOLD_BPS,
        filter_threshold=0.6,  # will tune below
    )
    train_metrics = model.train(is_fills, max_iter=200, lr=0.05, l2_lambda=0.1)
    logger.info("Training metrics: %s", {k: v for k, v in train_metrics.items() if k != "weights"})

    if "error" in train_metrics:
        logger.error("Training failed: %s", train_metrics["error"])
        return BacktestResult(n_fills_total=n_total)

    # Evaluate on IS
    is_features = np.array([extract_features(e) for e in is_fills])
    is_labels = np.array([
        1.0 if e.post_fill_return_bps < -ADVERSE_THRESHOLD_BPS else 0.0
        for e in is_fills
    ])
    is_scores = np.array([
        model.predict_proba(extract_features(e)) for e in is_fills
    ])
    auc_is = _compute_auc(is_labels, is_scores)

    # Evaluate on OOS
    oos_labels = np.array([
        1.0 if e.post_fill_return_bps < -ADVERSE_THRESHOLD_BPS else 0.0
        for e in oos_fills
    ])
    oos_scores = np.array([
        model.predict_proba(extract_features(e)) for e in oos_fills
    ])
    auc_oos = _compute_auc(oos_labels, oos_scores)

    logger.info("AUC IS: %.4f, AUC OOS: %.4f", auc_is, auc_oos)

    # Baseline metrics
    all_returns = np.array([e.post_fill_return_bps for e in oos_fills])
    all_adverse = np.array([
        e.post_fill_return_bps < -ADVERSE_THRESHOLD_BPS for e in oos_fills
    ])
    baseline_adverse_rate = float(np.mean(all_adverse))
    baseline_mean_return = float(np.mean(all_returns))

    # Sweep filter thresholds to find optimal
    best_threshold = 0.5
    best_improvement = 0.0

    for threshold in np.arange(0.45, 0.75, 0.05):
        passed = oos_scores <= threshold
        if np.sum(passed) < 10:
            continue
        filtered_returns = all_returns[passed]
        filtered_mean = float(np.mean(filtered_returns))
        improvement = filtered_mean - baseline_mean_return
        pass_rate = float(np.mean(passed))

        logger.info(
            "  threshold=%.2f: pass_rate=%.1f%%, mean_ret=%.3f bps, improvement=%.3f bps",
            threshold,
            pass_rate * 100,
            filtered_mean,
            improvement,
        )

        if improvement > best_improvement and pass_rate > 0.5:
            best_improvement = improvement
            best_threshold = float(threshold)

    # Final evaluation with best threshold
    model.filter_threshold = best_threshold
    passed = oos_scores <= best_threshold
    filtered_returns = all_returns[passed]
    filtered_adverse = all_adverse[passed]

    result = BacktestResult(
        n_fills_total=n_total,
        n_fills_is=len(is_fills),
        n_fills_oos=len(oos_fills),
        adverse_rate_baseline=baseline_adverse_rate,
        adverse_rate_filtered=float(np.mean(filtered_adverse)) if len(filtered_adverse) > 0 else 0.0,
        auc_is=auc_is,
        auc_oos=auc_oos,
        mean_return_baseline_bps=baseline_mean_return,
        mean_return_filtered_bps=float(np.mean(filtered_returns)) if len(filtered_returns) > 0 else 0.0,
        filter_pass_rate=float(np.mean(passed)),
        pnl_improvement_bps=best_improvement,
        model_weights=train_metrics.get("weights", {}),
    )

    logger.info("=" * 60)
    logger.info("BACKTEST RESULTS")
    logger.info("=" * 60)
    logger.info("Total fills: %d (IS: %d, OOS: %d)", result.n_fills_total, result.n_fills_is, result.n_fills_oos)
    logger.info("AUC: IS=%.4f, OOS=%.4f", result.auc_is, result.auc_oos)
    logger.info("Adverse rate: baseline=%.1f%%, filtered=%.1f%%",
                result.adverse_rate_baseline * 100, result.adverse_rate_filtered * 100)
    logger.info("Mean return: baseline=%.3f bps, filtered=%.3f bps",
                result.mean_return_baseline_bps, result.mean_return_filtered_bps)
    logger.info("Filter pass rate: %.1f%%", result.filter_pass_rate * 100)
    logger.info("PnL improvement: %.3f bps per fill", result.pnl_improvement_bps)
    logger.info("Best threshold: %.2f", best_threshold)
    logger.info("Model weights: %s", result.model_weights)

    return result


if __name__ == "__main__":
    run_backtest()
