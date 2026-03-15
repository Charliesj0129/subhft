"""Flow Persistence Alpha — order flow autocorrelation signal.

Hypothesis: Order flow exhibits persistence (autocorrelation). When OFI
maintains direction for sustained periods, the trend is likely to continue.
Short-term OFI autocorrelation predicts next-tick direction.

Formula:
  OFI_raw    = bid_qty - ask_qty
  ema_ofi    = EMA_8(OFI_raw)
  ema_abs    = EMA_16(|OFI_raw|)
  FP_t       = ema_ofi * |ema_ofi| / max(ema_abs, epsilon)

Interpretation:
  - High FP  -> sustained directional flow (persistence)
  - Low FP   -> choppy / random flow

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# EMA coefficients
# ---------------------------------------------------------------------------
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)  # ~0.1175
_EMA_ALPHA_16: float = 1.0 - math.exp(-1.0 / 16.0)  # ~0.0606

_EPSILON: float = 1e-8

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="flow_persistence",
    hypothesis=(
        "Order flow exhibits persistence (autocorrelation). When OFI "
        "maintains direction for sustained periods, the trend is likely "
        "to continue. Short-term OFI autocorrelation predicts next-tick direction."
    ),
    formula="FP_t = EMA_8(OFI_raw) * |EMA_8(OFI_raw)| / EMA_16(|OFI_raw|)",
    paper_refs=(),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


# ---------------------------------------------------------------------------
# Alpha implementation
# ---------------------------------------------------------------------------
class FlowPersistenceAlpha:
    """O(1) flow persistence predictor.

    FP_t = EMA_8(OFI_raw) * |EMA_8(OFI_raw)| / EMA_16(|OFI_raw|)

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
      - bids/asks arrays:   bids=np.ndarray (shape (N,2)), asks=np.ndarray (shape (N,2))
    """

    __slots__ = ("_ema_ofi", "_ema_abs", "_signal", "_initialized")

    def __init__(self) -> None:
        self._ema_ofi: float = 0.0
        self._ema_abs: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        """Update state and return the current signal."""
        # --- resolve bid_qty and ask_qty from various call conventions ---
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif len(args) == 1:
            raise ValueError(
                "update() requires 2 positional args (bid_qty, ask_qty) or keyword args"
            )
        elif "bids" in kwargs and "asks" in kwargs:
            import numpy as np

            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(np.asarray(bids).reshape(-1, 2)[0, 1])
            ask_qty = float(np.asarray(asks).reshape(-1, 2)[0, 1])
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        # Raw order flow imbalance
        ofi_raw = bid_qty - ask_qty

        # EMA bootstrap: first tick sets EMAs to raw values
        if not self._initialized:
            self._ema_ofi = ofi_raw
            self._ema_abs = abs(ofi_raw)
            self._initialized = True
        else:
            self._ema_ofi += _EMA_ALPHA_8 * (ofi_raw - self._ema_ofi)
            self._ema_abs += _EMA_ALPHA_16 * (abs(ofi_raw) - self._ema_abs)

        # FP_t = ema_ofi * |ema_ofi| / max(ema_abs, epsilon)
        denom = max(self._ema_abs, _EPSILON)
        self._signal = self._ema_ofi * abs(self._ema_ofi) / denom
        return self._signal

    def reset(self) -> None:
        """Clear all EMA state."""
        self._ema_ofi = 0.0
        self._ema_abs = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = FlowPersistenceAlpha

__all__ = ["FlowPersistenceAlpha", "ALPHA_CLASS"]
