"""Multi-Timescale Trend Reversion (MSTR) — alpha signal + IC analysis.

Computes standardized trend strength phi at multiple horizons and generates
contrarian signals based on the Schmidhuber cubic model:
    E[R(t+1)] = a + b*phi(t) + c*phi(t)^3

Key regimes (Safari & Schmidhuber 2025):
    T < 15 min:  reversion (weak trends revert, strong persist)
    T > 30 min:  trending  (weak persist, strong revert)
    Critical threshold: phi_c = sqrt(-b/(3c))  (where f'(phi)=0)

This module provides:
    1. MultiscaleTrendReversion — streaming signal generator (tick-by-tick)
    2. compute_ic_analysis()    — batch IC computation on numpy arrays
    3. run_ic_on_tmfd6()        — full IC analysis on local TMFD6 .npy files

Allocator Law : Pre-allocated numpy arrays for batch computation.
Precision Law : Float OK — offline research module (rule 11 exception).
Cache Law     : EMA state in contiguous arrays per horizon.

Paper refs:
    2501.16772 — Safari & Schmidhuber (2025)
    2006.07847 — Schmidhuber (2020)
    2505.17388 — Hu & Zhang (2025)
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HORIZONS_MIN: tuple[int, ...] = (2, 4, 8, 16, 32, 64)

# Paper coefficients from Safari & Schmidhuber 2025, Figure 5 (approximate)
_B_TILDE: dict[int, float] = {
    2: -0.0050, 4: -0.0040, 8: -0.0025,
    16: -0.0010, 32: +0.0005, 64: +0.0010,
}
_C_TILDE: dict[int, float] = {
    2: +0.0015, 4: +0.0012, 8: +0.0008,
    16: +0.0003, 32: -0.0002, 64: -0.0003,
}

# Critical thresholds: phi_c where dE/dphi = b + 3c*phi^2 = 0
# => phi_c = sqrt(-b / (3c))
_PHI_C: dict[int, float] = {}
for _h in HORIZONS_MIN:
    _b, _c = _B_TILDE[_h], _C_TILDE[_h]
    _PHI_C[_h] = math.sqrt(abs(_b / (3.0 * _c))) if _b * _c < 0 else 2.0

_WARMUP_TICKS: int = 256
_SIGNAL_CLIP: float = 5.0
_MIN_VARIANCE: float = 1e-20

# Forward return horizons for IC analysis (in minutes)
FWD_HORIZONS_MIN: tuple[int, ...] = (1, 5, 10, 30)

# Session skip: first 30 min of session (opening momentum period)
SESSION_SKIP_NS: int = 30 * 60 * 1_000_000_000

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

_MANIFEST = AlphaManifest(
    alpha_id="multiscale_trend_reversion",
    hypothesis=(
        "Intraday trends revert following a universal cubic scaling law. "
        "Multi-horizon phi monitoring with adaptive thresholds captures "
        "the reversion regime at 5-60 min horizons where 36ms RTT is irrelevant "
        "and 1.33 bps cost can be covered."
    ),
    formula=(
        "phi_T = EMA_T(R) / sigma_T * sqrt(N_eff); "
        "E[R] = a + b*phi + c*phi^3; "
        "signal = -sign(phi) when |phi| > phi_c"
    ),
    paper_refs=("2501.16772", "2006.07847", "2505.17388"),
    data_fields=("mid_price",),
    complexity="O(1)",
    latency_profile=None,
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
)

# ---------------------------------------------------------------------------
# Streaming signal generator
# ---------------------------------------------------------------------------


class _HorizonState:
    """Per-horizon EMA state for trend strength computation."""

    __slots__ = (
        "horizon_min", "ema_alpha", "ema_ret", "ema_ret_sq",
        "phi", "phi_c", "b_tilde", "c_tilde", "tick_count",
    )

    def __init__(
        self, horizon_min: int, tick_rate: float = 1.8,
        phi_c: float = 2.0, b_tilde: float = 0.0, c_tilde: float = 0.0,
    ) -> None:
        self.horizon_min = horizon_min
        n_ticks = max(1, int(horizon_min * 60 * tick_rate))
        self.ema_alpha = 1.0 - math.exp(-1.0 / n_ticks)
        self.ema_ret = 0.0
        self.ema_ret_sq = 0.0
        self.phi = 0.0
        self.phi_c = phi_c
        self.b_tilde = b_tilde
        self.c_tilde = c_tilde
        self.tick_count = 0

    def update(self, log_return: float) -> float:
        self.tick_count += 1
        a = self.ema_alpha
        self.ema_ret = a * log_return + (1.0 - a) * self.ema_ret
        self.ema_ret_sq = a * (log_return * log_return) + (1.0 - a) * self.ema_ret_sq

        var = self.ema_ret_sq - self.ema_ret * self.ema_ret
        if var < _MIN_VARIANCE:
            self.phi = 0.0
            return 0.0

        n_eff = min(self.tick_count, int(1.0 / self.ema_alpha))
        self.phi = self.ema_ret / math.sqrt(var) * math.sqrt(n_eff)
        self.phi = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self.phi))
        return self.phi

    def expected_return(self) -> float:
        p = self.phi
        return self.b_tilde * p + self.c_tilde * p * p * p

    def is_above_critical(self) -> bool:
        return abs(self.phi) > self.phi_c

    def reset(self) -> None:
        self.ema_ret = 0.0
        self.ema_ret_sq = 0.0
        self.phi = 0.0
        self.tick_count = 0


class MultiscaleTrendReversion:
    """Multi-horizon trend reversion signal generator.

    Parameters
    ----------
    horizons_min : tuple of int
        Trend horizons in minutes.
    tick_rate : float
        Expected ticks per second (TMFD6 ~ 1.8).
    actionable_horizons : tuple of int, optional
        Which horizons generate trading signals. Default: (16, 32, 64).
    """

    __slots__ = ("_horizons", "_prev_mid", "_tick_count", "_actionable", "_warmed_up")

    def __init__(
        self,
        horizons_min: tuple[int, ...] = HORIZONS_MIN,
        tick_rate: float = 1.8,
        actionable_horizons: Optional[tuple[int, ...]] = None,
    ) -> None:
        self._horizons: dict[int, _HorizonState] = {}
        for h in horizons_min:
            self._horizons[h] = _HorizonState(
                horizon_min=h,
                tick_rate=tick_rate,
                phi_c=_PHI_C.get(h, 2.0),
                b_tilde=_B_TILDE.get(h, 0.0),
                c_tilde=_C_TILDE.get(h, 0.0),
            )
        self._prev_mid: float = 0.0
        self._tick_count: int = 0
        self._actionable: tuple[int, ...] = actionable_horizons or (16, 32, 64)
        self._warmed_up: bool = False

    def update(self, mid_price: float) -> dict[str, object]:
        """Process a mid_price tick and return signal state."""
        result: dict[str, object] = {
            "signal": 0.0, "direction": 0, "trigger_horizon": None,
            "phi": {}, "expected_return": {}, "above_critical": {},
        }
        if mid_price <= 0.0 or self._prev_mid <= 0.0:
            self._prev_mid = mid_price if mid_price > 0.0 else self._prev_mid
            return result

        log_ret = (mid_price - self._prev_mid) / self._prev_mid
        self._prev_mid = mid_price
        self._tick_count += 1

        if self._tick_count < _WARMUP_TICKS:
            for hs in self._horizons.values():
                hs.update(log_ret)
            return result

        self._warmed_up = True
        phi_d: dict[int, float] = {}
        er_d: dict[int, float] = {}
        crit_d: dict[int, bool] = {}

        for h, hs in self._horizons.items():
            phi_d[h] = hs.update(log_ret)
            er_d[h] = hs.expected_return()
            crit_d[h] = hs.is_above_critical()

        result["phi"] = phi_d
        result["expected_return"] = er_d
        result["above_critical"] = crit_d

        # Pick longest actionable horizon that crossed critical
        for h in sorted(self._actionable, reverse=True):
            if crit_d.get(h, False):
                phi_val = phi_d[h]
                direction = -1 if phi_val > 0 else 1
                hs = self._horizons[h]
                excess = abs(phi_val) - hs.phi_c
                signal = direction * min(1.0, excess / max(hs.phi_c, 0.01))
                result["signal"] = signal
                result["direction"] = direction
                result["trigger_horizon"] = h
                break

        return result

    def get_phi(self, horizon_min: int) -> float:
        hs = self._horizons.get(horizon_min)
        return hs.phi if hs else 0.0

    def get_all_phi(self) -> dict[int, float]:
        return {h: s.phi for h, s in self._horizons.items()}

    def reset(self) -> None:
        self._prev_mid = 0.0
        self._tick_count = 0
        self._warmed_up = False
        for hs in self._horizons.values():
            hs.reset()

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    @property
    def warmed_up(self) -> bool:
        return self._warmed_up


# ---------------------------------------------------------------------------
# Batch IC analysis (numpy-vectorized)
# ---------------------------------------------------------------------------


def compute_phi_series(
    mid_prices: np.ndarray,
    horizon_min: int,
    tick_rate: float = 1.8,
) -> np.ndarray:
    """Compute phi (trend strength) series from mid_price array.

    Parameters
    ----------
    mid_prices : 1-D array of mid prices (float)
    horizon_min : horizon in minutes
    tick_rate : ticks/sec

    Returns
    -------
    phi : array same length as mid_prices (first element = 0)
    """
    n = len(mid_prices)
    phi = np.zeros(n, dtype=np.float64)
    if n < 2:
        return phi

    # Compute returns
    returns = np.diff(mid_prices) / np.where(mid_prices[:-1] > 0, mid_prices[:-1], 1.0)

    n_ticks = max(1, int(horizon_min * 60 * tick_rate))
    alpha = 1.0 - math.exp(-1.0 / n_ticks)
    oma = 1.0 - alpha

    ema_ret = 0.0
    ema_ret_sq = 0.0

    for i in range(len(returns)):
        r = returns[i]
        ema_ret = alpha * r + oma * ema_ret
        ema_ret_sq = alpha * (r * r) + oma * ema_ret_sq

        var = ema_ret_sq - ema_ret * ema_ret
        if var < _MIN_VARIANCE or i < 10:
            phi[i + 1] = 0.0
            continue

        n_eff = min(i + 1, n_ticks)
        val = ema_ret / math.sqrt(var) * math.sqrt(n_eff)
        phi[i + 1] = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, val))

    return phi


def compute_forward_returns(
    mid_prices: np.ndarray,
    fwd_ticks: int,
) -> np.ndarray:
    """Compute forward returns at a fixed tick offset.

    Returns array of same length as mid_prices; last fwd_ticks elements = NaN.
    """
    n = len(mid_prices)
    fwd = np.full(n, np.nan, dtype=np.float64)
    valid = mid_prices[:n - fwd_ticks]
    future = mid_prices[fwd_ticks:]
    mask = valid > 0
    fwd[:n - fwd_ticks][mask] = (future[mask] - valid[mask]) / valid[mask]
    return fwd


def rank_ic(signal: np.ndarray, forward_ret: np.ndarray, warmup: int = 500) -> float:
    """Compute Spearman rank IC between signal and forward returns.

    Skips NaN and warmup period.
    """
    s = signal[warmup:]
    f = forward_ret[warmup:]
    mask = np.isfinite(s) & np.isfinite(f) & (s != 0.0)
    s_valid = s[mask]
    f_valid = f[mask]
    if len(s_valid) < 100:
        return 0.0

    # Rank-based correlation (Spearman)
    from scipy.stats import spearmanr
    corr, _ = spearmanr(s_valid, f_valid)
    return float(corr) if np.isfinite(corr) else 0.0


def fit_cubic(
    phi: np.ndarray,
    fwd_ret: np.ndarray,
    warmup: int = 500,
) -> dict[str, float]:
    """Fit E[R] = a + b*phi + c*phi^3 and return coefficients + R^2."""
    s = phi[warmup:]
    f = fwd_ret[warmup:]
    mask = np.isfinite(s) & np.isfinite(f) & (s != 0.0)
    x = s[mask]
    y = f[mask]
    if len(x) < 100:
        return {"a": 0.0, "b": 0.0, "c": 0.0, "r_squared": 0.0, "phi_c": float("inf"), "n": 0}

    X = np.column_stack([np.ones_like(x), x, x**3])
    try:
        beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return {"a": 0.0, "b": 0.0, "c": 0.0, "r_squared": 0.0, "phi_c": float("inf"), "n": 0}

    a, b, c = beta
    y_pred = X @ beta
    ss_res = ((y - y_pred) ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    r_sq = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    # Critical point: dE/dphi = b + 3c*phi^2 = 0 => phi_c = sqrt(-b/(3c))
    phi_c = math.sqrt(abs(b / (3.0 * c))) if b * c < 0 else float("inf")

    return {"a": float(a), "b": float(b), "c": float(c),
            "r_squared": float(r_sq), "phi_c": float(phi_c), "n": len(x)}


def compute_ic_analysis(
    mid_prices: np.ndarray,
    tick_rate: float = 1.8,
    warmup: int = 500,
) -> dict:
    """Run full IC analysis on a single day's mid prices.

    Returns dict with:
        - phi_stats: {horizon: {mean, std, p5, p95, frac_above_1.5, ...}}
        - ic_table:  {(phi_horizon, fwd_horizon): rank_ic}
        - cubic_fit: {(phi_horizon, fwd_horizon): {a, b, c, r_sq, phi_c}}
    """
    results: dict = {"phi_stats": {}, "ic_table": {}, "cubic_fit": {}, "n_ticks": len(mid_prices)}

    # Compute phi series for each horizon
    phi_all: dict[int, np.ndarray] = {}
    for h in HORIZONS_MIN:
        phi = compute_phi_series(mid_prices, h, tick_rate)
        phi_all[h] = phi

        valid = phi[warmup:]
        valid_nz = valid[valid != 0.0]
        if len(valid_nz) > 0:
            results["phi_stats"][h] = {
                "mean": float(valid_nz.mean()),
                "std": float(valid_nz.std()),
                "p5": float(np.percentile(valid_nz, 5)),
                "p95": float(np.percentile(valid_nz, 95)),
                "frac_above_1.5": float((np.abs(valid_nz) > 1.5).mean()),
                "frac_above_2.0": float((np.abs(valid_nz) > 2.0).mean()),
            }

    # Compute forward returns at multiple horizons
    fwd_all: dict[int, np.ndarray] = {}
    for fwd_min in FWD_HORIZONS_MIN:
        fwd_ticks = max(1, int(fwd_min * 60 * tick_rate))
        fwd_all[fwd_min] = compute_forward_returns(mid_prices, fwd_ticks)

    # IC table: rank_ic(phi_h, fwd_k)
    for h in HORIZONS_MIN:
        for fwd_min in FWD_HORIZONS_MIN:
            ic = rank_ic(phi_all[h], fwd_all[fwd_min], warmup)
            results["ic_table"][(h, fwd_min)] = ic

    # Cubic fit for actionable horizons (16, 32, 64 min phi) vs 5/10 min fwd
    for h in (16, 32, 64):
        for fwd_min in (5, 10, 30):
            cf = fit_cubic(phi_all[h], fwd_all[fwd_min], warmup)
            results["cubic_fit"][(h, fwd_min)] = cf

    return results


# ---------------------------------------------------------------------------
# Full analysis on local TMFD6 .npy files
# ---------------------------------------------------------------------------


def load_tmfd6_days(
    data_dir: str = "research/data/raw/tmfd6",
) -> list[tuple[str, np.ndarray]]:
    """Load all per-day TMFD6 .npy files.

    Returns list of (date_str, mid_prices) tuples, sorted by date.
    Excludes the *_all_l1.npy aggregate file.
    """
    p = Path(data_dir)
    files = sorted(p.glob("TMFD6_????-??-??_l1.npy"))
    days: list[tuple[str, np.ndarray]] = []
    for f in files:
        date_str = f.stem.split("_")[1]  # e.g. "2026-01-26"
        data = np.load(f, allow_pickle=True)
        mid = data["mid_price"].astype(np.float64)
        days.append((date_str, mid))
    return days


def run_ic_on_tmfd6(
    data_dir: str = "research/data/raw/tmfd6",
    tick_rate: float = 1.8,
) -> dict:
    """Run IC analysis on all available TMFD6 days.

    Returns aggregated results with per-day and pooled IC values.
    """
    days = load_tmfd6_days(data_dir)
    if not days:
        return {"error": "No TMFD6 data found", "days": []}

    all_ic: dict[tuple[int, int], list[float]] = {}
    all_cubic: dict[tuple[int, int], list[dict]] = {}
    day_results: list[dict] = []

    for date_str, mid in days:
        if len(mid) < 1000:
            continue
        res = compute_ic_analysis(mid, tick_rate)
        res["date"] = date_str
        day_results.append(res)

        for key, ic_val in res["ic_table"].items():
            all_ic.setdefault(key, []).append(ic_val)
        for key, cf in res["cubic_fit"].items():
            all_cubic.setdefault(key, []).append(cf)

    # Pooled IC: mean and std across days
    pooled_ic: dict[tuple[int, int], dict[str, float]] = {}
    for key, ic_list in all_ic.items():
        arr = np.array(ic_list)
        pooled_ic[key] = {
            "mean_ic": float(arr.mean()),
            "std_ic": float(arr.std()),
            "t_stat": float(arr.mean() / (arr.std() / math.sqrt(len(arr)))) if arr.std() > 0 else 0.0,
            "n_days": len(arr),
            "pct_negative": float((arr < 0).mean()),
        }

    # Pooled cubic coefficients
    pooled_cubic: dict[tuple[int, int], dict[str, float]] = {}
    for key, cf_list in all_cubic.items():
        b_arr = np.array([cf["b"] for cf in cf_list])
        c_arr = np.array([cf["c"] for cf in cf_list])
        pooled_cubic[key] = {
            "mean_b": float(b_arr.mean()),
            "mean_c": float(c_arr.mean()),
            "b_t_stat": float(b_arr.mean() / (b_arr.std() / math.sqrt(len(b_arr)))) if b_arr.std() > 0 else 0.0,
            "c_t_stat": float(c_arr.mean() / (c_arr.std() / math.sqrt(len(c_arr)))) if c_arr.std() > 0 else 0.0,
            "n_days": len(cf_list),
        }

    return {
        "n_days": len(day_results),
        "dates": [d["date"] for d in day_results],
        "pooled_ic": pooled_ic,
        "pooled_cubic": pooled_cubic,
        "day_results": day_results,
    }


def print_ic_report(results: dict) -> None:
    """Pretty-print IC analysis results."""
    print("=" * 80)
    print(f"MSTR IC Analysis — {results['n_days']} days of TMFD6 data")
    print("=" * 80)

    # IC table
    print("\nPooled Rank IC (Spearman) — phi_horizon vs forward_return_horizon")
    print(f"{'phi\\fwd':>10}", end="")
    for fwd_min in FWD_HORIZONS_MIN:
        print(f"{'fwd_' + str(fwd_min) + 'min':>14}", end="")
    print()
    print("-" * 70)

    for h in HORIZONS_MIN:
        print(f"{'phi_' + str(h) + 'min':>10}", end="")
        for fwd_min in FWD_HORIZONS_MIN:
            key = (h, fwd_min)
            if key in results["pooled_ic"]:
                info = results["pooled_ic"][key]
                ic_str = f"{info['mean_ic']:+.4f}"
                t_str = f"(t={info['t_stat']:.1f})"
                print(f"{ic_str + ' ' + t_str:>14}", end="")
            else:
                print(f"{'—':>14}", end="")
        print()

    # Cubic fit for actionable horizons
    print("\nCubic Fit: E[R] = a + b*phi + c*phi^3 (pooled across days)")
    print(f"{'phi_h':>8} {'fwd_h':>8} {'mean_b':>12} {'b_t':>8} {'mean_c':>12} {'c_t':>8}")
    print("-" * 60)

    for (ph, fh), info in sorted(results.get("pooled_cubic", {}).items()):
        print(f"{str(ph) + 'min':>8} {str(fh) + 'min':>8} "
              f"{info['mean_b']:>12.6f} {info['b_t_stat']:>8.2f} "
              f"{info['mean_c']:>12.6f} {info['c_t_stat']:>8.2f}")

    # Key finding summary
    print("\n" + "=" * 80)
    print("Key Findings:")

    # Find best IC
    best_key = max(results["pooled_ic"], key=lambda k: abs(results["pooled_ic"][k]["mean_ic"]))
    best = results["pooled_ic"][best_key]
    print(f"  Best IC: phi_{best_key[0]}min vs fwd_{best_key[1]}min = {best['mean_ic']:+.4f} "
          f"(t={best['t_stat']:.1f}, {best['pct_negative']*100:.0f}% negative days)")

    # Check if cubic c coefficient is significant for actionable horizons
    for key in [(32, 10), (64, 10), (32, 30), (64, 30)]:
        if key in results.get("pooled_cubic", {}):
            cf = results["pooled_cubic"][key]
            sig = "***" if abs(cf["c_t_stat"]) > 2.58 else "**" if abs(cf["c_t_stat"]) > 1.96 else ""
            print(f"  Cubic c for phi_{key[0]}min/fwd_{key[1]}min: {cf['mean_c']:.6f} "
                  f"(t={cf['c_t_stat']:.2f}) {sig}")

    print("=" * 80)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

ALPHA_CLASS = MultiscaleTrendReversion


if __name__ == "__main__":
    results = run_ic_on_tmfd6()
    if "error" in results:
        print(f"ERROR: {results['error']}")
    else:
        print_ic_report(results)
