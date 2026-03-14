"""Flow Persistence Alpha — ref 089.

Hypothesis: Agreement between recent and longer-term OFI direction
(persistence) predicts trend continuation; disagreement predicts reversals.

Formula:
  qi        = (bid - ask) / max(bid + ask, 1)
  fast      = EMA_4(qi)
  slow      = EMA_32(qi)
  agreement = fast * slow
  signal    = clip(EMA_8(agreement), -1, 1)

Interpretation:
  - fast and slow same sign  -> agreement > 0 -> persistent trend
  - fast and slow opposite   -> agreement < 0 -> reversal signal

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# EMA coefficients
# ---------------------------------------------------------------------------
_A4: float = 1.0 - math.exp(-1.0 / 4.0)  # ~0.2212 — fast window
_A8: float = 1.0 - math.exp(-1.0 / 8.0)  # ~0.1175 — agreement smoothing
_A32: float = 1.0 - math.exp(-1.0 / 32.0)  # ~0.0308 — slow window

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="flow_persistence",
    hypothesis=(
        "Agreement between recent and longer-term OFI direction (persistence) "
        "predicts trend continuation; disagreement predicts reversals."
    ),
    formula=(
        "qi = (bid - ask) / max(bid + ask, 1); "
        "fast = EMA_4(qi); slow = EMA_32(qi); "
        "agreement = fast * slow; "
        "signal = clip(EMA_8(agreement), -1, 1)"
    ),
    paper_refs=("089",),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


# ---------------------------------------------------------------------------
# Alpha implementation
# ---------------------------------------------------------------------------
class FlowPersistenceAlpha:
    """O(1) flow persistence predictor with dual-EMA agreement scoring.

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
      - bids/asks arrays:   bids=np.ndarray (shape (N,2)), asks=np.ndarray (shape (N,2))
    """

    __slots__ = ("_fast_ema", "_slow_ema", "_agreement_ema", "_signal")

    def __init__(self) -> None:
        self._fast_ema: float = 0.0
        self._slow_ema: float = 0.0
        self._agreement_ema: float = 0.0
        self._signal: float = 0.0

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
            raise ValueError("update() requires 2 positional args (bid_qty, ask_qty) or keyword args")
        elif "bids" in kwargs and "asks" in kwargs:
            import numpy as np

            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(np.asarray(bids).reshape(-1, 2)[0, 1])
            ask_qty = float(np.asarray(asks).reshape(-1, 2)[0, 1])
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        # Raw queue imbalance
        total = bid_qty + ask_qty
        qi = (bid_qty - ask_qty) / max(total, 1.0)

        # Dual EMA tracking
        self._fast_ema += _A4 * (qi - self._fast_ema)
        self._slow_ema += _A32 * (qi - self._slow_ema)

        # Agreement: product of fast and slow EMAs
        agreement = self._fast_ema * self._slow_ema

        # Smooth the agreement signal
        self._agreement_ema += _A8 * (agreement - self._agreement_ema)

        # Clip to [-1, 1]
        self._signal = max(-1.0, min(1.0, self._agreement_ema))
        return self._signal

    def reset(self) -> None:
        """Clear all EMA state to zero."""
        self._fast_ema = 0.0
        self._slow_ema = 0.0
        self._agreement_ema = 0.0
        self._signal = 0.0

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = FlowPersistenceAlpha

__all__ = ["FlowPersistenceAlpha", "ALPHA_CLASS"]
