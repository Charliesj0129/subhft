"""Intraday Jump Recovery (IJR) — push-response based contrarian strategy.

Formalizes CBS as a jump recovery strategy using Vlasiuk & Smirnov's
push-response framework. Instead of fixed bps thresholds, uses
standardized price increments and nonparametric response estimation.

Model (Vlasiuk & Smirnov, 2511.06177):
    push_L(t)     = m(t) - m(t-L)       (backward price increment)
    response_L(t) = m(t+L) - m(t)       (forward price increment)
    z_push = (push - mu) / sigma         (standardized)
    E[response | z_push] is nonlinear and asymmetric

Key findings from paper:
    - Short lags (< 5000 ticks): response ~ 0 (efficient)
    - Medium lags (5000-150000 ticks): significant response tails
    - Negative pushes → stronger positive recovery (asymmetric)
    - Tradable pockets exist at specific lag-magnitude combinations

For TMFD6 at 1.8 ticks/sec:
    5000 ticks ~ 46 min
    10000 ticks ~ 93 min
    CBS's 600s window ~ 1080 ticks

Paper refs:
    2511.06177 — Vlasiuk & Smirnov (2025)
    2403.00819 — Bibinger, Hautsch & Ristig (2024)

Allocator Law : Ring buffer for price history, no heap in update loop.
Precision Law : Signal is float. Prices use mid_x2.
Cache Law     : Pre-allocated numpy array for price ring buffer.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus

_MANIFEST = AlphaManifest(
    alpha_id="intraday_jump_recovery",
    hypothesis=(
        "Large intraday price moves are followed by asymmetric recovery. "
        "Negative pushes produce stronger positive responses. Formalizing "
        "CBS as a push-response strategy with statistical detection and "
        "asymmetric sizing improves edge over fixed-threshold CBS."
    ),
    formula=(
        "z_push = (push_L - mu) / sigma; "
        "signal = -sign(z_push) * f(|z_push|) when |z_push| > threshold; "
        "f() = asymmetric sizing function (stronger for sell-side jumps)"
    ),
    paper_refs=("2511.06177", "2403.00819", "1901.02691"),
    data_fields=("mid_price_x2",),
    complexity="O(1)",
    latency_profile="sim_p95_v2026-02-26",
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_LAG_TICKS: int = 1080  # ~600s at 1.8 ticks/sec (CBS equivalent)
_DEFAULT_Z_THRESHOLD: float = 2.0  # standardized push threshold
_DEFAULT_ASYMMETRY_MULT: float = 1.3  # size multiplier for sell-side recovery

# Rolling stats window for push standardization
_DEFAULT_STATS_WINDOW: int = 5000  # ticks for rolling mean/std of pushes

# Hold period (in ticks)
_DEFAULT_HOLD_TICKS: int = 540  # ~300s at 1.8 ticks/sec

# Maximum ring buffer size
_MAX_BUFFER: int = 8192

# Warmup
_WARMUP_TICKS: int = 2000

# EMA alpha for rolling variance
_STATS_EMA_ALPHA: float = 1.0 - math.exp(-math.log(2.0) / 2000)


class IntradayJumpRecovery:
    """Push-response based jump recovery signal generator.

    Monitors backward price increments (pushes) over a configurable lag,
    standardizes them, and generates contrarian signals when the standardized
    push exceeds a threshold.

    Parameters
    ----------
    lag_ticks : int
        Lag L for push/response computation (in ticks).
    z_threshold : float
        Standardized push threshold for signal generation.
    asymmetry_mult : float
        Position size multiplier for sell-side recovery (negative push → buy).
    hold_ticks : int
        Hold period after entry (in ticks).
    """

    __slots__ = (
        "_lag_ticks",
        "_z_threshold",
        "_asymmetry_mult",
        "_hold_ticks",
        "_price_buf",
        "_buf_idx",
        "_buf_count",
        "_ema_push",
        "_ema_push_sq",
        "_tick_count",
        "_warmed_up",
    )

    def __init__(
        self,
        lag_ticks: int = _DEFAULT_LAG_TICKS,
        z_threshold: float = _DEFAULT_Z_THRESHOLD,
        asymmetry_mult: float = _DEFAULT_ASYMMETRY_MULT,
        hold_ticks: int = _DEFAULT_HOLD_TICKS,
    ) -> None:
        self._lag_ticks = lag_ticks
        self._z_threshold = z_threshold
        self._asymmetry_mult = asymmetry_mult
        self._hold_ticks = hold_ticks

        # Ring buffer for prices
        buf_size = max(_MAX_BUFFER, lag_ticks + 100)
        self._price_buf = np.zeros(buf_size, dtype=np.int64)
        self._buf_idx: int = 0
        self._buf_count: int = 0

        # Running statistics for push standardization
        self._ema_push: float = 0.0
        self._ema_push_sq: float = 0.0

        self._tick_count: int = 0
        self._warmed_up: bool = False

    def update(self, mid_x2: int) -> dict[str, object]:
        """Process a new tick and compute push-response signal.

        Returns dict with:
            - 'push_raw': raw backward price increment (mid_x2 units)
            - 'push_bps': push in bps
            - 'z_push': standardized push
            - 'signal': 0 (no signal), -1 (sell/contrarian to up-push), +1 (buy/contrarian to down-push)
            - 'size_mult': position size multiplier (asymmetry-adjusted)
            - 'hold_ticks': hold period for this trade
        """
        result: dict[str, object] = {
            "push_raw": 0,
            "push_bps": 0.0,
            "z_push": 0.0,
            "signal": 0,
            "size_mult": 1.0,
            "hold_ticks": self._hold_ticks,
        }

        if mid_x2 <= 0:
            return result

        # Store in ring buffer
        buf_size = len(self._price_buf)
        self._price_buf[self._buf_idx % buf_size] = mid_x2
        self._buf_idx += 1
        self._buf_count = min(self._buf_count + 1, buf_size)
        self._tick_count += 1

        # Need at least lag_ticks in buffer
        if self._buf_count <= self._lag_ticks:
            return result

        # Compute push: current price - price L ticks ago
        lag_idx = (self._buf_idx - 1 - self._lag_ticks) % buf_size
        prev_price = self._price_buf[lag_idx]
        if prev_price <= 0:
            return result

        push_raw = int(mid_x2) - int(prev_price)
        push_frac = push_raw / prev_price  # fractional push
        push_bps = push_frac * 10000.0

        # Update running statistics (EMA)
        a = _STATS_EMA_ALPHA
        self._ema_push = a * push_frac + (1.0 - a) * self._ema_push
        self._ema_push_sq = a * (push_frac ** 2) + (1.0 - a) * self._ema_push_sq

        result["push_raw"] = push_raw
        result["push_bps"] = push_bps

        if self._tick_count < _WARMUP_TICKS:
            return result

        self._warmed_up = True

        # Standardize push
        var = self._ema_push_sq - self._ema_push ** 2
        if var < 1e-20:
            return result

        sigma = math.sqrt(var)
        z_push = (push_frac - self._ema_push) / sigma
        z_push = max(-6.0, min(6.0, z_push))  # clip extremes
        result["z_push"] = z_push

        # Signal: contrarian when |z_push| exceeds threshold
        if abs(z_push) > self._z_threshold:
            # Contrarian: sell if push was positive (up-jump), buy if negative (down-jump)
            direction = -1 if z_push > 0 else 1
            result["signal"] = direction

            # Asymmetric sizing: larger for sell-side recovery (negative push → buy)
            if direction == 1:  # buying after down-jump
                result["size_mult"] = self._asymmetry_mult
            else:
                result["size_mult"] = 1.0

        return result

    def get_push_stats(self) -> dict[str, float]:
        """Get current rolling push statistics."""
        var = self._ema_push_sq - self._ema_push ** 2
        sigma = math.sqrt(max(var, 0.0))
        return {
            "mean_push": self._ema_push,
            "std_push": sigma,
            "threshold_frac": self._z_threshold * sigma if sigma > 0 else 0.0,
            "threshold_bps": self._z_threshold * sigma * 10000.0 if sigma > 0 else 0.0,
        }

    def reset(self) -> None:
        """Reset all state."""
        self._price_buf[:] = 0
        self._buf_idx = 0
        self._buf_count = 0
        self._ema_push = 0.0
        self._ema_push_sq = 0.0
        self._tick_count = 0
        self._warmed_up = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    @property
    def warmed_up(self) -> bool:
        return self._warmed_up
