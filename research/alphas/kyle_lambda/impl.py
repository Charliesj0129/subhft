"""Kyle's Lambda Alpha — ref 135 (Kyle 1985, Continuous Auctions and Insider Trading).

Signal:  Rolling Kyle's Lambda (Price Impact Coefficient)
  signed_vol = volume * sign(bid_qty - ask_qty)      # Tick rule
  lambda     = Cov(dP, signed_vol) / Var(signed_vol)  # Kyle's lambda
  signal     = clip(lambda / max(EMA_64(|lambda|), eps), -2, 2)

A positive lambda → price impact aligned with buy pressure → informed trading.
A negative lambda → adverse selection / mean-reversion dynamics.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants
_EMA_32_ALPHA: float = 1.0 - math.exp(-1.0 / 32.0)  # ~0.0308
_EMA_64_ALPHA: float = 1.0 - math.exp(-1.0 / 64.0)  # ~0.0155
_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="kyle_lambda",
    hypothesis=(
        "Kyle's lambda measures the price impact per unit of signed order flow."
        " High lambda indicates informed trading and directional price pressure;"
        " low lambda suggests noise-dominated flow."
    ),
    formula="lambda = Cov(dP, signed_vol) / Var(signed_vol); signal = clip(lambda / EMA_64(|lambda|), -2, 2)",
    paper_refs=("135",),
    data_fields=("mid_price", "volume", "bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class KyleLambdaAlpha:
    """O(1) rolling Kyle's Lambda with EMA-based covariance estimation.

    update() accepts either:
      - 4 positional args:  mid_price, volume, bid_qty, ask_qty
      - keyword args:       mid_price=..., volume=..., bid_qty=..., ask_qty=...
    """

    __slots__ = (
        "_prev_mid",
        "_ema_dp",
        "_ema_sv",
        "_ema_dp_sq",
        "_ema_sv_sq",
        "_ema_dp_sv",
        "_lambda_baseline",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._prev_mid: float = 0.0
        self._ema_dp: float = 0.0
        self._ema_sv: float = 0.0
        self._ema_dp_sq: float = 0.0
        self._ema_sv_sq: float = 0.0
        self._ema_dp_sv: float = 0.0
        self._lambda_baseline: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args, **kwargs) -> float:  # noqa: C901
        """Ingest one tick and return the normalized Kyle's Lambda signal."""
        # --- resolve 4 fields from various call conventions ---
        if len(args) == 4:
            mid_price = float(args[0])
            volume = float(args[1])
            bid_qty = float(args[2])
            ask_qty = float(args[3])
        elif len(args) == 0 and kwargs:
            mid_price = float(kwargs.get("mid_price", 0.0))
            volume = float(kwargs.get("volume", 0.0))
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))
        elif len(args) == 0:
            mid_price = 0.0
            volume = 0.0
            bid_qty = 0.0
            ask_qty = 0.0
        else:
            raise ValueError(
                "update() requires 4 positional args (mid_price, volume, bid_qty, ask_qty) "
                "or keyword args; got %d positional args" % len(args)
            )

        # Step 1: delta_mid
        if not self._initialized:
            delta_mid = 0.0
        else:
            delta_mid = mid_price - self._prev_mid

        # Step 2: tick-rule sign
        if bid_qty > ask_qty:
            sign_v = 1.0
        elif ask_qty > bid_qty:
            sign_v = -1.0
        else:
            sign_v = 0.0

        # Step 3: signed volume
        signed_vol = volume * sign_v

        # Step 4: EMA updates
        alpha = _EMA_32_ALPHA
        if not self._initialized:
            self._ema_dp = delta_mid
            self._ema_sv = signed_vol
            self._ema_dp_sq = delta_mid * delta_mid
            self._ema_sv_sq = signed_vol * signed_vol
            self._ema_dp_sv = delta_mid * signed_vol
            self._initialized = True
        else:
            self._ema_dp += alpha * (delta_mid - self._ema_dp)
            self._ema_sv += alpha * (signed_vol - self._ema_sv)
            self._ema_dp_sq += alpha * (delta_mid * delta_mid - self._ema_dp_sq)
            self._ema_sv_sq += alpha * (signed_vol * signed_vol - self._ema_sv_sq)
            self._ema_dp_sv += alpha * (delta_mid * signed_vol - self._ema_dp_sv)

        # Step 5: covariance and variance
        cov = self._ema_dp_sv - self._ema_dp * self._ema_sv
        var = self._ema_sv_sq - self._ema_sv * self._ema_sv

        # Step 6: raw lambda
        raw_lambda = cov / max(var, _EPSILON)

        # Step 7: baseline normalization (EMA-64 of |lambda|)
        self._lambda_baseline += _EMA_64_ALPHA * (abs(raw_lambda) - self._lambda_baseline)

        # Step 8: normalized signal
        self._signal = max(-2.0, min(2.0, raw_lambda / max(self._lambda_baseline, _EPSILON)))

        # Update prev_mid for next tick
        self._prev_mid = mid_price

        return self._signal

    def reset(self) -> None:
        self._prev_mid = 0.0
        self._ema_dp = 0.0
        self._ema_sv = 0.0
        self._ema_dp_sq = 0.0
        self._ema_sv_sq = 0.0
        self._ema_dp_sv = 0.0
        self._lambda_baseline = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = KyleLambdaAlpha

__all__ = ["KyleLambdaAlpha", "ALPHA_CLASS"]
