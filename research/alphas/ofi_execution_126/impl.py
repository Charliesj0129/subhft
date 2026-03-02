from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier


class OfiExecution126Alpha:
    __slots__ = (
        "_signal",
        "_ofi_ema",
        "_imbalance_ema",
        "_spread_ema",
        "_micro_mom_ema",
        "_prev_bid_qty",
        "_prev_ask_qty",
        "_prev_bid_px",
        "_prev_ask_px",
        "_prev_micro_dev",
        "_initialized",
    )

    _EMA_ALPHA = 1.0 - math.exp(-1.0 / 8.0)
    _MICRO_ALPHA = 1.0 - math.exp(-1.0 / 100.0)
    _EPS = 1e-8
    _SPREAD_PENALTY = 0.15

    def __init__(self) -> None:
        self._signal = 0.0
        self._ofi_ema = 0.0
        self._imbalance_ema = 0.0
        self._spread_ema = 0.0
        self._micro_mom_ema = 0.0
        self._prev_bid_qty = 0.0
        self._prev_ask_qty = 0.0
        self._prev_bid_px = 0.0
        self._prev_ask_px = 0.0
        self._prev_micro_dev = 0.0
        self._initialized = False

    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="ofi_execution_126",
            hypothesis=(
                "Execution alpha improves when microprice momentum and OFI align, "
                "while spread widening should reduce signal strength."
            ),
            formula=(
                "alpha_t = 0.75*clip(EMA100(Δmicro_dev_t),[-1,1]) "
                "+ 0.20*clip(EMA8(OFI_proxy_t),[-1,1]) "
                "- 0.15*EMA8(spread_t/mid_t)"
            ),
            paper_refs=("126",),
            data_fields=("bid_qty", "ask_qty", "bid_px", "ask_px", "mid_price"),
            complexity="O(1)",
            status=AlphaStatus.DRAFT,
            tier=AlphaTier.ENSEMBLE,
            rust_module=None,
            latency_profile=None,
            roles_used=("planner", "code-reviewer"),
            skills_used=("iterative-retrieval", "validation-gate"),
            feature_set_version="lob_shared_v1",
        )

    def update(
        self,
        bid_qty: float = 0.0,
        ask_qty: float = 0.0,
        bid_px: float = 0.0,
        ask_px: float = 0.0,
        mid_price: float = 0.0,
        *args,
        **kwargs,
    ) -> float:
        bid_q = float(kwargs.get("bid_qty", kwargs.get("l1_bid_qty", bid_qty)))
        ask_q = float(kwargs.get("ask_qty", kwargs.get("l1_ask_qty", ask_qty)))
        bid_p = float(kwargs.get("bid_px", kwargs.get("best_bid", bid_px)))
        ask_p = float(kwargs.get("ask_px", kwargs.get("best_ask", ask_px)))
        mid = float(kwargs.get("mid_price", kwargs.get("current_mid", mid_price)))

        if mid <= 0.0:
            if bid_p > 0.0 and ask_p > 0.0:
                mid = 0.5 * (bid_p + ask_p)
            else:
                mid = 1.0

        denom = bid_q + ask_q + self._EPS
        queue_imbalance = (bid_q - ask_q) / denom

        was_initialized = self._initialized

        # OFI proxy with price-move-aware queue reset terms.
        if not was_initialized:
            ofi_proxy = 0.0
            self._initialized = True
        else:
            delta_bid = bid_q - self._prev_bid_qty
            delta_ask = ask_q - self._prev_ask_qty
            if bid_p > self._prev_bid_px:
                delta_bid = bid_q
            elif bid_p < self._prev_bid_px:
                delta_bid = -self._prev_bid_qty
            if ask_p < self._prev_ask_px:
                delta_ask = ask_q
            elif ask_p > self._prev_ask_px:
                delta_ask = -self._prev_ask_qty
            ofi_proxy = (delta_bid - delta_ask) / denom

        spread = max(0.0, ask_p - bid_p) / max(mid, 1.0)
        micro_price = ((bid_p * ask_q) + (ask_p * bid_q)) / max(denom, 1.0)
        micro_dev = (micro_price - mid) / max(mid, 1.0)
        micro_delta = 0.0 if not was_initialized else (micro_dev - self._prev_micro_dev)

        alpha = self._EMA_ALPHA
        self._ofi_ema += alpha * (ofi_proxy - self._ofi_ema)
        self._imbalance_ema += alpha * (queue_imbalance - self._imbalance_ema)
        self._spread_ema += alpha * (spread - self._spread_ema)
        self._micro_mom_ema += self._MICRO_ALPHA * (micro_delta - self._micro_mom_ema)

        mom_term = max(-1.0, min(1.0, self._micro_mom_ema * 80000.0))
        ofi_term = max(-1.0, min(1.0, self._ofi_ema * 4.0))
        self._signal = (0.75 * mom_term) + (0.20 * ofi_term) - (self._SPREAD_PENALTY * self._spread_ema)

        self._prev_bid_qty = bid_q
        self._prev_ask_qty = ask_q
        self._prev_bid_px = bid_p
        self._prev_ask_px = ask_p
        self._prev_micro_dev = micro_dev
        return self._signal

    def reset(self) -> None:
        self._signal = 0.0
        self._ofi_ema = 0.0
        self._imbalance_ema = 0.0
        self._spread_ema = 0.0
        self._micro_mom_ema = 0.0
        self._prev_bid_qty = 0.0
        self._prev_ask_qty = 0.0
        self._prev_bid_px = 0.0
        self._prev_ask_px = 0.0
        self._prev_micro_dev = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = OfiExecution126Alpha
