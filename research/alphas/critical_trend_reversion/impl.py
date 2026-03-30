"""Critical Trend Reversion (CTR) — multi-horizon contrarian at z-score threshold.

Generalizes CBS's fixed 40bps/600s threshold to an adaptive, horizon-dependent
z-score threshold based on the Schmidhuber scaling law.

Model (Schmidhuber 2020, Safari & Schmidhuber 2025):
    E[R(t+1)] = a + b * phi(t) + c * phi(t)^3 + epsilon
    where phi(t) = t-statistic of the trend over horizon T

Key regimes from the paper:
    T < 15 min:  b < 0, c > 0  (reversion regime — weak trends revert)
    T > 30 min:  b > 0, c < 0  (trending regime — strong trends revert)
    Critical threshold: phi_c = sqrt(-b/(3c))  # from dE/dphi = b + 3c*phi^2 = 0

For our use (TMFD6 mean-reversion), we focus on the reversion regime
property at longer horizons (30-60 min) where:
    - Weak trends persist (b > 0)
    - Strong trends revert (c < 0)
    - phi_c ~ 1.5-2.0

Signal: Enter contrarian when |phi_T| > phi_c for any horizon T.
Exit: Time-based (hold_ns) or when phi reverts toward zero.

Cost model: TMFD6, 4 pts RT = 1.33 bps. Need ~2.7 bps/trade edge.

Paper refs:
    2006.07847 — Schmidhuber (2020)
    2501.16772 — Safari & Schmidhuber (2025)
    1310.8169  — Bury (2013)

Allocator Law  : Pre-allocated numpy arrays for rolling computations.
Precision Law  : Signal is float (not price accounting). Prices use mid_x2.
Cache Law      : EMA state in contiguous arrays per horizon.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

_MANIFEST = AlphaManifest(
    alpha_id="critical_trend_reversion",
    hypothesis=(
        "Intraday price trends revert when their t-statistic approaches a "
        "critical threshold phi_c = sqrt(-b/c), following Schmidhuber's cubic "
        "polynomial model. Multi-horizon z-score monitoring with adaptive "
        "thresholds generalizes CBS's fixed 40bps trigger."
    ),
    formula=(
        "phi_T(t) = EMA_T(R_hat) / sigma_T; "
        "E[R] = a + b*phi + c*phi^3; "
        "signal = -sign(phi) when |phi| > phi_c = sqrt(-b/(3c))"
    ),
    paper_refs=(
        "2006.07847",
        "2501.16772",
        "1310.8169",
    ),
    data_fields=("mid_price_x2",),
    complexity="O(1)",
    latency_profile="sim_p95_v2026-02-26",
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
)

# ---------------------------------------------------------------------------
# Constants — from Safari & Schmidhuber (2025) Table 5 & 6
# ---------------------------------------------------------------------------

# Horizons in minutes (2^k for k=1..6)
HORIZONS_MIN: tuple[int, ...] = (2, 4, 8, 16, 32, 64)

# Paper coefficients (modified/rescaled, b_tilde and c_tilde)
# Short-term aggregated (T <= 16 min): b = -0.912%, c = +0.259%
# Long-term aggregated (T >= 1 hour):  b = +0.132%, c = -0.039%
# We use per-horizon estimates interpolated from Figure 5 of the paper.
# For the 30-min crossover, b crosses zero; c crosses zero nearby.
#
# Approximate values read from Figure 5 (rescaled coefficients b_tilde, c_tilde):
#   T=2min:  b_tilde ~ -0.50%, c_tilde ~ +0.15%
#   T=4min:  b_tilde ~ -0.40%, c_tilde ~ +0.12%
#   T=8min:  b_tilde ~ -0.25%, c_tilde ~ +0.08%
#   T=16min: b_tilde ~ -0.10%, c_tilde ~ +0.03%
#   T=32min: b_tilde ~ +0.05%, c_tilde ~ -0.02%
#   T=64min: b_tilde ~ +0.10%, c_tilde ~ -0.03%
#
# Note: These are APPROXIMATE. Must be calibrated on TMFD6 data.
# The paper uses multi-asset aggregation; single-asset values may differ.
_B_TILDE_DEFAULT: dict[int, float] = {
    2: -0.0050,
    4: -0.0040,
    8: -0.0025,
    16: -0.0010,
    32: +0.0005,
    64: +0.0010,
}
_C_TILDE_DEFAULT: dict[int, float] = {
    2: +0.0015,
    4: +0.0012,
    8: +0.0008,
    16: +0.0003,
    32: -0.0002,
    64: -0.0003,
}

# Default critical thresholds: phi_c = sqrt(-b/(3c))
# Derived from dE/dphi = b + 3c*phi^2 = 0 => phi^2 = -b/(3c)
# For reversion regime (T >= 32 min), phi_c ~ 0.9-1.1
_PHI_C_DEFAULT: dict[int, float] = {}
for _h in HORIZONS_MIN:
    _b = _B_TILDE_DEFAULT[_h]
    _c = _C_TILDE_DEFAULT[_h]
    if _b * _c < 0:  # signs differ -> meaningful critical point
        _PHI_C_DEFAULT[_h] = math.sqrt(abs(_b / (3.0 * _c)))
    else:
        # Same sign: no critical crossover. Use fallback.
        # For short horizons (reversion regime), use 2.0 as a
        # conservative threshold for "strong trend that persists".
        _PHI_C_DEFAULT[_h] = 2.0

# EMA half-life for volatility estimation (in ticks)
_VOL_EMA_HALFLIFE_TICKS: int = 200
_VOL_EMA_ALPHA: float = 1.0 - math.exp(-math.log(2.0) / _VOL_EMA_HALFLIFE_TICKS)

# Minimum variance to avoid division by zero
_MIN_VARIANCE: float = 1e-16

# Warmup ticks: >= max EMA time constant (64 min * 60 * 1.8 = 6912 ticks)
_WARMUP_TICKS: int = 7000

# Signal clip — aligned with explore.py at 5.0 to preserve cubic tail
_SIGNAL_CLIP: float = 5.0

# Hold period default (ns) — same as CBS
_DEFAULT_HOLD_NS: int = 300_000_000_000  # 300s

# Stop-loss (bps)
_DEFAULT_STOP_LOSS_BPS: int = 15

# Session skip (opening momentum period)
_DEFAULT_SESSION_SKIP_NS: int = 30 * 60 * 1_000_000_000  # 30 min


class _HorizonState:
    """Per-horizon EMA state for trend strength computation.

    Implements the exponentially-weighted trend strength (t-statistic)
    as described in Schmidhuber (2020), eq. 3-4.

    The trend strength phi_T(t) is computed as:
        phi_T = EMA_T(R_hat) / sqrt(EMA_T(R_hat^2))

    where R_hat = log-return minus long-term mean (approximated as 0 intraday).
    The normalization ensures phi has unit variance when returns are iid.
    """

    __slots__ = (
        "horizon_min",
        "ema_alpha",
        "ema_ret",
        "ema_ret_sq",
        "phi",
        "phi_c",
        "b_tilde",
        "c_tilde",
        "tick_count",
    )

    def __init__(self, horizon_min: int, phi_c: float, b_tilde: float, c_tilde: float) -> None:
        self.horizon_min: int = horizon_min
        # EMA alpha: alpha = 1 - exp(-1/N) where N = horizon in ticks.
        # TMFD6 tick rate ~ 1.8/sec. Aligned with explore.py for parity.
        n_ticks = max(1, int(horizon_min * 60 * 1.8))
        self.ema_alpha: float = 1.0 - math.exp(-1.0 / n_ticks)
        self.ema_ret: float = 0.0
        self.ema_ret_sq: float = 0.0
        self.phi: float = 0.0
        self.phi_c: float = phi_c
        self.b_tilde: float = b_tilde
        self.c_tilde: float = c_tilde
        self.tick_count: int = 0

    def update(self, log_return: float) -> float:
        """Update EMA and compute trend strength phi."""
        self.tick_count += 1
        a = self.ema_alpha
        self.ema_ret = a * log_return + (1.0 - a) * self.ema_ret
        self.ema_ret_sq = a * (log_return * log_return) + (1.0 - a) * self.ema_ret_sq

        # Variance of returns (for normalization)
        var = self.ema_ret_sq - self.ema_ret * self.ema_ret
        if var < _MIN_VARIANCE:
            self.phi = 0.0
            return 0.0

        # Trend strength = mean / std (t-statistic analog)
        # Scale by sqrt(N) where N ~ number of observations in window
        # This makes phi comparable across horizons
        n_eff = min(self.tick_count, int(1.0 / self.ema_alpha))
        sqrt_n = math.sqrt(n_eff)
        self.phi = self.ema_ret / math.sqrt(var) * sqrt_n

        # Clip to avoid extreme values
        self.phi = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self.phi))
        return self.phi

    def expected_return(self) -> float:
        """Compute E[R] = b*phi + c*phi^3 (ignoring constant a)."""
        phi = self.phi
        return self.b_tilde * phi + self.c_tilde * phi * phi * phi

    def is_above_critical(self) -> bool:
        """Check if |phi| exceeds the critical threshold."""
        return abs(self.phi) > self.phi_c

    def reset(self) -> None:
        """Reset state for new session."""
        self.ema_ret = 0.0
        self.ema_ret_sq = 0.0
        self.phi = 0.0
        self.tick_count = 0


class CriticalTrendReversion:
    """Multi-horizon critical trend reversion signal generator.

    Monitors trend strength (z-score / t-statistic) at multiple horizons
    and generates contrarian signals when any horizon's phi exceeds its
    critical threshold.

    Parameters
    ----------
    horizons_min : tuple of int
        Trend horizons in minutes.
    phi_c_overrides : dict, optional
        Override critical thresholds per horizon.
    b_tilde_overrides : dict, optional
        Override b_tilde coefficients per horizon.
    c_tilde_overrides : dict, optional
        Override c_tilde coefficients per horizon.
    actionable_horizons : tuple of int, optional
        Which horizons to use for trading signals.
        Default: (32, 64) — the trending regime where strong trends revert.
    """

    __slots__ = (
        "_horizons",
        "_prev_mid_x2",
        "_tick_count",
        "_actionable_horizons",
        "_warmed_up",
    )

    def __init__(
        self,
        horizons_min: tuple[int, ...] = HORIZONS_MIN,
        phi_c_overrides: Optional[dict[int, float]] = None,
        b_tilde_overrides: Optional[dict[int, float]] = None,
        c_tilde_overrides: Optional[dict[int, float]] = None,
        actionable_horizons: Optional[tuple[int, ...]] = None,
    ) -> None:
        phi_c = dict(_PHI_C_DEFAULT)
        b_tilde = dict(_B_TILDE_DEFAULT)
        c_tilde = dict(_C_TILDE_DEFAULT)
        if phi_c_overrides:
            phi_c.update(phi_c_overrides)
        if b_tilde_overrides:
            b_tilde.update(b_tilde_overrides)
        if c_tilde_overrides:
            c_tilde.update(c_tilde_overrides)

        self._horizons: dict[int, _HorizonState] = {}
        for h in horizons_min:
            self._horizons[h] = _HorizonState(
                horizon_min=h,
                phi_c=phi_c.get(h, 2.0),
                b_tilde=b_tilde.get(h, 0.0),
                c_tilde=c_tilde.get(h, 0.0),
            )

        self._prev_mid_x2: int = 0
        self._tick_count: int = 0
        self._actionable_horizons: tuple[int, ...] = actionable_horizons or (32, 64)
        self._warmed_up: bool = False

    def update(self, mid_x2: int) -> dict[str, object]:
        """Process a new mid_price_x2 tick and return signal state.

        Returns dict with:
            - 'signal': float in [-1, 1], 0 = no signal
            - 'direction': int, -1 (sell), 0 (flat), +1 (buy)
            - 'trigger_horizon': int or None, which horizon triggered
            - 'phi': dict of {horizon: phi_value}
            - 'expected_return': dict of {horizon: E[R]}
            - 'above_critical': dict of {horizon: bool}
        """
        result: dict[str, object] = {
            "signal": 0.0,
            "direction": 0,
            "trigger_horizon": None,
            "phi": {},
            "expected_return": {},
            "above_critical": {},
        }

        if mid_x2 <= 0:
            return result

        if self._prev_mid_x2 <= 0:
            self._prev_mid_x2 = mid_x2
            return result

        # Compute log-return (approximated as linear for small changes)
        # log(mid_x2 / prev_mid_x2) ~ (mid_x2 - prev_mid_x2) / prev_mid_x2
        log_ret = (mid_x2 - self._prev_mid_x2) / self._prev_mid_x2
        self._prev_mid_x2 = mid_x2
        self._tick_count += 1

        if self._tick_count < _WARMUP_TICKS:
            # Still warming up — update EMAs but don't signal
            for h_state in self._horizons.values():
                h_state.update(log_ret)
            return result

        self._warmed_up = True

        # Update all horizons
        phi_dict: dict[int, float] = {}
        er_dict: dict[int, float] = {}
        crit_dict: dict[int, bool] = {}

        for h, h_state in self._horizons.items():
            phi = h_state.update(log_ret)
            phi_dict[h] = phi
            er_dict[h] = h_state.expected_return()
            crit_dict[h] = h_state.is_above_critical()

        result["phi"] = phi_dict
        result["expected_return"] = er_dict
        result["above_critical"] = crit_dict

        # Generate signal from actionable horizons
        # Strategy: pick the longest actionable horizon that is above critical
        # (longer horizons = more reliable signal per paper)
        best_horizon: Optional[int] = None
        best_phi: float = 0.0

        for h in sorted(self._actionable_horizons, reverse=True):
            if h in crit_dict and crit_dict[h]:
                best_horizon = h
                best_phi = phi_dict[h]
                break

        if best_horizon is not None:
            # Contrarian: sell if phi > 0 (up-trend), buy if phi < 0
            direction = -1 if best_phi > 0 else 1
            # Signal strength: how far past critical threshold
            h_state = self._horizons[best_horizon]
            excess = abs(best_phi) - h_state.phi_c
            signal = direction * min(1.0, excess / h_state.phi_c)

            result["signal"] = signal
            result["direction"] = direction
            result["trigger_horizon"] = best_horizon

        return result

    def get_phi(self, horizon_min: int) -> float:
        """Get current phi for a specific horizon."""
        h_state = self._horizons.get(horizon_min)
        return h_state.phi if h_state else 0.0

    def get_all_phi(self) -> dict[int, float]:
        """Get current phi for all horizons."""
        return {h: s.phi for h, s in self._horizons.items()}

    def reset(self) -> None:
        """Reset all state (e.g., new trading session)."""
        self._prev_mid_x2 = 0
        self._tick_count = 0
        self._warmed_up = False
        for h_state in self._horizons.values():
            h_state.reset()

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    @property
    def warmed_up(self) -> bool:
        return self._warmed_up
