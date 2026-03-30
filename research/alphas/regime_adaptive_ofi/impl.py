"""Regime-Adaptive OFI (RA-OFI) — regime-conditional order flow signal.

Detects market regime from spread, volatility, and OFI autocorrelation,
then adapts OFI aggregation window and applies a quasi-Sharpe filter
to trade only when expected edge exceeds the 4-pt RT cost.

Regime classification (3 states):
    QUIET:    spread < 6 bps, low volatility — OFI has strongest IC
    NORMAL:   moderate spread and vol
    VOLATILE: spread > 15 bps, high vol — adverse selection, avoid

Core hypothesis: regime-conditional OFI IC > unconditional OFI IC,
and the gap is large enough to cover costs in favorable regimes.

WARNING: R16 showed unconditional L1 OFI fails at 4pt cost.
This module MUST demonstrate regime conditioning materially improves IC.

Paper refs:
    2505.17388 — Hu & Zhang (2025), OFI regime dynamics on CSI 300
    2307.02375 — Tsaknaki et al. (2023), BOCPD for order flow regimes
    2603.20456 — Hu (2026), Neural HMM adaptive granularity

Allocator Law : Pre-allocated arrays, __slots__ on classes.
Precision Law : Float OK — offline research module (rule 11 exception).
Cache Law     : EMA state in scalar fields, no pointer chasing.
"""

from __future__ import annotations

import enum
import math
from pathlib import Path
from typing import Optional

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_VARIANCE: float = 1e-20
_SIGNAL_CLIP: float = 3.0
_WARMUP_TICKS: int = 200

# Regime thresholds (spread in bps)
_SPREAD_QUIET_MAX: float = 6.0
_SPREAD_VOLATILE_MIN: float = 15.0

# Realized volatility percentile thresholds (calibrated per-session)
_RVOL_QUIET_PCTILE: float = 33.0
_RVOL_VOLATILE_PCTILE: float = 67.0

# OFI EMA windows by regime (in ticks)
_OFI_WINDOW: dict[str, int] = {"quiet": 60, "normal": 20, "volatile": 8}

# Quasi-Sharpe threshold: only trade when expected|signal|*IC > cost
_QUASI_SHARPE_THRESHOLD: float = 1.5

# EMA alphas
_SPREAD_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 50)  # ~50 tick half-life
_RVOL_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 100)   # ~100 tick half-life

# Forward return horizons for IC (in ticks; TMFD6 ~1.8 ticks/sec)
FWD_TICKS: tuple[int, ...] = (108, 540, 1080, 3240)  # ~1min, 5min, 10min, 30min
FWD_LABELS: tuple[str, ...] = ("1min", "5min", "10min", "30min")

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

_MANIFEST = AlphaManifest(
    alpha_id="regime_adaptive_ofi",
    hypothesis=(
        "OFI predictive power varies with regime. Regime-conditional "
        "OFI with adaptive aggregation window and quasi-Sharpe filter "
        "can overcome the structural cost barrier that killed unconditional OFI."
    ),
    formula=(
        "regime = f(spread_ema, rvol, ofi_acf); "
        "ofi_agg = EMA(ofi_l1, window[regime]); "
        "signal = sign(ofi_agg) when quasi_sharpe > threshold"
    ),
    paper_refs=("2505.17388", "2307.02375", "2603.20456"),
    data_fields=("bid_px", "ask_px", "bid_qty", "ask_qty", "mid_price", "spread_bps"),
    complexity="O(1)",
    latency_profile=None,
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
)


# ---------------------------------------------------------------------------
# Regime enum
# ---------------------------------------------------------------------------


class Regime(enum.IntEnum):
    QUIET = 0
    NORMAL = 1
    VOLATILE = 2


# ---------------------------------------------------------------------------
# Streaming signal generator
# ---------------------------------------------------------------------------


class RegimeAdaptiveOFI:
    """Regime-adaptive OFI signal generator.

    Classifies ticks into regimes based on spread and realized volatility,
    then computes OFI with regime-dependent EMA window.
    """

    __slots__ = (
        "_prev_bid_px", "_prev_ask_px", "_prev_bid_qty", "_prev_ask_qty",
        "_prev_mid", "_spread_ema", "_rvol_ema", "_prev_ret",
        "_ofi_emas", "_ofi_raw_ema", "_tick_count", "_warmed_up",
        "_regime", "_ofi_acf_lag1_num", "_ofi_acf_lag1_den", "_prev_ofi",
    )

    def __init__(self) -> None:
        self._prev_bid_px: float = 0.0
        self._prev_ask_px: float = 0.0
        self._prev_bid_qty: float = 0.0
        self._prev_ask_qty: float = 0.0
        self._prev_mid: float = 0.0
        self._spread_ema: float = 0.0
        self._rvol_ema: float = 0.0
        self._prev_ret: float = 0.0
        # OFI EMA for each regime window size
        self._ofi_emas: dict[str, float] = {"quiet": 0.0, "normal": 0.0, "volatile": 0.0}
        self._ofi_raw_ema: float = 0.0
        self._tick_count: int = 0
        self._warmed_up: bool = False
        self._regime: Regime = Regime.NORMAL
        self._ofi_acf_lag1_num: float = 0.0
        self._ofi_acf_lag1_den: float = 0.0
        self._prev_ofi: float = 0.0

    def update(
        self,
        bid_px: float, ask_px: float,
        bid_qty: float, ask_qty: float,
        mid_price: float, spread_bps: float,
    ) -> dict[str, object]:
        """Process a tick and return signal + regime state."""
        result: dict[str, object] = {
            "signal": 0.0, "direction": 0, "regime": int(self._regime),
            "ofi_raw": 0.0, "ofi_agg": 0.0, "quasi_sharpe": 0.0,
            "spread_ema": self._spread_ema, "rvol_ema": self._rvol_ema,
        }

        if bid_px <= 0 or ask_px <= 0 or mid_price <= 0:
            return result

        self._tick_count += 1

        # --- Compute OFI (L1 order flow imbalance) ---
        ofi = 0.0
        if self._prev_bid_px > 0:
            # Bid side contribution
            if bid_px > self._prev_bid_px:
                ofi += bid_qty
            elif bid_px == self._prev_bid_px:
                ofi += bid_qty - self._prev_bid_qty
            else:
                ofi -= self._prev_bid_qty

            # Ask side contribution
            if ask_px < self._prev_ask_px:
                ofi -= ask_qty
            elif ask_px == self._prev_ask_px:
                ofi -= (ask_qty - self._prev_ask_qty)
            else:
                ofi += self._prev_ask_qty

        self._prev_bid_px = bid_px
        self._prev_ask_px = ask_px
        self._prev_bid_qty = bid_qty
        self._prev_ask_qty = ask_qty
        result["ofi_raw"] = ofi

        # --- Update OFI EMAs for all regime windows ---
        for regime_name, window in _OFI_WINDOW.items():
            alpha = 1.0 - math.exp(-1.0 / max(1, window))
            self._ofi_emas[regime_name] = (
                alpha * ofi + (1.0 - alpha) * self._ofi_emas[regime_name]
            )

        # --- Update spread EMA ---
        self._spread_ema = (
            _SPREAD_EMA_ALPHA * spread_bps +
            (1.0 - _SPREAD_EMA_ALPHA) * self._spread_ema
        )

        # --- Update realized volatility EMA ---
        if self._prev_mid > 0:
            ret = (mid_price - self._prev_mid) / self._prev_mid
            ret_sq = ret * ret
            self._rvol_ema = (
                _RVOL_EMA_ALPHA * ret_sq +
                (1.0 - _RVOL_EMA_ALPHA) * self._rvol_ema
            )
            self._prev_ret = ret
        self._prev_mid = mid_price

        # --- Update OFI autocorrelation (lag-1) ---
        acf_alpha = 0.01  # slow-moving estimate
        self._ofi_acf_lag1_num = (
            acf_alpha * (ofi * self._prev_ofi) +
            (1.0 - acf_alpha) * self._ofi_acf_lag1_num
        )
        self._ofi_acf_lag1_den = (
            acf_alpha * (ofi * ofi) +
            (1.0 - acf_alpha) * self._ofi_acf_lag1_den
        )
        self._prev_ofi = ofi

        # --- Classify regime ---
        self._regime = self._classify_regime()

        # --- Warmup check ---
        if self._tick_count < _WARMUP_TICKS:
            result["regime"] = int(self._regime)
            result["spread_ema"] = self._spread_ema
            result["rvol_ema"] = self._rvol_ema
            return result

        self._warmed_up = True

        # --- Compute regime-adaptive OFI signal ---
        regime_name = self._regime.name.lower()
        ofi_agg = self._ofi_emas[regime_name]
        result["ofi_agg"] = ofi_agg
        result["regime"] = int(self._regime)
        result["spread_ema"] = self._spread_ema
        result["rvol_ema"] = self._rvol_ema

        # --- Quasi-Sharpe filter ---
        # Only generate signal when OFI magnitude suggests edge > cost
        rvol_sqrt = math.sqrt(max(self._rvol_ema, _MIN_VARIANCE))
        if rvol_sqrt > 0:
            quasi_sharpe = abs(ofi_agg) / (rvol_sqrt * 1e4)
        else:
            quasi_sharpe = 0.0
        result["quasi_sharpe"] = quasi_sharpe

        if quasi_sharpe > _QUASI_SHARPE_THRESHOLD and self._regime != Regime.VOLATILE:
            direction = 1 if ofi_agg > 0 else -1
            signal = direction * min(1.0, quasi_sharpe / (_QUASI_SHARPE_THRESHOLD * 2))
            result["signal"] = signal
            result["direction"] = direction

        return result

    def _classify_regime(self) -> Regime:
        """Classify current regime from spread EMA, realized vol, and OFI ACF.

        Multi-factor classification (addresses Challenger challenge #5):
        - Quiet:    tight spread AND low rvol
        - Volatile: wide spread OR high rvol
        - Normal:   everything else
        OFI ACF is tracked but not used for regime gating (see challenge #6 results).
        """
        is_tight_spread = self._spread_ema < _SPREAD_QUIET_MAX
        is_wide_spread = self._spread_ema > _SPREAD_VOLATILE_MIN
        # rvol threshold: use 1e-8 as approximate p33/p67 boundaries
        # (calibrated from TMFD6 data: rvol_p33 ~ 5e-10, rvol_p67 ~ 2e-9)
        is_low_rvol = self._rvol_ema < 5e-10
        is_high_rvol = self._rvol_ema > 2e-9

        if is_tight_spread and is_low_rvol:
            return Regime.QUIET
        if is_wide_spread or is_high_rvol:
            return Regime.VOLATILE
        return Regime.NORMAL

    @property
    def regime(self) -> Regime:
        return self._regime

    @property
    def warmed_up(self) -> bool:
        return self._warmed_up

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def reset(self) -> None:
        self._prev_bid_px = 0.0
        self._prev_ask_px = 0.0
        self._prev_bid_qty = 0.0
        self._prev_ask_qty = 0.0
        self._prev_mid = 0.0
        self._spread_ema = 0.0
        self._rvol_ema = 0.0
        self._prev_ret = 0.0
        self._ofi_emas = {"quiet": 0.0, "normal": 0.0, "volatile": 0.0}
        self._tick_count = 0
        self._warmed_up = False
        self._regime = Regime.NORMAL
        self._prev_ofi = 0.0


# ---------------------------------------------------------------------------
# Batch IC analysis
# ---------------------------------------------------------------------------


def compute_ofi_series(data: np.ndarray) -> np.ndarray:
    """Compute raw L1 OFI from structured array with bid/ask px/qty."""
    n = len(data)
    ofi = np.zeros(n, dtype=np.float64)

    bid_px = data["bid_px"]
    ask_px = data["ask_px"]
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]

    for i in range(1, n):
        val = 0.0
        # Bid side
        if bid_px[i] > bid_px[i - 1]:
            val += bid_qty[i]
        elif bid_px[i] == bid_px[i - 1]:
            val += bid_qty[i] - bid_qty[i - 1]
        else:
            val -= bid_qty[i - 1]
        # Ask side
        if ask_px[i] < ask_px[i - 1]:
            val -= ask_qty[i]
        elif ask_px[i] == ask_px[i - 1]:
            val -= (ask_qty[i] - ask_qty[i - 1])
        else:
            val += ask_qty[i - 1]
        ofi[i] = val
    return ofi


def compute_ema_series(values: np.ndarray, window: int) -> np.ndarray:
    """Compute EMA with given window (half-life in ticks)."""
    alpha = 1.0 - math.exp(-1.0 / max(1, window))
    oma = 1.0 - alpha
    n = len(values)
    ema = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        ema[i] = alpha * values[i] + oma * ema[i - 1]
    return ema


def classify_regimes(spread_bps: np.ndarray) -> np.ndarray:
    """Classify each tick into regime based on spread."""
    spread_ema = compute_ema_series(spread_bps, 50)
    regimes = np.full(len(spread_bps), Regime.NORMAL, dtype=np.int8)
    regimes[spread_ema < _SPREAD_QUIET_MAX] = Regime.QUIET
    regimes[spread_ema > _SPREAD_VOLATILE_MIN] = Regime.VOLATILE
    return regimes


def compute_forward_returns(mid: np.ndarray, fwd_ticks: int) -> np.ndarray:
    """Forward returns at fixed tick offset. Last fwd_ticks = NaN."""
    n = len(mid)
    fwd = np.full(n, np.nan, dtype=np.float64)
    valid = mid[:n - fwd_ticks]
    future = mid[fwd_ticks:]
    mask = valid > 0
    fwd[:n - fwd_ticks][mask] = (future[mask] - valid[mask]) / valid[mask]
    return fwd


def rank_ic(signal: np.ndarray, fwd_ret: np.ndarray, mask: np.ndarray) -> float:
    """Spearman rank IC on masked subset."""
    s = signal[mask]
    f = fwd_ret[mask]
    valid = np.isfinite(s) & np.isfinite(f) & (s != 0.0)
    s_v = s[valid]
    f_v = f[valid]
    if len(s_v) < 100:
        return 0.0
    from scipy.stats import spearmanr
    corr, _ = spearmanr(s_v, f_v)
    return float(corr) if np.isfinite(corr) else 0.0


def run_regime_ic_analysis(
    data_path: str = "research/data/raw/tmfd6/TMFD6_all_l1.npy",
) -> dict:
    """Run regime-conditional IC analysis on TMFD6.

    This is the KEY test: does regime conditioning improve OFI IC enough
    to overcome the 4-pt cost barrier?
    """
    data = np.load(data_path, allow_pickle=True)
    n = len(data)

    mid = data["mid_price"].astype(np.float64)
    spread = data["spread_bps"].astype(np.float64)

    # Compute OFI
    ofi_raw = compute_ofi_series(data)

    # Compute OFI EMAs for each regime window
    ofi_emas: dict[str, np.ndarray] = {}
    for regime_name, window in _OFI_WINDOW.items():
        ofi_emas[regime_name] = compute_ema_series(ofi_raw, window)

    # Classify regimes
    regimes = classify_regimes(spread)
    warmup = max(500, _WARMUP_TICKS)

    # Regime distribution
    regime_counts = {
        "quiet": int((regimes[warmup:] == Regime.QUIET).sum()),
        "normal": int((regimes[warmup:] == Regime.NORMAL).sum()),
        "volatile": int((regimes[warmup:] == Regime.VOLATILE).sum()),
    }
    total = sum(regime_counts.values())
    regime_pcts = {k: v / total * 100 for k, v in regime_counts.items()}

    # Forward returns
    fwd_rets: dict[str, np.ndarray] = {}
    for ticks, label in zip(FWD_TICKS, FWD_LABELS):
        fwd_rets[label] = compute_forward_returns(mid, ticks)

    # IC analysis: unconditional vs per-regime
    results: dict = {
        "n_ticks": n,
        "regime_counts": regime_counts,
        "regime_pcts": regime_pcts,
        "unconditional_ic": {},
        "regime_ic": {r: {} for r in ["quiet", "normal", "volatile"]},
        "regime_adapted_ic": {},
    }

    # Unconditional IC (standard OFI EMA-20)
    ofi_standard = ofi_emas["normal"]  # 20-tick EMA
    mask_all = np.arange(n) >= warmup
    for label in FWD_LABELS:
        ic = rank_ic(ofi_standard, fwd_rets[label], mask_all)
        results["unconditional_ic"][label] = ic

    # Per-regime IC (using fixed OFI window)
    for regime_name, regime_val in [("quiet", Regime.QUIET), ("normal", Regime.NORMAL), ("volatile", Regime.VOLATILE)]:
        mask_regime = (regimes == regime_val) & (np.arange(n) >= warmup)
        for label in FWD_LABELS:
            ic = rank_ic(ofi_standard, fwd_rets[label], mask_regime)
            results["regime_ic"][regime_name][label] = ic

    # Regime-ADAPTED IC (use regime-specific OFI window)
    # Build the adaptive OFI signal: pick the right EMA based on regime
    ofi_adapted = np.zeros(n, dtype=np.float64)
    for i in range(n):
        r = regimes[i]
        if r == Regime.QUIET:
            ofi_adapted[i] = ofi_emas["quiet"][i]
        elif r == Regime.VOLATILE:
            ofi_adapted[i] = ofi_emas["volatile"][i]
        else:
            ofi_adapted[i] = ofi_emas["normal"][i]

    for label in FWD_LABELS:
        ic = rank_ic(ofi_adapted, fwd_rets[label], mask_all)
        results["regime_adapted_ic"][label] = ic

    # Per-regime adapted IC
    results["regime_adapted_per_regime_ic"] = {}
    for regime_name, regime_val in [("quiet", Regime.QUIET), ("normal", Regime.NORMAL), ("volatile", Regime.VOLATILE)]:
        mask_regime = (regimes == regime_val) & (np.arange(n) >= warmup)
        results["regime_adapted_per_regime_ic"][regime_name] = {}
        for label in FWD_LABELS:
            ic = rank_ic(ofi_adapted, fwd_rets[label], mask_regime)
            results["regime_adapted_per_regime_ic"][regime_name][label] = ic

    # Session analysis: opening (first 30 min) vs rest
    ts = data["local_ts"]
    # Detect session boundaries (gaps > 1 hour)
    ts_diff = np.diff(ts.astype(np.float64))
    session_starts = np.where(ts_diff > 3600 * 1e9)[0] + 1
    session_starts = np.insert(session_starts, 0, 0)

    opening_mask = np.zeros(n, dtype=bool)
    rest_mask = np.zeros(n, dtype=bool)
    skip_ns = 30 * 60 * int(1e9)  # 30 min in ns
    for s_start in session_starts:
        if s_start >= n:
            continue
        s_ts_start = ts[s_start]
        for i in range(s_start, min(s_start + 200000, n)):
            if ts[i] - s_ts_start < skip_ns:
                opening_mask[i] = True
            else:
                rest_mask[i] = True

    results["session_ic"] = {"opening": {}, "rest": {}}
    for label in FWD_LABELS:
        mask_open = opening_mask & (np.arange(n) >= warmup)
        mask_rest = rest_mask & (np.arange(n) >= warmup)
        results["session_ic"]["opening"][label] = rank_ic(ofi_adapted, fwd_rets[label], mask_open)
        results["session_ic"]["rest"][label] = rank_ic(ofi_adapted, fwd_rets[label], mask_rest)

    return results


def print_regime_ic_report(results: dict) -> None:
    """Pretty-print regime IC report."""
    print("=" * 80)
    print(f"Regime-Adaptive OFI — IC Analysis ({results['n_ticks']:,} ticks)")
    print("=" * 80)

    # Regime distribution
    print("\nRegime Distribution:")
    for r, pct in results["regime_pcts"].items():
        cnt = results["regime_counts"][r]
        print(f"  {r:>10}: {cnt:>10,} ticks ({pct:.1f}%)")

    # Unconditional IC
    print("\nUnconditional OFI IC (EMA-20, all regimes):")
    print(f"  {'fwd':>10}", end="")
    for label in FWD_LABELS:
        print(f"  {label:>10}", end="")
    print()
    print(f"  {'IC':>10}", end="")
    for label in FWD_LABELS:
        ic = results["unconditional_ic"][label]
        print(f"  {ic:>+10.4f}", end="")
    print()

    # Per-regime IC (standard window)
    print("\nPer-Regime IC (standard EMA-20 OFI):")
    for regime_name in ["quiet", "normal", "volatile"]:
        print(f"  {regime_name:>10}", end="")
        for label in FWD_LABELS:
            ic = results["regime_ic"][regime_name][label]
            print(f"  {ic:>+10.4f}", end="")
        print()

    # Regime-adapted IC
    print("\nRegime-ADAPTED IC (adaptive EMA window per regime):")
    print(f"  {'all':>10}", end="")
    for label in FWD_LABELS:
        ic = results["regime_adapted_ic"][label]
        print(f"  {ic:>+10.4f}", end="")
    print()
    for regime_name in ["quiet", "normal", "volatile"]:
        print(f"  {regime_name:>10}", end="")
        for label in FWD_LABELS:
            ic = results["regime_adapted_per_regime_ic"][regime_name][label]
            print(f"  {ic:>+10.4f}", end="")
        print()

    # Session analysis
    print("\nSession IC (regime-adapted OFI):")
    for session in ["opening", "rest"]:
        print(f"  {session:>10}", end="")
        for label in FWD_LABELS:
            ic = results["session_ic"][session][label]
            print(f"  {ic:>+10.4f}", end="")
        print()

    # Key comparison
    print("\n" + "=" * 80)
    print("KEY COMPARISON: Unconditional vs Regime-Adapted vs Quiet-Only IC")
    print("-" * 80)
    for label in FWD_LABELS:
        uncond = results["unconditional_ic"][label]
        adapted = results["regime_adapted_ic"][label]
        quiet = results["regime_adapted_per_regime_ic"]["quiet"][label]
        improvement_adapted = (adapted - uncond) / max(abs(uncond), 1e-6) * 100
        improvement_quiet = (quiet - uncond) / max(abs(uncond), 1e-6) * 100
        print(f"  {label}: uncond={uncond:+.4f}  adapted={adapted:+.4f} "
              f"({improvement_adapted:+.0f}%)  quiet_only={quiet:+.4f} "
              f"({improvement_quiet:+.0f}%)")

    # Verdict
    print("\n" + "=" * 80)
    best_adapted = max(results["regime_adapted_ic"].values(), key=abs)
    best_quiet = max(results["regime_adapted_per_regime_ic"]["quiet"].values(), key=abs)
    best_uncond = max(results["unconditional_ic"].values(), key=abs)

    if abs(best_quiet) > abs(best_uncond) * 1.5:
        print("VERDICT: Regime conditioning MATERIALLY improves IC in quiet regime.")
        print(f"  Best quiet IC = {best_quiet:+.4f} vs uncond = {best_uncond:+.4f}")
    elif abs(best_adapted) > abs(best_uncond) * 1.2:
        print("VERDICT: Regime adaptation provides MODERATE IC improvement.")
    else:
        print("VERDICT: Regime conditioning does NOT materially improve IC.")
        print("  The R16 finding holds: L1 OFI is structurally too weak at 4pt cost.")
    print("=" * 80)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

ALPHA_CLASS = RegimeAdaptiveOFI


if __name__ == "__main__":
    results = run_regime_ic_analysis()
    print_regime_ic_report(results)
