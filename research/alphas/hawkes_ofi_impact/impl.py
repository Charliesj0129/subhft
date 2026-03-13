"""Hawkes OFI Impact Alpha — ref 026.

Signal:  signal_t = EMA_8(OFI) * clip(hawkes_intensity / baseline, 0.5, 2.0)

OFI measures net order flow imbalance via bid/ask queue changes.
A self-exciting Hawkes process (approximated by exponential kernel EMA)
modulates intensity: when trades cluster (high |OFI| bursts), the signal
is amplified.  During quiet periods, intensity decays toward baseline
and the amplification factor shrinks.

Allocator Law  : __slots__ on class; all state is scalar float/bool/int.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay for OFI: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)
# Hawkes intensity decay: window ~ 16 ticks -> slower decay for self-exciting
_HAWKES_DECAY: float = 1.0 - math.exp(-1.0 / 16.0)
# Baseline intensity: the long-run mean that intensity decays toward
_BASELINE_INTENSITY: float = 0.05
_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="hawkes_ofi_impact",
    hypothesis=(
        "Self-exciting order flow clustering amplifies OFI predictive power: "
        "when trades arrive in bursts (high Hawkes intensity), the OFI signal "
        "carries stronger short-term price prediction."
    ),
    formula="signal = EMA_8(OFI) * clip(hawkes_intensity / baseline, 0.5, 2.0)",
    paper_refs=("026",),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.ENSEMBLE,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
    feature_set_version="lob_shared_v1",
)


class HawkesOfiImpactAlpha:
    """O(1) Hawkes-modulated OFI predictor.

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
    """

    __slots__ = (
        "_ofi_ema",
        "_intensity_ema",
        "_prev_bid",
        "_prev_ask",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._ofi_ema: float = 0.0
        self._intensity_ema: float = _BASELINE_INTENSITY
        self._prev_bid: float = 0.0
        self._prev_ask: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        """Ingest one tick of bid/ask queue sizes and return signal."""
        # --- resolve bid_qty and ask_qty from various call conventions ---
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif len(args) == 1:
            raise ValueError("update() requires 2 positional args (bid_qty, ask_qty) or keyword args")
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        if not self._initialized:
            self._prev_bid = bid_qty
            self._prev_ask = ask_qty
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # OFI: change in bid qty minus change in ask qty
        ofi = (bid_qty - self._prev_bid) - (ask_qty - self._prev_ask)
        self._prev_bid = bid_qty
        self._prev_ask = ask_qty

        # EMA of OFI
        self._ofi_ema += _EMA_ALPHA * (ofi - self._ofi_ema)

        # Hawkes intensity: excited by |OFI| magnitude, normalized by total qty
        denom = bid_qty + ask_qty + _EPSILON
        excitation = abs(ofi) / denom
        self._intensity_ema += _HAWKES_DECAY * (excitation - self._intensity_ema)

        # Intensity factor: ratio to baseline, clipped to [0.5, 2.0]
        intensity_factor = max(
            0.5,
            min(2.0, self._intensity_ema / (_BASELINE_INTENSITY + _EPSILON)),
        )

        # Combined signal: normalized OFI * intensity amplification
        normalized_ofi = self._ofi_ema / denom
        self._signal = max(-2.0, min(2.0, normalized_ofi * intensity_factor))

        return self._signal

    def reset(self) -> None:
        self._ofi_ema = 0.0
        self._intensity_ema = _BASELINE_INTENSITY
        self._prev_bid = 0.0
        self._prev_ask = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = HawkesOfiImpactAlpha

__all__ = ["HawkesOfiImpactAlpha", "ALPHA_CLASS"]
