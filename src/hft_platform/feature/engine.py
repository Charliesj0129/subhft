from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Any

from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
from hft_platform.feature.registry import FeatureRegistry, default_feature_registry

try:
    try:
        _rust_core = importlib.import_module("hft_platform.rust_core")
    except Exception:
        _rust_core = importlib.import_module("rust_core")
    _RUST_LOB_FEATURE_KERNEL_V1 = getattr(_rust_core, "LobFeatureKernelV1", None)
except Exception:
    _RUST_LOB_FEATURE_KERNEL_V1 = None


QUALITY_FLAG_GAP = 1 << 0
QUALITY_FLAG_STATE_RESET = 1 << 1
QUALITY_FLAG_STALE_INPUT = 1 << 2
QUALITY_FLAG_OUT_OF_ORDER = 1 << 3
QUALITY_FLAG_PARTIAL = 1 << 4


@dataclass(slots=True)
class _FeatureState:
    seq: int
    source_ts_ns: int
    local_ts_ns: int
    values: tuple[int | float, ...]
    warm_count: int
    quality_flags: int = 0


@dataclass(slots=True)
class _LobKernelState:
    prev_best_bid: int = 0
    prev_best_ask: int = 0
    prev_l1_bid_qty: int = 0
    prev_l1_ask_qty: int = 0
    ofi_l1_cum: int = 0
    ofi_l1_ema8: float = 0.0
    spread_ema8: float = 0.0
    imbalance_ema8_ppm: float = 0.0
    initialized: bool = False


class FeatureEngine:
    """Shared LOB-derived feature computation and cache (prototype).

    v1 scope:
    - Consumes `LOBStatsEvent`
    - Computes a small shared feature set (`lob_shared_v1`)
    - Exposes latest per-symbol values + emits `FeatureUpdateEvent`
    """

    __slots__ = (
        "_registry",
        "_feature_set",
        "_feature_ids",
        "_index_by_id",
        "_states",
        "_lob_kernel_states",
        "_rust_kernels",
        "_kernel_backend",
        "_seq",
        "_emit_events",
        "_quality_flags_next",
    )

    def __init__(
        self,
        registry: FeatureRegistry | None = None,
        *,
        feature_set_id: str | None = None,
        emit_events: bool | None = None,
        kernel_backend: str | None = None,
    ) -> None:
        self._registry = registry or default_feature_registry()
        self._feature_set = self._registry.get(feature_set_id) if feature_set_id else self._registry.get_default()
        self._feature_ids = self._feature_set.feature_ids
        self._index_by_id = self._feature_set.index_by_id
        self._states: dict[str, _FeatureState] = {}
        self._lob_kernel_states: dict[str, _LobKernelState] = {}
        self._rust_kernels: dict[str, Any] = {}
        self._seq = 0
        if emit_events is None:
            emit_events = os.getenv("HFT_FEATURE_ENGINE_EMIT_EVENTS", "1").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
        self._emit_events = bool(emit_events)
        self._quality_flags_next: dict[str, int] = {}
        backend = str(kernel_backend or os.getenv("HFT_FEATURE_ENGINE_BACKEND", "python")).strip().lower()
        if backend == "rust" and _RUST_LOB_FEATURE_KERNEL_V1 is None:
            backend = "python"
        self._kernel_backend = backend if backend in {"python", "rust"} else "python"

    def feature_set_id(self) -> str:
        return self._feature_set.feature_set_id

    def feature_ids(self) -> tuple[str, ...]:
        return self._feature_ids

    def schema_version(self) -> int:
        return int(self._feature_set.schema_version)

    def kernel_backend(self) -> str:
        return str(self._kernel_backend)

    def rust_backend_available(self) -> bool:
        return _RUST_LOB_FEATURE_KERNEL_V1 is not None

    def has_symbol(self, symbol: str) -> bool:
        return str(symbol) in self._states

    def reset_symbol(self, symbol: str) -> None:
        symbol = str(symbol)
        self._states.pop(symbol, None)
        self._lob_kernel_states.pop(symbol, None)
        kernel = self._rust_kernels.pop(symbol, None)
        if kernel is not None:
            try:
                reset = getattr(kernel, "reset", None)
                if callable(reset):
                    reset()
            except Exception:
                pass
        self._quality_flags_next[symbol] = self._quality_flags_next.get(symbol, 0) | QUALITY_FLAG_STATE_RESET

    def reset_all(self) -> None:
        for symbol in list(self._states):
            self._quality_flags_next[symbol] = self._quality_flags_next.get(symbol, 0) | QUALITY_FLAG_STATE_RESET
        self._states.clear()
        self._lob_kernel_states.clear()
        self._rust_kernels.clear()

    def get_feature(self, symbol: str, feature_id: str) -> int | float | None:
        state = self._states.get(str(symbol))
        if state is None:
            return None
        idx = self._index_by_id.get(str(feature_id))
        if idx is None or idx >= len(state.values):
            return None
        return state.values[idx]

    def get_feature_by_index(self, symbol: str, idx: int) -> int | float | None:
        state = self._states.get(str(symbol))
        if state is None or idx < 0 or idx >= len(state.values):
            return None
        return state.values[idx]

    def get_feature_tuple(self, symbol: str) -> tuple[int | float, ...] | None:
        state = self._states.get(str(symbol))
        if state is None:
            return None
        return state.values

    def get_feature_view(self, symbol: str) -> dict[str, Any] | None:
        state = self._states.get(str(symbol))
        if state is None:
            return None
        return {
            "symbol": str(symbol),
            "feature_set_id": self._feature_set.feature_set_id,
            "schema_version": int(self._feature_set.schema_version),
            "seq": int(state.seq),
            "source_ts_ns": int(state.source_ts_ns),
            "local_ts_ns": int(state.local_ts_ns),
            "quality_flags": int(state.quality_flags),
            "feature_ids": self._feature_ids,
            "values": state.values,
        }

    def process_lob_stats(self, stats: LOBStatsEvent, *, local_ts_ns: int | None = None) -> FeatureUpdateEvent | None:
        return self.process_lob_update(None, stats, local_ts_ns=local_ts_ns)

    def process_lob_update(
        self,
        event: object | None,
        stats: LOBStatsEvent,
        *,
        local_ts_ns: int | None = None,
    ) -> FeatureUpdateEvent | None:
        symbol = str(stats.symbol)
        self._seq += 1
        seq = self._seq
        source_ts_ns = int(getattr(stats, "ts", 0) or 0)
        local_ts_ns = int(source_ts_ns if local_ts_ns is None else local_ts_ns)

        values = self._compute_values(symbol, event, stats)
        prev = self._states.get(symbol)
        changed_mask = self._compute_changed_mask(prev.values if prev else None, values)
        warm_count = (prev.warm_count + 1) if prev else 1
        warmup_ready_mask = self._compute_warmup_ready_mask(warm_count)
        qflags = int(self._quality_flags_next.pop(symbol, 0))
        if prev is not None and source_ts_ns and source_ts_ns < prev.source_ts_ns:
            qflags |= QUALITY_FLAG_OUT_OF_ORDER

        self._states[symbol] = _FeatureState(
            seq=seq,
            source_ts_ns=source_ts_ns,
            local_ts_ns=local_ts_ns,
            values=values,
            warm_count=warm_count,
            quality_flags=qflags,
        )

        if not self._emit_events:
            return None
        return FeatureUpdateEvent(
            symbol=symbol,
            ts=source_ts_ns,
            local_ts=local_ts_ns,
            seq=seq,
            feature_set_id=self._feature_set.feature_set_id,
            schema_version=int(self._feature_set.schema_version),
            changed_mask=changed_mask,
            warmup_ready_mask=warmup_ready_mask,
            quality_flags=qflags,
            feature_ids=self._feature_ids,
            values=values,
        )

    def _compute_values(self, symbol: str, event: object | None, stats: LOBStatsEvent) -> tuple[int, ...]:
        if self._kernel_backend == "rust":
            return self._compute_values_rust(symbol, event, stats)

        best_bid = int(getattr(stats, "best_bid", 0) or 0)
        best_ask = int(getattr(stats, "best_ask", 0) or 0)
        mid_price_x2 = int(getattr(stats, "mid_price_x2", 0) or 0)
        spread_scaled = int(getattr(stats, "spread_scaled", 0) or 0)
        bid_depth = int(getattr(stats, "bid_depth", 0) or 0)
        ask_depth = int(getattr(stats, "ask_depth", 0) or 0)
        l1_bid_qty, l1_ask_qty = self._extract_l1_qty(event, bid_depth, ask_depth)

        depth_total = bid_depth + ask_depth
        if depth_total > 0:
            imbalance_ppm = int(round(((bid_depth - ask_depth) * 1_000_000.0) / float(depth_total)))
        else:
            imbalance_ppm = 0

        l1_total = l1_bid_qty + l1_ask_qty
        if l1_total > 0:
            l1_imbalance_ppm = int(round(((l1_bid_qty - l1_ask_qty) * 1_000_000.0) / float(l1_total)))
            # Microprice *2 in scaled units: 2 * ((ask*bid_qty + bid*ask_qty)/(bid_qty+ask_qty))
            microprice_x2 = int(round((2.0 * ((best_ask * l1_bid_qty) + (best_bid * l1_ask_qty))) / float(l1_total)))
        else:
            l1_imbalance_ppm = 0
            microprice_x2 = mid_price_x2

        ks = self._lob_kernel_states.get(symbol)
        if ks is None:
            ks = _LobKernelState()
            self._lob_kernel_states[symbol] = ks

        if not ks.initialized:
            ofi_l1_raw = 0
            ofi_l1_cum = 0
            ofi_l1_ema8 = 0
            ks.spread_ema8 = float(spread_scaled)
            ks.imbalance_ema8_ppm = float(l1_imbalance_ppm)
            spread_ema8_scaled = int(round(ks.spread_ema8))
            depth_imbalance_ema8_ppm = int(round(ks.imbalance_ema8_ppm))
            ks.initialized = True
        else:
            ofi_l1_raw = self._compute_ofi_l1_raw(
                best_bid=best_bid,
                best_ask=best_ask,
                bid_qty=l1_bid_qty,
                ask_qty=l1_ask_qty,
                prev_best_bid=ks.prev_best_bid,
                prev_best_ask=ks.prev_best_ask,
                prev_bid_qty=ks.prev_l1_bid_qty,
                prev_ask_qty=ks.prev_l1_ask_qty,
            )
            ks.ofi_l1_cum += ofi_l1_raw
            alpha = 2.0 / 9.0  # EMA8
            ks.ofi_l1_ema8 = (1.0 - alpha) * ks.ofi_l1_ema8 + alpha * float(ofi_l1_raw)
            ks.spread_ema8 = (1.0 - alpha) * ks.spread_ema8 + alpha * float(spread_scaled)
            ks.imbalance_ema8_ppm = (1.0 - alpha) * ks.imbalance_ema8_ppm + alpha * float(l1_imbalance_ppm)
            ofi_l1_cum = int(ks.ofi_l1_cum)
            ofi_l1_ema8 = int(round(ks.ofi_l1_ema8))
            spread_ema8_scaled = int(round(ks.spread_ema8))
            depth_imbalance_ema8_ppm = int(round(ks.imbalance_ema8_ppm))

        ks.prev_best_bid = best_bid
        ks.prev_best_ask = best_ask
        ks.prev_l1_bid_qty = l1_bid_qty
        ks.prev_l1_ask_qty = l1_ask_qty

        return (
            best_bid,
            best_ask,
            mid_price_x2,
            spread_scaled,
            bid_depth,
            ask_depth,
            imbalance_ppm,
            microprice_x2,
            l1_bid_qty,
            l1_ask_qty,
            l1_imbalance_ppm,
            int(ofi_l1_raw),
            int(ofi_l1_cum),
            int(ofi_l1_ema8),
            int(spread_ema8_scaled),
            int(depth_imbalance_ema8_ppm),
        )

    def _compute_values_rust(self, symbol: str, event: object | None, stats: LOBStatsEvent) -> tuple[int, ...]:
        if _RUST_LOB_FEATURE_KERNEL_V1 is None:
            # Safety fallback in case runtime extension is unavailable.
            self._kernel_backend = "python"
            return self._compute_values(symbol, event, stats)

        best_bid = int(getattr(stats, "best_bid", 0) or 0)
        best_ask = int(getattr(stats, "best_ask", 0) or 0)
        mid_price_x2 = int(getattr(stats, "mid_price_x2", 0) or 0)
        spread_scaled = int(getattr(stats, "spread_scaled", 0) or 0)
        bid_depth = int(getattr(stats, "bid_depth", 0) or 0)
        ask_depth = int(getattr(stats, "ask_depth", 0) or 0)
        l1_bid_qty, l1_ask_qty = self._extract_l1_qty(event, bid_depth, ask_depth)

        kernel = self._rust_kernels.get(symbol)
        if kernel is None:
            kernel = _RUST_LOB_FEATURE_KERNEL_V1()
            self._rust_kernels[symbol] = kernel
        out = kernel.update(
            int(best_bid),
            int(best_ask),
            int(mid_price_x2),
            int(spread_scaled),
            int(bid_depth),
            int(ask_depth),
            int(l1_bid_qty),
            int(l1_ask_qty),
        )
        if not isinstance(out, tuple):
            out = tuple(out)
        return tuple(int(v) for v in out)

    def _extract_l1_qty(
        self, event: object | None, bid_depth_fallback: int, ask_depth_fallback: int
    ) -> tuple[int, int]:
        if event is None:
            return int(max(0, bid_depth_fallback)), int(max(0, ask_depth_fallback))

        bids = getattr(event, "bids", None)
        asks = getattr(event, "asks", None)

        def _top_qty(side) -> int | None:
            if side is None:
                return None
            try:
                if hasattr(side, "size"):
                    # numpy path
                    if int(getattr(side, "size", 0)) <= 0:
                        return 0
                    return int(side[0][1])
                if len(side) <= 0:
                    return 0
                top = side[0]
                return int(top[1]) if len(top) > 1 else 0
            except Exception:
                return None

        bq = _top_qty(bids)
        aq = _top_qty(asks)
        if bq is None:
            bq = int(max(0, bid_depth_fallback))
        if aq is None:
            aq = int(max(0, ask_depth_fallback))
        return int(max(0, bq)), int(max(0, aq))

    def _compute_ofi_l1_raw(
        self,
        *,
        best_bid: int,
        best_ask: int,
        bid_qty: int,
        ask_qty: int,
        prev_best_bid: int,
        prev_best_ask: int,
        prev_bid_qty: int,
        prev_ask_qty: int,
    ) -> int:
        # Standard L1 OFI decomposition (same sign conventions as OFI alpha reference path).
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

    def _compute_changed_mask(
        self,
        prev_values: tuple[int | float, ...] | None,
        new_values: tuple[int | float, ...],
    ) -> int:
        if prev_values is None or len(prev_values) != len(new_values):
            return (1 << len(new_values)) - 1 if new_values else 0
        mask = 0
        for idx, (a, b) in enumerate(zip(prev_values, new_values)):
            if a != b:
                mask |= 1 << idx
        return mask

    def _compute_warmup_ready_mask(self, warm_count: int) -> int:
        mask = 0
        for idx, spec in enumerate(self._feature_set.features):
            if warm_count >= int(spec.warmup_min_events):
                mask |= 1 << idx
        return mask
