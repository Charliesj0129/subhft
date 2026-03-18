"""Depth Concentration Index Alpha — LOB liquidity fragility detector.

Signal:
    HHI_bid = sum( (d_i / D_bid)^2 )  for each bid level i
    HHI_ask = sum( (d_i / D_ask)^2 )  for each ask level i
    raw     = HHI_ask - HHI_bid        (positive => asks fragile => bullish)
    signal  = EMA_16(raw)

Hypothesis:
    The Herfindahl-Hirschman Index of order-book depth distribution across
    price levels reveals liquidity fragility.  When depth on the ask side
    is concentrated at a single level (high HHI_ask), aggressive buying can
    punch through that level quickly, causing upward price pressure.
    Conversely, concentrated bid depth signals downward fragility.

Academic refs:
    - Kyle (1985): lambda and depth distribution
    - Cont, Stoikov & Talreja (2010): LOB shape dynamics
    - Huang & Polak (2011): LOB depth concentration and volatility

Allocator Law : __slots__ on class; all state is scalar.
Precision Law : output is float (signal score, not price).
Latency profile: shioaji_sim_p95_v2026-03-04 (TWSE / Shioaji P95 RTT).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 16 ticks => alpha = 1 - exp(-1/16)
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 16.0)
_EPSILON: float = 1e-12  # guards division by zero in HHI

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="depth_concentration_index",
    hypothesis=(
        "The Herfindahl-Hirschman Index of LOB depth distribution across"
        " price levels reveals liquidity fragility.  When ask-side depth"
        " is concentrated at best level (high HHI), the ask side is"
        " vulnerable to rapid depletion, predicting upward price pressure."
        " The asymmetry HHI_ask - HHI_bid is a directional predictor."
    ),
    formula="signal = EMA_16( HHI_ask - HHI_bid )",
    paper_refs=("kyle1985", "cont_stoikov_talreja2010", "huang_polak2011"),
    data_fields=("bids", "asks"),
    complexity="O(L)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


def _hhi(quantities: tuple[float, ...]) -> float:
    """Compute Herfindahl-Hirschman Index for a depth distribution.

    HHI = sum( (q_i / Q)^2 )  where Q = sum(q_i).
    Returns 1.0 when all depth is at one level (maximum concentration),
    approaches 1/N when depth is evenly distributed.
    """
    total = 0.0
    for q in quantities:
        total += q
    if total <= _EPSILON:
        return 0.0
    inv_total = 1.0 / total
    hhi_val = 0.0
    for q in quantities:
        share = q * inv_total
        hhi_val += share * share
    return hhi_val


class DepthConcentrationIndexAlpha:
    """O(L) depth-concentration fragility predictor with EMA smoothing.

    update() accepts:
      - keyword args: bids=np.ndarray (N,2), asks=np.ndarray (N,2)
        where column 0 = price, column 1 = quantity
      - keyword args: bid_qtys=tuple/list, ask_qtys=tuple/list
        (pre-extracted quantity vectors)
    """

    __slots__ = ("_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ARG002
        """Compute HHI asymmetry and update EMA.

        Accepts keyword arguments (in priority order):
          1. bids, asks: np.ndarray shape (N, 2) — col 0 = price, col 1 = qty
          2. bid_qtys, ask_qtys: sequence of floats — depth quantities per level
          3. bid_qty, ask_qty: float — L1 only fallback (monitor/backtest bridge)
             In L1 mode, uses depth ratio as proxy: (ask_qty - bid_qty) / total
        """
        bid_qtys: tuple[float, ...]
        ask_qtys: tuple[float, ...]

        if "bid_qtys" in kwargs and "ask_qtys" in kwargs:
            bid_qtys = tuple(float(q) for q in kwargs["bid_qtys"])  # type: ignore[union-attr]
            ask_qtys = tuple(float(q) for q in kwargs["ask_qtys"])  # type: ignore[union-attr]
        elif "bids" in kwargs and "asks" in kwargs:
            import numpy as np  # lazy import; not on hot path

            bids_arr = np.asarray(kwargs["bids"]).reshape(-1, 2)
            asks_arr = np.asarray(kwargs["asks"]).reshape(-1, 2)
            bid_qtys = tuple(float(q) for q in bids_arr[:, 1])
            ask_qtys = tuple(float(q) for q in asks_arr[:, 1])
        elif "bid_qty" in kwargs and "ask_qty" in kwargs:
            # L1 fallback: single-level depth ratio as HHI proxy.
            # With one level per side, HHI = 1.0 always, so we use
            # normalized depth asymmetry instead: (ask - bid) / (ask + bid).
            # This preserves the sign convention: positive = asks fragile = bullish.
            bq = float(kwargs["bid_qty"])
            aq = float(kwargs["ask_qty"])
            denom = bq + aq
            raw = (aq - bq) / (denom + _EPSILON) if denom > _EPSILON else 0.0
            return self._update_ema(raw)
        else:
            # Silently return current signal if no recognized fields
            # (graceful degradation for monitor payloads with partial data)
            return self._signal

        hhi_bid = _hhi(bid_qtys)
        hhi_ask = _hhi(ask_qtys)
        raw = hhi_ask - hhi_bid  # positive => ask fragile => bullish
        return self._update_ema(raw)

    def _update_ema(self, raw: float) -> float:
        """Apply EMA smoothing and update signal."""
        if not self._initialized:
            self._ema = raw
            self._initialized = True
        else:
            self._ema += _EMA_ALPHA * (raw - self._ema)
        self._signal = self._ema
        return self._signal

    def reset(self) -> None:
        self._ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = DepthConcentrationIndexAlpha

__all__ = ["DepthConcentrationIndexAlpha", "ALPHA_CLASS"]
