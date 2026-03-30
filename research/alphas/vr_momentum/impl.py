"""VR-Filtered Momentum (VRM) — momentum strategy with variance ratio filter.

Entry: VR(5min) > vr_threshold AND |push_z(10min)| > z_threshold
Direction: sign(push) — follow the trend
Hold: configurable (default 3000 ticks ~ 28 min)
Stop: -20 bps
Session: skip opening 30 min

The Variance Ratio VR(q) = Var(r_q) / (q * Var(r_1)) measures whether
returns are trending (VR > 1) or mean-reverting (VR < 1). When VR > 1,
momentum strategies have edge; when VR < 1, contrarian strategies
(like CBS) have edge.

Paper refs:
    2511.06177 — Vlasiuk & Smirnov (2025), Push-response anomalies
    2501.16772 — Safari & Schmidhuber (2025), Trends at intraday scales
    2511.08571 — Singha et al. (2025), Forecast-to-Fill

Allocator Law : __slots__, pre-allocated ring buffer, O(1) per tick.
Precision Law : Signal is float. Prices use mid_x2 (scaled int).
Cache Law     : EMA state in scalar floats.
"""

from __future__ import annotations

import math

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus

_MANIFEST = AlphaManifest(
    alpha_id="vr_momentum",
    hypothesis=(
        "TMFD6 exhibits intraday momentum at 10-46 min horizons. VR > 1 "
        "confirms trending regime. Combining VR filter with push z-score "
        "threshold creates a momentum strategy: enter WITH the trend."
    ),
    formula=(
        "VR(q) = Var(r_q) / (q * Var(r_1)); "
        "z_push = (push_L - mu) / sigma; "
        "signal = sign(push) when VR > threshold AND |z_push| > z_thresh"
    ),
    paper_refs=("2511.06177", "2501.16772", "2511.08571"),
    data_fields=("mid_price_x2",),
    complexity="O(1)",
    latency_profile="sim_p95_v2026-02-26",
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_VR_Q: int = 540           # 5-min aggregation for VR
_DEFAULT_VR_THRESHOLD: float = 1.2
_DEFAULT_Z_THRESHOLD: float = 2.0
_DEFAULT_PUSH_LAG: int = 1080      # ~10 min
_DEFAULT_HOLD_TICKS: int = 3000    # ~28 min
_DEFAULT_STOP_BPS: float = 20.0
_DEFAULT_COOLDOWN: int = 3000
_DEFAULT_WARMUP: int = 7000

# EMA half-life for running stats (ticks)
_STATS_HL: int = 3000
_STATS_ALPHA: float = 1.0 - math.exp(-math.log(2.0) / _STATS_HL)

# Ring buffer
_BUF_SIZE: int = 16384


class VRMomentum:
    """Variance-Ratio filtered momentum signal generator.

    Parameters
    ----------
    vr_q : int
        Aggregation period (ticks) for VR numerator.
    vr_threshold : float
        Minimum VR to confirm trending regime.
    z_threshold : float
        Minimum |z_push| for entry.
    push_lag : int
        Backward lag (ticks) for push computation.
    hold_ticks : int
        Default hold period.
    stop_bps : float
        Stop-loss in bps (adverse move from entry).
    cooldown_ticks : int
        Ticks between exit and next allowed entry.
    warmup_ticks : int
        Minimum ticks before signal generation.
    """

    __slots__ = (
        "_vr_q",
        "_vr_threshold",
        "_z_threshold",
        "_push_lag",
        "_hold_ticks",
        "_stop_bps",
        "_cooldown_ticks",
        "_warmup_ticks",
        # Ring buffer
        "_price_buf",
        "_buf_idx",
        "_buf_count",
        # Running EMA stats for push standardization
        "_ema_push",
        "_ema_push_sq",
        # Running EMA stats for VR
        "_ema_r1_sq",     # Var(1-tick return)
        "_ema_rq_sq",     # Var(q-tick return)
        # Counters
        "_tick_count",
        "_warmed_up",
    )

    def __init__(
        self,
        vr_q: int = _DEFAULT_VR_Q,
        vr_threshold: float = _DEFAULT_VR_THRESHOLD,
        z_threshold: float = _DEFAULT_Z_THRESHOLD,
        push_lag: int = _DEFAULT_PUSH_LAG,
        hold_ticks: int = _DEFAULT_HOLD_TICKS,
        stop_bps: float = _DEFAULT_STOP_BPS,
        cooldown_ticks: int = _DEFAULT_COOLDOWN,
        warmup_ticks: int = _DEFAULT_WARMUP,
    ) -> None:
        self._vr_q = vr_q
        self._vr_threshold = vr_threshold
        self._z_threshold = z_threshold
        self._push_lag = push_lag
        self._hold_ticks = hold_ticks
        self._stop_bps = stop_bps
        self._cooldown_ticks = cooldown_ticks
        self._warmup_ticks = warmup_ticks

        buf_size = max(_BUF_SIZE, push_lag + vr_q + 100)
        self._price_buf = np.zeros(buf_size, dtype=np.int64)
        self._buf_idx: int = 0
        self._buf_count: int = 0

        self._ema_push: float = 0.0
        self._ema_push_sq: float = 0.0
        self._ema_r1_sq: float = 0.0
        self._ema_rq_sq: float = 0.0

        self._tick_count: int = 0
        self._warmed_up: bool = False

    def update(self, mid_x2: int) -> dict[str, object]:
        """Process one tick. Returns signal state dict.

        Returns dict with:
            'vr': current variance ratio
            'push_bps': backward push in bps
            'z_push': standardized push
            'signal': 0 (no signal), +1 (buy/long momentum), -1 (sell/short momentum)
            'hold_ticks': hold period for this entry
        """
        result: dict[str, object] = {
            "vr": 1.0,
            "push_bps": 0.0,
            "z_push": 0.0,
            "signal": 0,
            "hold_ticks": self._hold_ticks,
        }

        if mid_x2 <= 0:
            return result

        buf_size = len(self._price_buf)
        self._price_buf[self._buf_idx % buf_size] = mid_x2
        self._buf_idx += 1
        self._buf_count = min(self._buf_count + 1, buf_size)
        self._tick_count += 1

        # Need enough history for both push_lag and vr_q
        min_history = max(self._push_lag, self._vr_q) + 1
        if self._buf_count < min_history:
            return result

        # --- 1-tick return ---
        prev_idx = (self._buf_idx - 2) % buf_size
        prev_price = self._price_buf[prev_idx]
        if prev_price <= 0:
            return result
        r1 = (float(mid_x2) - float(prev_price)) / float(prev_price)

        # --- q-tick return ---
        q_idx = (self._buf_idx - 1 - self._vr_q) % buf_size
        q_price = self._price_buf[q_idx]
        rq = 0.0
        if q_price > 0:
            rq = (float(mid_x2) - float(q_price)) / float(q_price)

        # --- Update EMA stats ---
        a = _STATS_ALPHA
        self._ema_r1_sq = a * (r1 * r1) + (1.0 - a) * self._ema_r1_sq
        self._ema_rq_sq = a * (rq * rq) + (1.0 - a) * self._ema_rq_sq

        # --- Push computation ---
        lag_idx = (self._buf_idx - 1 - self._push_lag) % buf_size
        lag_price = self._price_buf[lag_idx]
        push_frac = 0.0
        if lag_price > 0:
            push_frac = (float(mid_x2) - float(lag_price)) / float(lag_price)

        self._ema_push = a * push_frac + (1.0 - a) * self._ema_push
        self._ema_push_sq = a * (push_frac * push_frac) + (1.0 - a) * self._ema_push_sq

        push_bps = push_frac * 10000.0
        result["push_bps"] = push_bps

        if self._tick_count < self._warmup_ticks:
            return result

        self._warmed_up = True

        # --- Variance Ratio ---
        var_r1 = max(self._ema_r1_sq, 1e-20)
        var_rq = self._ema_rq_sq  # no need to subtract mean^2 (mean ~ 0 intraday)
        vr = var_rq / (self._vr_q * var_r1) if var_r1 > 1e-20 else 1.0
        result["vr"] = vr

        # --- Standardized push ---
        push_var = self._ema_push_sq - self._ema_push * self._ema_push
        if push_var < 1e-20:
            return result
        push_sigma = math.sqrt(push_var)
        z_push = (push_frac - self._ema_push) / push_sigma
        z_push = max(-6.0, min(6.0, z_push))
        result["z_push"] = z_push

        # --- Signal ---
        if vr > self._vr_threshold and abs(z_push) > self._z_threshold:
            result["signal"] = 1 if z_push > 0 else -1

        return result

    @property
    def vr_threshold(self) -> float:
        return self._vr_threshold

    @property
    def z_threshold(self) -> float:
        return self._z_threshold

    def reset(self) -> None:
        """Reset all state."""
        self._price_buf[:] = 0
        self._buf_idx = 0
        self._buf_count = 0
        self._ema_push = 0.0
        self._ema_push_sq = 0.0
        self._ema_r1_sq = 0.0
        self._ema_rq_sq = 0.0
        self._tick_count = 0
        self._warmed_up = False

    def get_regime(self) -> str:
        """Return current regime based on VR.

        Used for CBS/VRM mutual exclusion:
        - 'trending': VR > 1.0 → VRM should be active, CBS should stand down
        - 'reverting': VR <= 1.0 → CBS should be active, VRM should stand down
        - 'unknown': not yet warmed up
        """
        if not self._warmed_up:
            return "unknown"
        var_r1 = max(self._ema_r1_sq, 1e-20)
        vr = self._ema_rq_sq / (self._vr_q * var_r1) if var_r1 > 1e-20 else 1.0
        return "trending" if vr > 1.0 else "reverting"

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    @property
    def warmed_up(self) -> bool:
        return self._warmed_up


class VRRegimeSwitch:
    """VR-based regime arbitrator for CBS/VRM mutual exclusion.

    When VR > 1.0 (trending), enables VRM and disables CBS.
    When VR <= 1.0 (mean-reverting), enables CBS and disables VRM.

    This prevents the directional conflict where VRM (momentum) and CBS
    (contrarian) would take opposing positions on the same symbol,
    paying 2x RT costs for a net-zero position.

    Usage:
        switch = VRRegimeSwitch(vrm_signal_generator)
        # In strategy runner:
        regime = switch.get_active_strategy()
        if regime == 'vrm':
            # process VRM signal
        elif regime == 'cbs':
            # process CBS signal
    """

    __slots__ = ("_vrm",)

    def __init__(self, vrm: VRMomentum) -> None:
        self._vrm = vrm

    def get_active_strategy(self) -> str:
        """Return which strategy should be active.

        Returns:
            'vrm': VRM (momentum) should trade
            'cbs': CBS (contrarian) should trade
            'none': neither (warmup period)
        """
        regime = self._vrm.get_regime()
        if regime == "trending":
            return "vrm"
        elif regime == "reverting":
            return "cbs"
        return "none"
