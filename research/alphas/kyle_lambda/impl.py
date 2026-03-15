"""Kyle Lambda Alpha -- ref 001 (Kyle 1985).

Signal: Estimates Kyle's lambda (price impact per unit signed flow).
        High lambda -> illiquid market, flow has large price impact.
        Low lambda -> liquid market, flow absorbed without price move.

Formula:
  ofi       = delta(bid_qty) - delta(ask_qty)
  dp        = delta(mid_price)
  ofi_ema   = EMA_16(ofi)
  dp_ema    = EMA_16(dp)
  ofi2_ema  = EMA_16(ofi^2)
  lambda_est = dp_ema / (ofi_ema + epsilon)   # signed impact
  signal    = clip(EMA_8(lambda_est * ofi_ema / max(sqrt(ofi2_ema), eps)), -2, 2)

Interpretation:
  signal > 0 -> recent flow is buy-side with positive price impact
  signal < 0 -> recent flow is sell-side with negative price impact
  magnitude  -> strength of flow * impact (illiquidity amplifies)

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)
_EMA_ALPHA_16: float = 1.0 - math.exp(-1.0 / 16.0)
_EPSILON: float = 1e-8

_MANIFEST = AlphaManifest(
    alpha_id="kyle_lambda",
    hypothesis=(
        "Kyle's lambda measures price impact per unit of signed order flow. "
        "High lambda signals illiquid conditions where flow moves price; "
        "the product of lambda and flow direction predicts short-term returns."
    ),
    formula="signal = clip(EMA_8(lambda_est * ofi_ema / sqrt(ofi2_ema)), -2, 2)",
    paper_refs=("001",),
    data_fields=("bid_qty", "ask_qty", "mid_price"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.ENSEMBLE,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
    feature_set_version="lob_shared_v1",
)


class KyleLambdaAlpha:
    """O(1) Kyle lambda price-impact predictor.

    update() accepts either:
      - 3 positional args:  bid_qty, ask_qty, mid_price
      - keyword args:       bid_qty=..., ask_qty=..., mid_price=...
    """

    __slots__ = (
        "_ofi_ema",
        "_dp_ema",
        "_ofi2_ema",
        "_signal_ema",
        "_prev_bid",
        "_prev_ask",
        "_prev_mid",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._ofi_ema: float = 0.0
        self._dp_ema: float = 0.0
        self._ofi2_ema: float = 0.0
        self._signal_ema: float = 0.0
        self._prev_bid: float = 0.0
        self._prev_ask: float = 0.0
        self._prev_mid: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        """Ingest one tick and return signal."""
        if len(args) >= 3:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
            mid_price = float(args[2])
        elif len(args) in (1, 2):
            raise ValueError(
                "update() requires 3 positional args (bid_qty, ask_qty, mid_price) "
                "or keyword args"
            )
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))
            mid_price = float(kwargs.get("mid_price", 0.0))

        if not self._initialized:
            self._prev_bid = bid_qty
            self._prev_ask = ask_qty
            self._prev_mid = mid_price
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # OFI: change in bid qty minus change in ask qty
        ofi = (bid_qty - self._prev_bid) - (ask_qty - self._prev_ask)
        dp = mid_price - self._prev_mid
        self._prev_bid = bid_qty
        self._prev_ask = ask_qty
        self._prev_mid = mid_price

        # EMA of OFI, price change, and OFI squared
        self._ofi_ema += _EMA_ALPHA_16 * (ofi - self._ofi_ema)
        self._dp_ema += _EMA_ALPHA_16 * (dp - self._dp_ema)
        self._ofi2_ema += _EMA_ALPHA_16 * (ofi * ofi - self._ofi2_ema)

        # Kyle's lambda estimate: price change per unit flow
        lambda_est = self._dp_ema / (self._ofi_ema + _EPSILON)

        # Normalize: lambda * flow_direction / flow_volatility
        ofi_vol = math.sqrt(max(self._ofi2_ema, _EPSILON))
        raw = lambda_est * self._ofi_ema / ofi_vol

        # Smooth and clip
        self._signal_ema += _EMA_ALPHA_8 * (raw - self._signal_ema)
        self._signal = max(-2.0, min(2.0, self._signal_ema))
        return self._signal

    def reset(self) -> None:
        self._ofi_ema = 0.0
        self._dp_ema = 0.0
        self._ofi2_ema = 0.0
        self._signal_ema = 0.0
        self._prev_bid = 0.0
        self._prev_ask = 0.0
        self._prev_mid = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = KyleLambdaAlpha

__all__ = ["KyleLambdaAlpha", "ALPHA_CLASS"]
