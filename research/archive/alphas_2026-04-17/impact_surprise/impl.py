"""Impact Surprise Signal (ISS) — adaptive OFI-to-price sensitivity detector.

Signal: ternary regime indicator (+1 / 0 / -1) based on whether realized
price impact of OFI exceeds or falls below a baseline.

Baselines (selectable via `baseline_mode`):
    "depth"  : b_eq = 1 / (2 * D(t))  — Cont et al. (2014) equilibrium
    "ema"    : b_eq = slow EMA of b_hat — self-referencing (Challenger B-2)

Session boundary masking (Challenger B-3):
    Signal forced to 0.0 during open/close windows where microstructure is
    non-stationary.  Default: first 300 ticks (~11s at 27 ticks/s for TXFD6)
    after session start, configurable via `session_mask_ticks`.

Dead-zone (Challenger B-1):
    |deviation| < _ISS_THRESHOLD => signal = 0 (no regime call).

Paper refs:
  Cont, Kukanov, Stoikov (2014) "The price impact of order book events"
  Takahashi (2025) arXiv:2508.06788 — SVAR-ITH intraday b_r estimation

Allocator Law  : __slots__ on class; no heap allocations in update().
Precision Law  : OFI and returns use scaled int (x10000); ISS output is float.
Cache Law      : all state in scalar slots (no arrays, no dicts).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

_EMA_SPAN: int = 200
_EMA_ALPHA: float = 2.0 / (_EMA_SPAN + 1)
_BASELINE_EMA_SPAN: int = 2000
_BASELINE_EMA_ALPHA: float = 2.0 / (_BASELINE_EMA_SPAN + 1)
_WARMUP_TICKS: int = 2 * _EMA_SPAN
_ISS_THRESHOLD: float = 0.3
_SIGNAL_CLIP: float = 1.0
_DEPTH_EPSILON: float = 1.0
_VAR_MIN_MEANINGFUL: float = 0.01
_SESSION_MASK_TICKS: int = 300

_MANIFEST = AlphaManifest(
    alpha_id="impact_surprise",
    hypothesis=(
        "Deviations of realized OFI-to-price sensitivity from depth-implied "
        "equilibrium (Cont 2014) indicate regime transitions between informed "
        "and noise-dominated flow.  When realized impact exceeds equilibrium, "
        "OFI signals carry genuine information; when below, OFI is unreliable."
    ),
    formula=(
        "b_eq = 1/(2*depth) or slow_EMA(b_hat); b_hat = ema_cov(OFI,ret)/ema_var(OFI); "
        "ISS = clip(sign(b_hat - b_eq) * min(|b_hat - b_eq|/b_eq, 1), [-1,1])"
    ),
    paper_refs=("arXiv:2508.06788", "Cont2014"),
    data_fields=(
        "ofi_l1_raw",
        "mid_price_x2",
        "bid_depth",
        "ask_depth",
    ),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class ImpactSurpriseAlpha:
    """O(1) EMA-based impact regime detector.

    Consumes L1 feature data via keyword args:
        ofi_l1_raw: int   — current tick OFI (from FeatureEngine slot 11)
        mid_price_x2: int — current mid_price_x2 (slot 2)
        bid_depth: int    — current bid depth (slot 4)
        ask_depth: int    — current ask depth (slot 5)

    Optional config via constructor:
        baseline_mode: "depth" (default) or "ema"
        session_mask_ticks: ticks to suppress at session start (default 300)
    """

    __slots__ = (
        "_ema_ofi",
        "_ema_ret",
        "_ema_ofi2",
        "_ema_ofi_ret",
        "_baseline_ema",
        "_prev_mid_x2",
        "_signal",
        "_initialized",
        "_tick_count",
        "_b_hat",
        "_b_eq",
        "_baseline_mode",
        "_session_mask_ticks",
    )

    def __init__(
        self,
        baseline_mode: str = "depth",
        session_mask_ticks: int = _SESSION_MASK_TICKS,
    ) -> None:
        self._baseline_mode: str = baseline_mode
        self._session_mask_ticks: int = session_mask_ticks
        self._ema_ofi: float = 0.0
        self._ema_ret: float = 0.0
        self._ema_ofi2: float = 1.0
        self._ema_ofi_ret: float = 0.0
        self._baseline_ema: float = 0.0
        self._prev_mid_x2: int = 0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._tick_count: int = 0
        self._b_hat: float = 0.0
        self._b_eq: float = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: object) -> float:
        ofi_raw = int(kwargs.get("ofi_l1_raw", 0))
        mid_x2 = int(kwargs.get("mid_price_x2", 0))
        bid_depth = int(kwargs.get("bid_depth", 0))
        ask_depth = int(kwargs.get("ask_depth", 0))

        self._tick_count += 1

        # Depth-based equilibrium (always computed for diagnostics)
        total_depth = float(max(bid_depth + ask_depth, 1))
        depth_b_eq = 1.0 / (2.0 * total_depth + _DEPTH_EPSILON)

        if not self._initialized:
            self._prev_mid_x2 = mid_x2
            self._ema_ofi = 0.0
            self._ema_ret = 0.0
            self._ema_ofi2 = 1.0
            self._ema_ofi_ret = 0.0
            self._baseline_ema = depth_b_eq
            self._b_eq = depth_b_eq
            self._initialized = True
            self._signal = 0.0
            return self._signal

        ret = float(mid_x2 - self._prev_mid_x2)
        self._prev_mid_x2 = mid_x2

        ofi_f = float(ofi_raw)
        a = _EMA_ALPHA

        self._ema_ofi = (1.0 - a) * self._ema_ofi + a * ofi_f
        self._ema_ret = (1.0 - a) * self._ema_ret + a * ret
        self._ema_ofi2 = (1.0 - a) * self._ema_ofi2 + a * ofi_f * ofi_f
        self._ema_ofi_ret = (1.0 - a) * self._ema_ofi_ret + a * ofi_f * ret

        cov_hat = self._ema_ofi_ret - self._ema_ofi * self._ema_ret
        var_hat = self._ema_ofi2 - self._ema_ofi * self._ema_ofi

        if var_hat > _VAR_MIN_MEANINGFUL:
            self._b_hat = cov_hat / var_hat
        else:
            self._b_hat = depth_b_eq  # Bayesian shrinkage to prior

        # Update self-referencing baseline
        ba = _BASELINE_EMA_ALPHA
        self._baseline_ema = (1.0 - ba) * self._baseline_ema + ba * self._b_hat

        # Select baseline
        if self._baseline_mode == "ema":
            self._b_eq = self._baseline_ema if self._baseline_ema > 1e-15 else depth_b_eq
        else:
            self._b_eq = depth_b_eq

        # Warmup and session mask
        if self._tick_count < _WARMUP_TICKS or self._tick_count < self._session_mask_ticks:
            self._signal = 0.0
            return self._signal

        if self._b_eq > 1e-15:
            deviation = (self._b_hat - self._b_eq) / self._b_eq
        else:
            deviation = 0.0

        # Dead-zone threshold (Challenger B-1)
        if abs(deviation) < _ISS_THRESHOLD:
            raw_signal = 0.0
        else:
            raw_signal = math.copysign(min(abs(deviation), _SIGNAL_CLIP), deviation)

        self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, raw_signal))
        return self._signal

    def reset(self) -> None:
        self._ema_ofi = 0.0
        self._ema_ret = 0.0
        self._ema_ofi2 = 1.0
        self._ema_ofi_ret = 0.0
        self._baseline_ema = 0.0
        self._prev_mid_x2 = 0
        self._signal = 0.0
        self._initialized = False
        self._tick_count = 0
        self._b_hat = 0.0
        self._b_eq = 0.0

    def get_signal(self) -> float:
        return self._signal

    @property
    def b_hat(self) -> float:
        return self._b_hat

    @property
    def b_eq(self) -> float:
        return self._b_eq


ALPHA_CLASS = ImpactSurpriseAlpha

__all__ = ["ImpactSurpriseAlpha", "ALPHA_CLASS"]
