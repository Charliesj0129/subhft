"""LOB feature computation kernels (Python reference + Rust adapter).

Extracted from engine.py to separate computation from state management.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


try:
    try:
        _rust_core = importlib.import_module("hft_platform.rust_core")
    except Exception:
        _rust_core = importlib.import_module("rust_core")
    _RUST_LOB_FEATURE_KERNEL_V1 = getattr(_rust_core, "LobFeatureKernelV1", None)
    _RUST_FEATURE_PIPELINE_V1 = getattr(_rust_core, "RustFeaturePipelineV1", None)
except Exception:
    _RUST_LOB_FEATURE_KERNEL_V1 = None
    _RUST_FEATURE_PIPELINE_V1 = None


def rust_backend_available() -> bool:
    return _RUST_LOB_FEATURE_KERNEL_V1 is not None


def _top_qty(side: Any) -> int | None:
    """Extract top-of-book quantity from a bid/ask side array."""
    if side is None:
        return None
    try:
        if hasattr(side, "size"):
            if int(getattr(side, "size", 0)) <= 0:
                return 0
            return int(side[0][1])
        if len(side) <= 0:
            return 0
        top = side[0]
        return int(top[1]) if len(top) > 1 else 0
    except Exception:
        return None


def extract_l1_qty(event: object | None, bid_depth_fallback: int, ask_depth_fallback: int) -> tuple[int, int]:
    """Extract L1 bid/ask quantities from an event or use depth fallbacks."""
    if event is None:
        return int(max(0, bid_depth_fallback)), int(max(0, ask_depth_fallback))

    bids = getattr(event, "bids", None)
    asks = getattr(event, "asks", None)

    bq = _top_qty(bids)
    aq = _top_qty(asks)
    if bq is None:
        bq = int(max(0, bid_depth_fallback))
    if aq is None:
        aq = int(max(0, ask_depth_fallback))
    return int(max(0, bq)), int(max(0, aq))


def compute_ofi_l1_raw(
    best_bid: int,
    best_ask: int,
    bid_qty: int,
    ask_qty: int,
    prev_best_bid: int,
    prev_best_ask: int,
    prev_bid_qty: int,
    prev_ask_qty: int,
) -> int:
    """Standard L1 OFI decomposition."""
    if best_bid > prev_best_bid:
        b_flow = bid_qty
    elif best_bid == prev_best_bid:
        b_flow = bid_qty - prev_bid_qty
    else:
        b_flow = -prev_bid_qty

    if best_ask > prev_best_ask:
        a_flow = -prev_ask_qty
    elif best_ask == prev_best_ask:
        a_flow = ask_qty - prev_ask_qty
    else:
        a_flow = ask_qty

    return int(b_flow - a_flow)


def compute_changed_mask(
    prev_values: tuple[int | float, ...] | None,
    new_values: tuple[int | float, ...],
) -> int:
    """Compute bitmask of features that changed between prev and new values."""
    if prev_values is None or len(prev_values) != len(new_values):
        return (1 << len(new_values)) - 1 if new_values else 0
    mask = 0
    for idx, (a, b) in enumerate(zip(prev_values, new_values)):
        if a != b:
            mask |= 1 << idx
    return mask


@dataclass(slots=True)
class SymbolState:
    """Combined per-symbol state: feature output + LOB kernel rolling state."""

    seq: int = 0
    source_ts_ns: int = 0
    local_ts_ns: int = 0
    values: tuple[int | float, ...] = ()
    warm_count: int = 0
    quality_flags: int = 0
    prev_best_bid: int = 0
    prev_best_ask: int = 0
    prev_l1_bid_qty: int = 0
    prev_l1_ask_qty: int = 0
    ofi_l1_cum: int = 0
    ofi_l1_ema8: float = 0.0
    spread_ema8: float = 0.0
    imbalance_ema8_ppm: float = 0.0
    initialized: bool = False

    def update_output(
        self,
        seq: int,
        source_ts_ns: int,
        local_ts_ns: int,
        values: tuple[int | float, ...],
        warm_count: int,
        quality_flags: int,
    ) -> None:
        self.seq = seq
        self.source_ts_ns = source_ts_ns
        self.local_ts_ns = local_ts_ns
        self.values = values
        self.warm_count = warm_count
        self.quality_flags = quality_flags


class LobFeatureKernel:
    """Python reference kernel for LOB feature computation."""

    __slots__ = ("_ema_alpha", "_ofi_enabled")

    def __init__(self, ema_alpha: float, ofi_enabled: bool) -> None:
        self._ema_alpha = ema_alpha
        self._ofi_enabled = ofi_enabled

    def compute(
        self,
        state: SymbolState,
        bb: int,
        ba: int,
        mid: int,
        spread: int,
        bd: int,
        ad: int,
        l1bq: int,
        l1aq: int,
    ) -> tuple[int, ...]:
        """Compute all 16 features, updating rolling state in-place."""
        depth_total = bd + ad
        if depth_total > 0:
            imbalance_ppm = int(round(((bd - ad) * 1_000_000.0) / float(depth_total)))
        else:
            imbalance_ppm = 0

        l1_total = l1bq + l1aq
        if l1_total > 0:
            l1_imbalance_ppm = int(round(((l1bq - l1aq) * 1_000_000.0) / float(l1_total)))
            microprice_x2 = int(round((2.0 * ((ba * l1bq) + (bb * l1aq))) / float(l1_total)))
        else:
            l1_imbalance_ppm = 0
            microprice_x2 = mid

        alpha = float(self._ema_alpha)

        if not state.initialized:
            ofi_l1_raw = 0
            ofi_l1_cum = 0
            ofi_l1_ema8 = 0
            state.spread_ema8 = float(spread)
            state.imbalance_ema8_ppm = float(l1_imbalance_ppm)
            spread_ema8_scaled = int(round(state.spread_ema8))
            depth_imbalance_ema8_ppm = int(round(state.imbalance_ema8_ppm))
            state.initialized = True
        else:
            if self._ofi_enabled:
                ofi_l1_raw = compute_ofi_l1_raw(
                    best_bid=bb,
                    best_ask=ba,
                    bid_qty=l1bq,
                    ask_qty=l1aq,
                    prev_best_bid=state.prev_best_bid,
                    prev_best_ask=state.prev_best_ask,
                    prev_bid_qty=state.prev_l1_bid_qty,
                    prev_ask_qty=state.prev_l1_ask_qty,
                )
                state.ofi_l1_cum += ofi_l1_raw
                state.ofi_l1_ema8 = (1.0 - alpha) * state.ofi_l1_ema8 + alpha * float(ofi_l1_raw)
            else:
                ofi_l1_raw = 0
                state.ofi_l1_cum = 0
                state.ofi_l1_ema8 = 0.0
            state.spread_ema8 = (1.0 - alpha) * state.spread_ema8 + alpha * float(spread)
            state.imbalance_ema8_ppm = (1.0 - alpha) * state.imbalance_ema8_ppm + alpha * float(l1_imbalance_ppm)
            ofi_l1_cum = int(state.ofi_l1_cum)
            ofi_l1_ema8 = int(round(state.ofi_l1_ema8))
            spread_ema8_scaled = int(round(state.spread_ema8))
            depth_imbalance_ema8_ppm = int(round(state.imbalance_ema8_ppm))

        state.prev_best_bid = bb
        state.prev_best_ask = ba
        state.prev_l1_bid_qty = l1bq
        state.prev_l1_ask_qty = l1aq

        return (
            bb,
            ba,
            mid,
            spread,
            bd,
            ad,
            imbalance_ppm,
            microprice_x2,
            l1bq,
            l1aq,
            l1_imbalance_ppm,
            int(ofi_l1_raw),
            int(ofi_l1_cum),
            int(ofi_l1_ema8),
            int(spread_ema8_scaled),
            int(depth_imbalance_ema8_ppm),
        )


class RustFeatureKernelAdapter:
    """Adapter for Rust LobFeatureKernelV1 / RustFeaturePipelineV1."""

    __slots__ = (
        "_kernels",
        "_pipelines",
        "_ema_alpha",
        "_warmup_thresholds",
        "_fallback_warned",
    )

    def __init__(self, ema_alpha: float, warmup_thresholds: list[int]) -> None:
        self._kernels: dict[str, Any] = {}
        self._pipelines: dict[str, Any] = {}
        self._ema_alpha = ema_alpha
        self._warmup_thresholds = warmup_thresholds
        self._fallback_warned: set[str] = set()

    def reset_symbol(self, symbol: str) -> None:
        kernel = self._kernels.pop(symbol, None)
        if kernel is not None:
            try:
                reset = getattr(kernel, "reset", None)
                if callable(reset):
                    reset()
            except Exception:
                pass
        pipeline = self._pipelines.pop(symbol, None)
        if pipeline is not None:
            try:
                reset = getattr(pipeline, "reset", None)
                if callable(reset):
                    reset()
            except Exception:
                pass

    def reset_all(self) -> None:
        self._kernels.clear()
        self._pipelines.clear()

    def compute(
        self,
        symbol: str,
        bb: int,
        ba: int,
        mid: int,
        spread: int,
        bd: int,
        ad: int,
        l1bq: int,
        l1aq: int,
    ) -> tuple[int, ...] | None:
        """Compute via Rust kernel. Returns None if Rust unavailable."""
        if _RUST_LOB_FEATURE_KERNEL_V1 is None:
            return None
        kernel = self._kernels.get(symbol)
        if kernel is None:
            try:
                kernel = _RUST_LOB_FEATURE_KERNEL_V1(ema_alpha=float(self._ema_alpha))
            except TypeError:
                kernel = _RUST_LOB_FEATURE_KERNEL_V1()
            self._kernels[symbol] = kernel
        out = kernel.update(bb, ba, mid, spread, bd, ad, l1bq, l1aq)
        if not isinstance(out, tuple):
            out = tuple(out)
        return out

    def compute_fused(
        self,
        symbol: str,
        bb: int,
        ba: int,
        mid: int,
        spread: int,
        bd: int,
        ad: int,
        l1bq: int,
        l1aq: int,
        warm_count: int,
    ) -> tuple[tuple[int, ...], int, int] | None:
        """Fused Rust pipeline: returns (values, changed_mask, warmup_ready_mask) or None."""
        if _RUST_FEATURE_PIPELINE_V1 is None:
            return None
        try:
            pipeline = self._pipelines.get(symbol)
            if pipeline is None:
                pipeline = _RUST_FEATURE_PIPELINE_V1(self._warmup_thresholds, ema_alpha=float(self._ema_alpha))
                self._pipelines[symbol] = pipeline
            values_list, changed_mask, warmup_mask = pipeline.process(
                bb,
                ba,
                mid,
                spread,
                bd,
                ad,
                l1bq,
                l1aq,
                warm_count,
            )
            if not isinstance(values_list, tuple):
                values_list = tuple(values_list)
            return (values_list, int(changed_mask), int(warmup_mask))
        except Exception as exc:
            if symbol not in self._fallback_warned:
                self._fallback_warned.add(symbol)
                logger.warning("rust_feature_kernel_fallback", symbol=symbol, error=str(exc))
            return None
