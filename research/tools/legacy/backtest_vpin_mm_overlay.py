"""VPIN as MM risk overlay — comparative backtest.

Measures the value of VPIN regime detection as a position-sizing factor
for a simple market-making strategy on real TXFD6 data.

Two scenarios:
  1. Baseline MM: always quotes full size (±5 contracts)
  2. VPIN-Adjusted MM: scales quoting by VPIN regime
     - LOW:      full size (5 contracts)
     - ELEVATED: reduced  (3 contracts)
     - TOXIC:    minimal  (1 contract) + widen spread by 1 tick

Key question: Does adding VPIN regime detection reduce max drawdown
while maintaining acceptable Sharpe?

Usage:
    uv run python research/tools/backtest_vpin_mm_overlay.py

Outputs: outputs/team_artifacts/alpha-research/stage4_vpin_mm_overlay.json
"""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import structlog

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from research.alphas.vpin_regime_switch.impl import (  # noqa: E402
    Regime,
    VpinRegimeSwitchAlpha,
)

logger = structlog.get_logger("backtest.vpin_mm_overlay")

_DEFAULT_DATA = _REPO_ROOT / "research" / "data" / "raw" / "txfd6" / "TXFD6_all_l1.npy"
_OUT_PATH = (
    _REPO_ROOT
    / "outputs"
    / "team_artifacts"
    / "alpha-research"
    / "stage4_vpin_mm_overlay.json"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mini-TAIEX: 1 point = 10 NTD, tick size = 1 point
TICK_SIZE_POINTS: int = 1
POINT_VALUE_NTD: int = 10  # 1 point = 10 NTD for Mini-TAIEX

# Transaction cost: 0.5 tick per round-trip (per side = 0.25 tick)
HALF_RT_COST_POINTS: int = 0  # we apply 0.5 tick per RT at PnL summary
RT_COST_NTD: int = TICK_SIZE_POINTS * POINT_VALUE_NTD // 2  # 5 NTD per RT

# Position limits
MAX_POS_BASELINE: int = 5
MAX_POS_VPIN: int = 5

# VPIN regime -> max quoting size
REGIME_MAX_SIZE: dict[int, int] = {
    Regime.LOW: 5,
    Regime.ELEVATED: 3,
    Regime.TOXIC: 1,
}

# VPIN regime -> spread widening (in ticks/points)
REGIME_SPREAD_WIDEN: dict[int, int] = {
    Regime.LOW: 0,
    Regime.ELEVATED: 0,
    Regime.TOXIC: 1,  # widen by 1 tick in TOXIC
}

# Latency: Shioaji P95 submit = 36ms
# Typical tick rate ~400k/day, ~0.07ms/tick => ~500 ticks delay
LATENCY_TICKS: int = 500

# VPIN parameters (depth-churn mode since L1 data has no trade volume)
VPIN_BAR_VOLUME_TARGET: int = 500
VPIN_N_BUCKETS: int = 50
VPIN_WARMUP_BARS: int = 60

# Auto-calibration warmup: first N rows used to collect VPIN history
CALIBRATION_ROWS: int = 200_000


# ---------------------------------------------------------------------------
# Data structures (using scaled int for PnL accounting)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MMState:
    """Mutable MM simulation state.

    All monetary values in scaled int: NTD * 10000.
    """

    position: int = 0
    realized_pnl: int = 0  # NTD * 10000
    n_fills: int = 0
    n_buys: int = 0
    n_sells: int = 0
    peak_equity: int = 0
    max_drawdown: int = 0  # positive value = worst drawdown
    equity_curve: list[int] = field(default_factory=list)
    # Per-regime tracking: equity change attributed to each regime
    last_equity_snapshot: int = 0
    pnl_in_regime: dict[int, int] = field(
        default_factory=lambda: {Regime.LOW: 0, Regime.ELEVATED: 0, Regime.TOXIC: 0}
    )
    fills_in_regime: dict[int, int] = field(
        default_factory=lambda: {Regime.LOW: 0, Regime.ELEVATED: 0, Regime.TOXIC: 0}
    )

    def mark_to_market(self, mid_price_points: int) -> int:
        """Return total equity = realized + unrealized, in NTD * 10000."""
        unrealized = self.position * mid_price_points * POINT_VALUE_NTD * 10000
        return self.realized_pnl + unrealized

    def update_drawdown(self, equity: int) -> None:
        if equity > self.peak_equity:
            self.peak_equity = equity
        dd = self.peak_equity - equity
        if dd > self.max_drawdown:
            self.max_drawdown = dd

    def attribute_pnl_to_regime(self, current_equity: int, regime: int) -> None:
        """Attribute equity change since last snapshot to the current regime."""
        delta = current_equity - self.last_equity_snapshot
        self.pnl_in_regime[regime] += delta
        self.last_equity_snapshot = current_equity


@dataclass(slots=True, frozen=True)
class SimResult:
    """Immutable backtest result summary."""

    label: str
    total_pnl_ntd: float
    sharpe: float
    max_drawdown_ntd: float
    n_fills: int
    n_buys: int
    n_sells: int
    pnl_per_regime_ntd: dict[str, float]
    fills_per_regime: dict[str, int]
    equity_curve_ntd: list[float]


# ---------------------------------------------------------------------------
# Fill simulation
# ---------------------------------------------------------------------------


def _try_fill(
    state: MMState,
    mid_price_points: int,
    prev_mid_price_points: int,
    bid_price_points: int,
    ask_price_points: int,
    max_pos: int,
    regime: int,
) -> None:
    """Simple fill model: if mid_price crosses our quote level, we get filled.

    - Buy fill: mid_price drops to or below our bid -> we buy at bid
    - Sell fill: mid_price rises to or above our ask -> we sell at ask

    Each fill is 1 contract.
    """
    # Buy fill: mid crossed down through our bid
    if mid_price_points <= bid_price_points and state.position < max_pos:
        # Bought 1 contract at bid_price
        cost_scaled = bid_price_points * POINT_VALUE_NTD * 10000
        state.realized_pnl -= cost_scaled
        state.position += 1
        state.n_fills += 1
        state.n_buys += 1
        # Transaction cost (half RT)
        state.realized_pnl -= RT_COST_NTD * 10000 // 2
        state.fills_in_regime[regime] += 1

    # Sell fill: mid crossed up through our ask
    if mid_price_points >= ask_price_points and state.position > -max_pos:
        # Sold 1 contract at ask_price
        revenue_scaled = ask_price_points * POINT_VALUE_NTD * 10000
        state.realized_pnl += revenue_scaled
        state.position -= 1
        state.n_fills += 1
        state.n_sells += 1
        # Transaction cost (half RT)
        state.realized_pnl -= RT_COST_NTD * 10000 // 2
        state.fills_in_regime[regime] += 1


# ---------------------------------------------------------------------------
# Main simulation loop
# ---------------------------------------------------------------------------


def run_mm_simulation(
    data: np.ndarray,
    *,
    use_vpin: bool,
    vpin_alpha: VpinRegimeSwitchAlpha | None = None,
    regime_tracker: VpinRegimeSwitchAlpha | None = None,
    label: str = "baseline",
    sample_interval: int = 1000,
) -> SimResult:
    """Run MM simulation over L1 data.

    Args:
        data: Structured numpy array with bid_px, ask_px, bid_qty, ask_qty, etc.
        use_vpin: Whether to apply VPIN regime overlay for position sizing.
        vpin_alpha: Pre-configured VPIN alpha for position sizing decisions.
        regime_tracker: Separate VPIN alpha used only for PnL attribution
            (runs in parallel but does not affect trading decisions).
        label: Result label.
        sample_interval: Equity curve sampling interval (rows).

    Returns:
        SimResult with metrics.
    """
    n = len(data)
    state = MMState()

    bid_px = data["bid_px"]
    ask_px = data["ask_px"]
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]

    # Pre-compute mid prices in points (raw float prices are in points for TXFD6)
    mid_prices_points = np.round((bid_px + ask_px) / 2.0).astype(np.int64)

    # Spread = ask - bid in points
    spreads_points = np.round(ask_px - bid_px).astype(np.int64)

    # Delayed regime: apply latency by using regime from LATENCY_TICKS ago
    regime_buffer: list[int] = []
    current_regime: int = Regime.LOW
    # Attribution regime (no latency, for PnL tracking only)
    attribution_regime: int = Regime.LOW

    prev_mid: int = int(mid_prices_points[0])

    logger.info(
        "starting_simulation",
        label=label,
        use_vpin=use_vpin,
        n_rows=n,
        latency_ticks=LATENCY_TICKS,
    )

    t0 = time.monotonic()

    for i in range(n):
        mid = int(mid_prices_points[i])
        spread = int(spreads_points[i])

        # Skip invalid rows
        if mid <= 0 or spread < 0:
            prev_mid = mid
            continue

        # --- VPIN update ---
        bid_d = int(bid_qty[i])
        ask_d = int(ask_qty[i])
        mid_x2 = int(round(bid_px[i])) + int(round(ask_px[i]))
        ts = int(data["local_ts"][i])

        if use_vpin and vpin_alpha is not None:
            # Feed depth update to VPIN (depth-churn mode)
            vpin_alpha.update(
                mid_price_x2=mid_x2,
                bid_depth=bid_d,
                ask_depth=ask_d,
                ts=ts,
            )
            # Store regime with latency delay
            regime_buffer.append(int(vpin_alpha.regime))
            if len(regime_buffer) > LATENCY_TICKS:
                current_regime = regime_buffer[-LATENCY_TICKS - 1]
            else:
                current_regime = Regime.LOW  # warmup: assume LOW

        # Regime tracker for PnL attribution (runs even for baseline)
        if regime_tracker is not None:
            regime_tracker.update(
                mid_price_x2=mid_x2,
                bid_depth=bid_d,
                ask_depth=ask_d,
                ts=ts,
            )
            attribution_regime = int(regime_tracker.regime)

        # --- Quote generation ---
        if use_vpin:
            max_pos = min(MAX_POS_VPIN, REGIME_MAX_SIZE.get(current_regime, 5))
            widen = REGIME_SPREAD_WIDEN.get(current_regime, 0)
        else:
            max_pos = MAX_POS_BASELINE
            widen = 0

        # Quote at best bid/ask, with optional widening
        bid_quote = int(round(bid_px[i])) - widen
        ask_quote = int(round(ask_px[i])) + widen

        # --- Fill check ---
        _try_fill(
            state,
            mid,
            prev_mid,
            bid_quote,
            ask_quote,
            max_pos,
            attribution_regime,
        )

        # --- Equity tracking ---
        if i % sample_interval == 0:
            equity = state.mark_to_market(mid)
            state.update_drawdown(equity)
            state.equity_curve.append(equity)
            # Attribute equity change to current regime
            state.attribute_pnl_to_regime(equity, attribution_regime)

        prev_mid = mid

    # Final mark-to-market
    final_mid = int(mid_prices_points[-1])
    final_equity = state.mark_to_market(final_mid)
    state.update_drawdown(final_equity)
    state.equity_curve.append(final_equity)

    elapsed = time.monotonic() - t0
    logger.info(
        "simulation_complete",
        label=label,
        elapsed_s=round(elapsed, 2),
        n_fills=state.n_fills,
        final_position=state.position,
    )

    # --- Compute Sharpe ---
    eq_arr = np.array(state.equity_curve, dtype=np.float64)
    returns = np.diff(eq_arr)
    if len(returns) > 1 and returns.std() > 1e-15:
        # Annualize: ~4 trading days of data, sample_interval=1000
        # Each sample ≈ 1000 ticks, ~400k ticks/day => ~400 samples/day
        samples_per_day = max(1, n // sample_interval // 4)
        sharpe = float(returns.mean() / returns.std()) * math.sqrt(
            252 * samples_per_day
        )
    else:
        sharpe = 0.0

    # Convert scaled int to NTD for reporting
    scale = 10000.0
    total_pnl_ntd = final_equity / scale
    max_dd_ntd = state.max_drawdown / scale

    pnl_per_regime_ntd = {
        Regime(k).name: v / scale for k, v in state.pnl_in_regime.items()
    }
    fills_per_regime = {
        Regime(k).name: v for k, v in state.fills_in_regime.items()
    }
    equity_curve_ntd = [e / scale for e in state.equity_curve]

    return SimResult(
        label=label,
        total_pnl_ntd=round(total_pnl_ntd, 2),
        sharpe=round(sharpe, 4),
        max_drawdown_ntd=round(max_dd_ntd, 2),
        n_fills=state.n_fills,
        n_buys=state.n_buys,
        n_sells=state.n_sells,
        pnl_per_regime_ntd=pnl_per_regime_ntd,
        fills_per_regime=fills_per_regime,
        equity_curve_ntd=equity_curve_ntd,
    )


# ---------------------------------------------------------------------------
# VPIN calibration pass
# ---------------------------------------------------------------------------


def calibrate_vpin(data: np.ndarray, n_rows: int) -> VpinRegimeSwitchAlpha:
    """Run VPIN on first n_rows to collect history, then auto-calibrate thresholds.

    Returns a fresh, calibrated VpinRegimeSwitchAlpha ready for the full run.
    """
    logger.info("calibrating_vpin", n_rows=n_rows)

    # Calibration pass: collect raw VPIN values
    cal_alpha = VpinRegimeSwitchAlpha(
        bar_volume_target=VPIN_BAR_VOLUME_TARGET,
        n_vpin_buckets=VPIN_N_BUCKETS,
        warmup_bars=VPIN_WARMUP_BARS,
        use_tick_volume=False,  # L1 data has no trade volume
    )

    vpin_history: list[float] = []
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]
    bid_px = data["bid_px"]
    ask_px = data["ask_px"]

    limit = min(n_rows, len(data))
    for i in range(limit):
        mid_x2 = int(round(bid_px[i])) + int(round(ask_px[i]))
        bid_d = int(bid_qty[i])
        ask_d = int(ask_qty[i])
        ts = int(data["local_ts"][i])
        cal_alpha.update(
            mid_price_x2=mid_x2,
            bid_depth=bid_d,
            ask_depth=ask_d,
            ts=ts,
        )
        if cal_alpha.bars_seen > VPIN_WARMUP_BARS:
            vpin_history.append(cal_alpha.raw_vpin)

    logger.info(
        "calibration_vpin_stats",
        bars_seen=cal_alpha.bars_seen,
        vpin_samples=len(vpin_history),
        vpin_mean=round(np.mean(vpin_history), 4) if vpin_history else 0.0,
        vpin_std=round(np.std(vpin_history), 4) if vpin_history else 0.0,
    )

    # Create production alpha with calibrated thresholds
    prod_alpha = VpinRegimeSwitchAlpha(
        bar_volume_target=VPIN_BAR_VOLUME_TARGET,
        n_vpin_buckets=VPIN_N_BUCKETS,
        warmup_bars=VPIN_WARMUP_BARS,
        use_tick_volume=False,
    )

    if len(vpin_history) >= 20:
        # Access the internal RegimeDetector to calibrate
        prod_alpha._regime_detector.calibrate(vpin_history)
        logger.info(
            "calibration_thresholds",
            threshold_elevated=round(
                prod_alpha._regime_detector.threshold_elevated, 4
            ),
            threshold_toxic=round(
                prod_alpha._regime_detector.threshold_toxic, 4
            ),
        )
    else:
        logger.warning("calibration_insufficient_data", n_samples=len(vpin_history))

    return prod_alpha


# ---------------------------------------------------------------------------
# Regime statistics
# ---------------------------------------------------------------------------


def compute_regime_stats(
    data: np.ndarray,
    vpin_alpha: VpinRegimeSwitchAlpha,
) -> dict[str, Any]:
    """Run VPIN over full data and compute regime time fractions + transitions."""
    logger.info("computing_regime_stats")

    n = len(data)
    regime_counts: dict[int, int] = {Regime.LOW: 0, Regime.ELEVATED: 0, Regime.TOXIC: 0}
    transitions: int = 0
    prev_regime: int = Regime.LOW

    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]
    bid_px = data["bid_px"]
    ask_px = data["ask_px"]

    # Fresh alpha with same calibrated thresholds
    alpha = VpinRegimeSwitchAlpha(
        bar_volume_target=VPIN_BAR_VOLUME_TARGET,
        n_vpin_buckets=VPIN_N_BUCKETS,
        warmup_bars=VPIN_WARMUP_BARS,
        use_tick_volume=False,
    )
    # Copy calibrated thresholds
    alpha._regime_detector._threshold_elevated = (
        vpin_alpha._regime_detector.threshold_elevated
    )
    alpha._regime_detector._threshold_toxic = (
        vpin_alpha._regime_detector.threshold_toxic
    )

    for i in range(n):
        mid_x2 = int(round(bid_px[i])) + int(round(ask_px[i]))
        bid_d = int(bid_qty[i])
        ask_d = int(ask_qty[i])
        ts = int(data["local_ts"][i])
        alpha.update(mid_price_x2=mid_x2, bid_depth=bid_d, ask_depth=ask_d, ts=ts)

        r = int(alpha.regime)
        regime_counts[r] += 1
        if r != prev_regime:
            transitions += 1
            prev_regime = r

    total = sum(regime_counts.values())
    regime_fractions = {
        Regime(k).name: round(v / max(total, 1) * 100.0, 2)
        for k, v in regime_counts.items()
    }

    return {
        "regime_fractions_pct": regime_fractions,
        "regime_counts": {Regime(k).name: v for k, v in regime_counts.items()},
        "n_transitions": transitions,
        "threshold_elevated": round(
            vpin_alpha._regime_detector.threshold_elevated, 4
        ),
        "threshold_toxic": round(vpin_alpha._regime_detector.threshold_toxic, 4),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _make_tracker(calibrated_alpha: VpinRegimeSwitchAlpha) -> VpinRegimeSwitchAlpha:
    """Create a fresh VPIN alpha with the same calibrated thresholds.

    Used for PnL attribution without affecting trading decisions.
    """
    tracker = VpinRegimeSwitchAlpha(
        bar_volume_target=VPIN_BAR_VOLUME_TARGET,
        n_vpin_buckets=VPIN_N_BUCKETS,
        warmup_bars=VPIN_WARMUP_BARS,
        use_tick_volume=False,
    )
    tracker._regime_detector._threshold_elevated = (
        calibrated_alpha._regime_detector.threshold_elevated
    )
    tracker._regime_detector._threshold_toxic = (
        calibrated_alpha._regime_detector.threshold_toxic
    )
    return tracker


def main() -> None:
    data_path = _DEFAULT_DATA
    if not data_path.exists():
        logger.error("data_not_found", path=str(data_path))
        sys.exit(1)

    logger.info("loading_data", path=str(data_path))
    data = np.load(str(data_path), allow_pickle=True)
    logger.info("data_loaded", rows=len(data), fields=list(data.dtype.names or []))

    # --- Step 1: Calibrate VPIN thresholds ---
    vpin_alpha = calibrate_vpin(data, n_rows=CALIBRATION_ROWS)

    # --- Step 2: Run Baseline MM (with regime tracker for PnL attribution) ---
    baseline_tracker = _make_tracker(vpin_alpha)
    baseline = run_mm_simulation(
        data,
        use_vpin=False,
        regime_tracker=baseline_tracker,
        label="baseline_mm",
    )

    # --- Step 3: Run VPIN-Adjusted MM ---
    vpin_tracker = _make_tracker(vpin_alpha)
    vpin_result = run_mm_simulation(
        data,
        use_vpin=True,
        vpin_alpha=vpin_alpha,
        regime_tracker=vpin_tracker,
        label="vpin_adjusted_mm",
    )

    # --- Step 4: Compute regime statistics ---
    regime_stats = compute_regime_stats(data, vpin_alpha)

    # --- Step 5: Comparative analysis ---
    dd_reduction_pct = 0.0
    if baseline.max_drawdown_ntd > 0:
        dd_reduction_pct = round(
            (1.0 - vpin_result.max_drawdown_ntd / baseline.max_drawdown_ntd) * 100.0,
            2,
        )

    sharpe_delta = round(vpin_result.sharpe - baseline.sharpe, 4)
    pnl_delta = round(vpin_result.total_pnl_ntd - baseline.total_pnl_ntd, 2)

    comparison = {
        "baseline": {
            "total_pnl_ntd": baseline.total_pnl_ntd,
            "sharpe": baseline.sharpe,
            "max_drawdown_ntd": baseline.max_drawdown_ntd,
            "n_fills": baseline.n_fills,
            "n_buys": baseline.n_buys,
            "n_sells": baseline.n_sells,
        },
        "vpin_adjusted": {
            "total_pnl_ntd": vpin_result.total_pnl_ntd,
            "sharpe": vpin_result.sharpe,
            "max_drawdown_ntd": vpin_result.max_drawdown_ntd,
            "n_fills": vpin_result.n_fills,
            "n_buys": vpin_result.n_buys,
            "n_sells": vpin_result.n_sells,
        },
        "delta": {
            "pnl_delta_ntd": pnl_delta,
            "sharpe_delta": sharpe_delta,
            "drawdown_reduction_pct": dd_reduction_pct,
        },
        "regime_stats": regime_stats,
        "config": {
            "max_pos_baseline": MAX_POS_BASELINE,
            "max_pos_vpin": MAX_POS_VPIN,
            "regime_max_size": {Regime(k).name: v for k, v in REGIME_MAX_SIZE.items()},
            "regime_spread_widen": {
                Regime(k).name: v for k, v in REGIME_SPREAD_WIDEN.items()
            },
            "latency_ticks": LATENCY_TICKS,
            "latency_ms": 36.0,
            "vpin_bar_volume_target": VPIN_BAR_VOLUME_TARGET,
            "vpin_n_buckets": VPIN_N_BUCKETS,
            "vpin_warmup_bars": VPIN_WARMUP_BARS,
            "tick_size_points": TICK_SIZE_POINTS,
            "point_value_ntd": POINT_VALUE_NTD,
            "rt_cost_ntd": RT_COST_NTD,
            "data_rows": len(data),
            "data_source": str(data_path.name),
            "calibration_rows": CALIBRATION_ROWS,
        },
        "pnl_during_toxic": {
            "baseline_ntd": baseline.pnl_per_regime_ntd.get("TOXIC", 0.0),
            "vpin_adjusted_ntd": vpin_result.pnl_per_regime_ntd.get("TOXIC", 0.0),
        },
    }

    # --- Save results ---
    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_OUT_PATH, "w") as f:
        json.dump(comparison, f, indent=2, default=str)
    logger.info("results_saved", path=str(_OUT_PATH))

    # --- Print comparison table ---
    print("\n" + "=" * 72)
    print("  VPIN MM OVERLAY BACKTEST — COMPARATIVE RESULTS")
    print("=" * 72)
    print(f"  Data: {data_path.name} ({len(data):,} rows)")
    print(f"  Latency model: {LATENCY_TICKS} ticks (~36ms P95)")
    print(f"  Transaction cost: {RT_COST_NTD} NTD per round-trip")
    print("-" * 72)
    print(f"  {'Metric':<30} {'Baseline':>18} {'VPIN-Adjusted':>18}")
    print("-" * 72)
    print(
        f"  {'Total PnL (NTD)':<30} {baseline.total_pnl_ntd:>18,.2f}"
        f" {vpin_result.total_pnl_ntd:>18,.2f}"
    )
    print(
        f"  {'Sharpe Ratio':<30} {baseline.sharpe:>18.4f}"
        f" {vpin_result.sharpe:>18.4f}"
    )
    print(
        f"  {'Max Drawdown (NTD)':<30} {baseline.max_drawdown_ntd:>18,.2f}"
        f" {vpin_result.max_drawdown_ntd:>18,.2f}"
    )
    print(
        f"  {'Total Fills':<30} {baseline.n_fills:>18,}"
        f" {vpin_result.n_fills:>18,}"
    )
    print(
        f"  {'Buys / Sells':<30} {baseline.n_buys:>8,} / {baseline.n_sells:<8,}"
        f" {vpin_result.n_buys:>8,} / {vpin_result.n_sells:<8,}"
    )
    print("-" * 72)
    print(f"  {'PnL Delta (NTD)':<30} {pnl_delta:>18,.2f}")
    print(f"  {'Sharpe Delta':<30} {sharpe_delta:>18.4f}")
    print(f"  {'Drawdown Reduction':<30} {dd_reduction_pct:>17.2f}%")
    print("-" * 72)
    print("  REGIME STATISTICS")
    print("-" * 72)
    for regime_name, pct in regime_stats["regime_fractions_pct"].items():
        count = regime_stats["regime_counts"][regime_name]
        print(f"    {regime_name:<12} {pct:>6.2f}%  ({count:>10,} ticks)")
    print(f"    Transitions: {regime_stats['n_transitions']:,}")
    print(
        f"    Thresholds: elevated={regime_stats['threshold_elevated']:.4f}"
        f"  toxic={regime_stats['threshold_toxic']:.4f}"
    )
    print("-" * 72)
    print("  PnL DURING TOXIC PERIODS")
    print("-" * 72)
    toxic_bl = baseline.pnl_per_regime_ntd.get("TOXIC", 0.0)
    toxic_vp = vpin_result.pnl_per_regime_ntd.get("TOXIC", 0.0)
    print(f"    Baseline:      {toxic_bl:>12,.2f} NTD")
    print(f"    VPIN-Adjusted: {toxic_vp:>12,.2f} NTD")
    if abs(toxic_bl) > 0:
        toxic_reduction = round((1.0 - abs(toxic_vp) / abs(toxic_bl)) * 100.0, 2)
        print(f"    Loss reduction: {toxic_reduction:.2f}%")
    print("=" * 72)

    # --- Verdict ---
    print("\n  VERDICT:")
    if dd_reduction_pct > 0 and sharpe_delta > -0.5:
        print(
            "    POSITIVE: VPIN overlay reduces max drawdown by"
            f" {dd_reduction_pct:.1f}% with acceptable Sharpe impact"
            f" ({sharpe_delta:+.4f})."
        )
        print(
            "    RECOMMENDATION: VPIN regime detection adds value as a"
            " risk overlay for MM strategies."
        )
    elif dd_reduction_pct > 0:
        print(
            "    MIXED: Drawdown reduced but Sharpe impact is significant"
            f" ({sharpe_delta:+.4f})."
        )
        print(
            "    RECOMMENDATION: Consider less aggressive regime scaling"
            " (e.g., TOXIC=2 instead of 1)."
        )
    else:
        print(
            "    NEGATIVE: VPIN overlay did not reduce drawdown."
            " Review regime calibration and latency model."
        )
    print()


if __name__ == "__main__":
    main()
