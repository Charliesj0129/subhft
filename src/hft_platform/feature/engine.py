from __future__ import annotations

import importlib
import math
import os
from dataclasses import dataclass, field
from typing import Any

from structlog import get_logger

from hft_platform.core.timebase import now_ns as _now_ns
from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
from hft_platform.feature.profile import FeatureProfile
from hft_platform.feature.registry import FeatureRegistry, default_feature_registry

logger = get_logger("feature.engine")


def _safe_int_round(val: float, default: int = 0) -> int:
    """Convert float to int, returning default if NaN/Inf."""
    if not math.isfinite(val):
        return default
    return int(round(val))


try:
    try:
        _rust_core = importlib.import_module("hft_platform.rust_core")
    except Exception as _exc:  # noqa: BLE001
        _rust_core = importlib.import_module("rust_core")
    _RUST_LOB_FEATURE_KERNEL_V1 = getattr(_rust_core, "LobFeatureKernelV1", None)
    _RUST_FEATURE_PIPELINE_V1 = getattr(_rust_core, "RustFeaturePipelineV1", None)
except Exception as _exc:  # noqa: BLE001
    _RUST_LOB_FEATURE_KERNEL_V1 = None
    _RUST_FEATURE_PIPELINE_V1 = None


class _StatsTupleProxy:
    """Zero-allocation proxy: provides LOBStatsEvent-compatible attribute access over a raw tuple.

    Tuple layout from BookState.get_stats_tuple() (tagged):
        ("lobstats", symbol, ts, mid_price_x2, spread_scaled, imbalance, best_bid, best_ask, bid_depth, ask_depth)
    """

    __slots__ = ("_t",)

    def __init__(self, t: tuple) -> None:
        self._t = t

    @property
    def symbol(self) -> str:
        return self._t[1]

    @property
    def ts(self) -> int:
        return self._t[2]

    @property
    def mid_price_x2(self) -> int:
        return self._t[3]

    @property
    def spread_scaled(self) -> int:
        return self._t[4]

    @property
    def imbalance(self) -> float:
        return self._t[5]

    @property
    def best_bid(self) -> int:
        return self._t[6]

    @property
    def best_ask(self) -> int:
        return self._t[7]

    @property
    def bid_depth(self) -> int:
        return self._t[8]

    @property
    def ask_depth(self) -> int:
        return self._t[9]


def _top_qty(side: object) -> int | None:
    """Extract top-of-book quantity from a bid/ask side array.

    Module-level function (lifted from ``_extract_l1_qty`` closure) to avoid
    allocating a new function object on every hot-path call.
    """
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
    except Exception:  # noqa: BLE001
        return None


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


_RET_AUTOCOV_WINDOW = 40  # ~5s at 125ms tick cadence


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
    # --- v2 fields: ofi_depth_norm, ret_autocov, tob_survival ---
    # Ring buffer for mid_price_x2 returns (for autocovariance)
    ret_buf: list[int] = field(default_factory=lambda: [0] * _RET_AUTOCOV_WINDOW)  # pre-allocated ring buffer
    ret_buf_pos: int = 0  # write position in ring buffer
    ret_buf_count: int = 0  # number of valid entries
    prev_mid_price_x2: int = 0  # previous mid_price_x2 for delta
    # TOB survival: timestamp of last best price change
    last_tob_change_ns: int = 0
    # --- v2 fields: ISS (Impact Surprise Signal) ---
    iss_ema_ofi: float = 0.0
    iss_ema_ret: float = 0.0
    iss_ema_ofi2: float = 1.0
    iss_ema_ofi_ret: float = 0.0
    iss_baseline_ema: float = 0.0
    iss_prev_mid_x2: int = 0
    iss_tick_count: int = 0
    # --- v2 fields: MLDM (Multi-Level Depth Momentum) ---
    mldm_prev_bid_qty_l2: float = 0.0
    mldm_prev_bid_qty_l3: float = 0.0
    mldm_prev_bid_qty_l4: float = 0.0
    mldm_prev_bid_qty_l5: float = 0.0
    mldm_prev_ask_qty_l2: float = 0.0
    mldm_prev_ask_qty_l3: float = 0.0
    mldm_prev_ask_qty_l4: float = 0.0
    mldm_prev_ask_qty_l5: float = 0.0
    mldm_deep_ema_fast: float = 0.0
    mldm_deep_ema_slow: float = 0.0
    mldm_output_ema: float = 0.0
    mldm_tick_count: int = 0
    # --- Toxicity tracking (trade-signed) ---
    tox_signed_vol_ema: float = 0.0
    tox_total_vol_ema: float = 0.0
    tox_tick_count: int = 0
    # --- v3 fields: multi-window EMA aggregation ---
    agg_ofi_ema5s: float = 0.0
    agg_ofi_ema30s: float = 0.0
    agg_imb_ema5s: float = 0.0
    agg_spread_ema30s: float = 0.0
    agg_spread_ema300s: float = 0.0

    def has_nan(self) -> bool:
        """Check if any float EMA field contains NaN or Inf.

        Hot-path safe: explicit or-chain avoids any per-call allocation or
        attribute iteration overhead.
        """
        return (
            not math.isfinite(self.ofi_l1_ema8)
            or not math.isfinite(self.spread_ema8)
            or not math.isfinite(self.imbalance_ema8_ppm)
            or not math.isfinite(self.iss_ema_ofi)
            or not math.isfinite(self.iss_ema_ret)
            or not math.isfinite(self.iss_ema_ofi2)
            or not math.isfinite(self.iss_ema_ofi_ret)
            or not math.isfinite(self.iss_baseline_ema)
            or not math.isfinite(self.mldm_deep_ema_fast)
            or not math.isfinite(self.mldm_deep_ema_slow)
            or not math.isfinite(self.mldm_output_ema)
            or not math.isfinite(self.tox_signed_vol_ema)
            or not math.isfinite(self.tox_total_vol_ema)
            or not math.isfinite(self.agg_ofi_ema5s)
            or not math.isfinite(self.agg_ofi_ema30s)
            or not math.isfinite(self.agg_imb_ema5s)
            or not math.isfinite(self.agg_spread_ema30s)
            or not math.isfinite(self.agg_spread_ema300s)
        )


class FeatureEngine:
    """Shared LOB-derived feature computation and cache (prototype).

    v1 scope:
    - Consumes `LOBStatsEvent`
    - Computes a small shared feature set (`lob_shared_v1`)
    - Exposes latest per-symbol values + emits `FeatureUpdateEvent`
    """

    _feature_profile: FeatureProfile | None

    __slots__ = (
        "_registry",
        "_feature_set",
        "_feature_ids",
        "_index_by_id",
        "_states",
        "_lob_kernel_states",
        "_rust_kernels",
        "_rust_pipelines",
        "_kernel_backend",
        "_seq",
        "_emit_events",
        "_quality_flags_next",
        "_feature_profile",
        "_ema_alpha",
        "_ofi_enabled",
        "_alpha_5s",
        "_alpha_30s",
        "_alpha_300s",
        "_event_cache",
        "_max_symbols",
        "_last_update_ns",
        "_full_warmup_mask",
        "_warmup_ready_symbols",
    )

    def __init__(
        self,
        registry: FeatureRegistry | None = None,
        *,
        feature_set_id: str | None = None,
        emit_events: bool | None = None,
        kernel_backend: str | None = None,
        feature_profile: FeatureProfile | None = None,
    ) -> None:
        self._registry = registry or default_feature_registry()
        self._feature_set = self._registry.get(feature_set_id) if feature_set_id else self._registry.get_default()
        self._feature_ids = self._feature_set.feature_ids
        self._index_by_id = self._feature_set.index_by_id
        self._states: dict[str, _FeatureState] = {}
        self._lob_kernel_states: dict[str, _LobKernelState] = {}
        self._rust_kernels: dict[str, Any] = {}
        self._rust_pipelines: dict[str, Any] = {}
        self._max_symbols: int = int(os.getenv("HFT_EXPOSURE_MAX_SYMBOLS", "10000"))
        self._last_update_ns: dict[str, int] = {}
        self._seq = 0
        if emit_events is None:
            emit_events = os.getenv("HFT_FEATURE_ENGINE_EMIT_EVENTS", "1").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
        self._emit_events = bool(emit_events)
        self._event_cache: dict[str, FeatureUpdateEvent] = {}
        self._quality_flags_next: dict[str, int] = {}
        self._feature_profile = None
        self._ema_alpha = 2.0 / 9.0
        self._ofi_enabled = True
        self._alpha_5s = 2.0 / 41.0
        self._alpha_30s = 2.0 / 241.0
        self._alpha_300s = 2.0 / 2401.0
        backend = str(kernel_backend or os.getenv("HFT_FEATURE_ENGINE_BACKEND", "python")).strip().lower()
        if backend == "rust" and _RUST_LOB_FEATURE_KERNEL_V1 is None:
            backend = "python"
        self._kernel_backend = backend if backend in {"python", "rust"} else "python"
        n = len(self._feature_set.features)
        self._full_warmup_mask: int = (1 << n) - 1 if n > 0 else 0
        self._warmup_ready_symbols: set[str] = set()
        if feature_profile is not None:
            self.apply_profile(feature_profile)

    def feature_set_id(self) -> str:
        return self._feature_set.feature_set_id

    def feature_ids(self) -> tuple[str, ...]:
        return self._feature_ids

    def schema_version(self) -> int:
        return int(self._feature_set.schema_version)

    def kernel_backend(self) -> str:
        return str(self._kernel_backend)

    def active_profile_id(self) -> str | None:
        prof = self._feature_profile
        return None if prof is None else str(prof.profile_id)

    def active_profile(self) -> FeatureProfile | None:
        return self._feature_profile

    def profile_params(self) -> dict[str, Any]:
        prof = self._feature_profile
        return dict(prof.params or {}) if prof is not None else {}

    def runtime_status(self) -> dict[str, Any]:
        return {
            "feature_set_id": self.feature_set_id(),
            "schema_version": self.schema_version(),
            "kernel_backend": self.kernel_backend(),
            "rust_backend_available": self.rust_backend_available(),
            "emit_events": bool(self._emit_events),
            "active_profile_id": self.active_profile_id(),
            "profile_params": self.profile_params(),
            "tracked_symbols": len(self._states),
        }

    def apply_profile(self, profile: FeatureProfile) -> None:
        if str(profile.feature_set_id) != self._feature_set.feature_set_id:
            raise ValueError(
                f"Feature profile {profile.profile_id!r} targets {profile.feature_set_id!r}, "
                f"but engine uses {self._feature_set.feature_set_id!r}"
            )
        if profile.schema_version is not None and int(profile.schema_version) > int(self._feature_set.schema_version):
            raise ValueError(
                f"Feature profile schema_version={profile.schema_version} exceeds runtime schema "
                f"{self._feature_set.schema_version}"
            )
        self._feature_profile = profile
        params = dict(profile.params or {})
        ema_window = int(params.get("ema_window", 8) or 8)
        ema_window = max(1, ema_window)
        self._ema_alpha = 2.0 / float(ema_window + 1)
        self._ofi_enabled = str(params.get("ofi_enabled", True)).strip().lower() not in {"0", "false", "no", "off"}

    def rust_backend_available(self) -> bool:
        return _RUST_LOB_FEATURE_KERNEL_V1 is not None

    def last_update_ns(self, symbol: str) -> int | None:
        """Return the wall-clock ns timestamp of the last successful update, or None if never updated."""
        return self._last_update_ns.get(str(symbol))

    def has_symbol(self, symbol: str) -> bool:
        return str(symbol) in self._states

    def reset_symbol(self, symbol: str) -> None:
        symbol = str(symbol)
        self._states.pop(symbol, None)
        self._lob_kernel_states.pop(symbol, None)
        self._last_update_ns.pop(symbol, None)
        self._warmup_ready_symbols.discard(symbol)
        kernel = self._rust_kernels.pop(symbol, None)
        if kernel is not None:
            try:
                reset = getattr(kernel, "reset", None)
                if callable(reset):
                    reset()
            except Exception as _exc:  # noqa: BLE001
                pass
        pipeline = self._rust_pipelines.pop(symbol, None)
        if pipeline is not None:
            try:
                reset = getattr(pipeline, "reset", None)
                if callable(reset):
                    reset()
            except Exception as _exc:  # noqa: BLE001
                pass
        self._quality_flags_next[symbol] = self._quality_flags_next.get(symbol, 0) | QUALITY_FLAG_STATE_RESET

    def reset_all(self) -> None:
        for symbol in list(self._states):
            self._quality_flags_next[symbol] = self._quality_flags_next.get(symbol, 0) | QUALITY_FLAG_STATE_RESET
        self._states.clear()
        self._lob_kernel_states.clear()
        self._rust_kernels.clear()
        self._rust_pipelines.clear()
        self._last_update_ns.clear()

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
            "feature_profile_id": self.active_profile_id(),
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
        stats: LOBStatsEvent | tuple,
        *,
        local_ts_ns: int | None = None,
    ) -> FeatureUpdateEvent | None:
        # Tuple fast path: extract fields positionally to avoid LOBStatsEvent construction.
        # See _StatsTupleProxy for tuple layout definition.
        stats_resolved: LOBStatsEvent | _StatsTupleProxy
        if isinstance(stats, tuple):
            symbol = str(stats[1])
            source_ts_ns = int(stats[2])
            stats_resolved = _StatsTupleProxy(stats)
        else:
            symbol = str(stats.symbol)
            source_ts_ns = int(getattr(stats, "ts", 0) or 0)
            stats_resolved = stats
        self._seq += 1
        seq = self._seq
        local_ts_ns = int(source_ts_ns if local_ts_ns is None else local_ts_ns)

        prev = self._states.get(symbol)
        if prev is None and len(self._states) >= self._max_symbols:
            logger.warning(
                "feature_symbol_cardinality_exceeded",
                current=len(self._states),
                limit=self._max_symbols,
                symbol=symbol,
            )
            return None
        warm_count = (prev.warm_count + 1) if prev else 1

        # Fused Rust pipeline: compute values + changed_mask + warmup_mask in one call
        fused = None
        if self._kernel_backend == "rust":
            fused = self._compute_fused_rust(symbol, event, stats_resolved, warm_count)

        if fused is not None:
            values, changed_mask, warmup_ready_mask = fused
        else:
            values = self._compute_values(symbol, event, stats_resolved)
            # NaN/Inf contamination guard — reset kernel state if detected
            ks = self._lob_kernel_states.get(symbol)
            if ks is not None and ks.has_nan():
                logger.warning("feature_nan_detected", symbol=symbol)
                self.reset_symbol(symbol)
            changed_mask = self._compute_changed_mask(prev.values if prev else None, values)
            warmup_ready_mask = self._compute_warmup_ready_mask(warm_count, symbol)
        qflags = int(self._quality_flags_next.pop(symbol, 0))
        if prev is not None and source_ts_ns and source_ts_ns < prev.source_ts_ns:
            qflags |= QUALITY_FLAG_OUT_OF_ORDER

        # Hot-path exception: in-place mutation to avoid per-tick allocation
        if prev is not None:
            prev.seq = seq
            prev.source_ts_ns = source_ts_ns
            prev.local_ts_ns = local_ts_ns
            prev.values = values
            prev.warm_count = warm_count
            prev.quality_flags = qflags
        else:
            self._states[symbol] = _FeatureState(
                seq=seq,
                source_ts_ns=source_ts_ns,
                local_ts_ns=local_ts_ns,
                values=values,
                warm_count=warm_count,
                quality_flags=qflags,
            )

        self._last_update_ns[symbol] = _now_ns()

        if not self._emit_events:
            return None
        # Always create a new FeatureUpdateEvent per tick.
        # Previously the cached event object was mutated in-place and returned;
        # multiple downstream consumers (StrategyRunner, RecorderService) held the same
        # reference, so the next tick's mutation could corrupt data still being read.
        # _event_cache is kept to store the last emitted event per symbol for debugging,
        # but the returned object is always a fresh allocation.
        evt = FeatureUpdateEvent(
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
        self._event_cache[symbol] = evt
        return evt

    def _compute_values(
        self, symbol: str, event: object | None, stats: LOBStatsEvent | _StatsTupleProxy
    ) -> tuple[int, ...]:
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
            spread_ema8_scaled = _safe_int_round(ks.spread_ema8)
            depth_imbalance_ema8_ppm = _safe_int_round(ks.imbalance_ema8_ppm)
            # v3 EMA seed values
            ks.agg_ofi_ema5s = 0.0
            ks.agg_ofi_ema30s = 0.0
            ks.agg_imb_ema5s = float(l1_imbalance_ppm)
            ks.agg_spread_ema30s = float(spread_scaled)
            ks.agg_spread_ema300s = float(spread_scaled)
            ks.initialized = True
        else:
            if self._ofi_enabled:
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
                alpha = float(self._ema_alpha)
                ks.ofi_l1_ema8 = (1.0 - alpha) * ks.ofi_l1_ema8 + alpha * float(ofi_l1_raw)
            else:
                ofi_l1_raw = 0
                ks.ofi_l1_cum = 0
                ks.ofi_l1_ema8 = 0.0
                alpha = float(self._ema_alpha)
            ks.spread_ema8 = (1.0 - alpha) * ks.spread_ema8 + alpha * float(spread_scaled)
            ks.imbalance_ema8_ppm = (1.0 - alpha) * ks.imbalance_ema8_ppm + alpha * float(l1_imbalance_ppm)
            ofi_l1_cum = int(ks.ofi_l1_cum)
            ofi_l1_ema8 = _safe_int_round(ks.ofi_l1_ema8)
            spread_ema8_scaled = _safe_int_round(ks.spread_ema8)
            depth_imbalance_ema8_ppm = _safe_int_round(ks.imbalance_ema8_ppm)

        v1_tuple = (
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

        # --- v2 features ---
        # Must be computed BEFORE updating ks.prev_best_bid/ask (TOB survival needs old values).
        n_features = len(self._feature_ids)
        if n_features > 16:
            # Save previous BBO for MLDM guard (needed before BBO update below)
            _prev_bb_for_mldm = ks.prev_best_bid
            _prev_ba_for_mldm = ks.prev_best_ask

            v2_base = self._compute_v2_features(
                ks,
                stats,
                mid_price_x2,
                l1_bid_qty,
                l1_ask_qty,
                best_bid,
                best_ask,
            )

        ks.prev_best_bid = best_bid
        ks.prev_best_ask = best_ask
        ks.prev_l1_bid_qty = l1_bid_qty
        ks.prev_l1_ask_qty = l1_ask_qty

        if n_features <= 16:
            return v1_tuple

        if n_features <= 19:
            return (*v1_tuple, *v2_base)  # type: ignore[possibly-undefined]

        iss_val = self._compute_iss(ks, int(ofi_l1_raw), mid_price_x2, bid_depth, ask_depth)
        mldm_val = self._compute_mldm(ks, event, best_bid, best_ask, _prev_bb_for_mldm, _prev_ba_for_mldm)  # type: ignore[possibly-undefined]
        tox_val = self._compute_toxicity(ks)

        if n_features <= 22:
            return (*v1_tuple, *v2_base, iss_val, mldm_val, tox_val)  # type: ignore[possibly-undefined]

        # --- v3 EMA aggregation features ---
        a5 = self._alpha_5s
        a30 = self._alpha_30s
        a300 = self._alpha_300s

        ks.agg_ofi_ema5s += a5 * (float(ofi_l1_raw) - ks.agg_ofi_ema5s)
        ks.agg_ofi_ema30s += a30 * (float(ofi_l1_raw) - ks.agg_ofi_ema30s)
        ks.agg_imb_ema5s += a5 * (float(l1_imbalance_ppm) - ks.agg_imb_ema5s)
        ks.agg_spread_ema30s += a30 * (float(spread_scaled) - ks.agg_spread_ema30s)
        ks.agg_spread_ema300s += a300 * (float(spread_scaled) - ks.agg_spread_ema300s)

        return (
            *v1_tuple,
            *v2_base,  # type: ignore[possibly-undefined]
            iss_val,
            mldm_val,
            tox_val,
            _safe_int_round(ks.agg_ofi_ema5s),
            _safe_int_round(ks.agg_ofi_ema30s),
            _safe_int_round(ks.agg_imb_ema5s),
            _safe_int_round(ks.agg_spread_ema30s),
            _safe_int_round(ks.agg_spread_ema300s),
        )

    def _compute_v2_features(
        self,
        ks: _LobKernelState,
        stats: LOBStatsEvent | _StatsTupleProxy,
        mid_price_x2: int,
        l1_bid_qty: int,
        l1_ask_qty: int,
        best_bid: int,
        best_ask: int,
    ) -> tuple[int, int, int]:
        """Compute v2-only features: depth-norm OFI, return autocov, TOB survival."""
        # [16] ofi_depth_norm_ppm = ofi_ema8 / avg_l1_depth * 1_000_000
        avg_l1_depth = (l1_bid_qty + l1_ask_qty) // 2
        if avg_l1_depth > 0:
            ofi_depth_norm_ppm = int(round(ks.ofi_l1_ema8 * 1_000_000.0 / float(avg_l1_depth)))
        else:
            ofi_depth_norm_ppm = 0

        # [17] ret_autocov_5s_x1e6: lag-1 autocovariance of mid_price_x2 returns
        ret = mid_price_x2 - ks.prev_mid_price_x2 if ks.prev_mid_price_x2 != 0 else 0
        buf = ks.ret_buf
        pos = ks.ret_buf_pos
        buf[pos] = ret
        ks.ret_buf_pos = (pos + 1) % _RET_AUTOCOV_WINDOW
        if ks.ret_buf_count < _RET_AUTOCOV_WINDOW:
            ks.ret_buf_count += 1
        count = ks.ret_buf_count
        ret_autocov_5s_x1e6 = 0
        if count >= 3:
            autocov_sum = 0
            n_pairs = 0
            # oldest valid entry is at (pos - count + 1) % W
            oldest = pos - count + 1
            for i in range(1, count):
                idx_curr = (oldest + i) % _RET_AUTOCOV_WINDOW
                idx_prev = (oldest + i - 1) % _RET_AUTOCOV_WINDOW
                autocov_sum += buf[idx_curr] * buf[idx_prev]
                n_pairs += 1
            if n_pairs > 0:
                ret_autocov_5s_x1e6 = int(round(float(autocov_sum) * 1_000_000.0 / float(n_pairs)))
        ks.prev_mid_price_x2 = mid_price_x2

        # [18] tob_survival_ms: ms since last best price change
        source_ts_ns = int(getattr(stats, "ts", 0) or 0)
        tob_changed = (best_bid != ks.prev_best_bid) or (best_ask != ks.prev_best_ask)
        if tob_changed or ks.last_tob_change_ns == 0:
            ks.last_tob_change_ns = source_ts_ns
        if source_ts_ns > 0 and ks.last_tob_change_ns > 0:
            tob_survival_ms = int((source_ts_ns - ks.last_tob_change_ns) // 1_000_000)
        else:
            tob_survival_ms = 0

        return (int(ofi_depth_norm_ppm), int(ret_autocov_5s_x1e6), int(tob_survival_ms))

    # --- v2 feature methods: ISS + MLDM ---

    _ISS_EMA_ALPHA: float = 2.0 / 201.0  # span=200
    _ISS_BASELINE_ALPHA: float = 2.0 / 2001.0  # span=2000
    _ISS_VAR_MIN: float = 0.01
    _ISS_THRESHOLD: float = 0.3
    _ISS_WARMUP: int = 400
    _ISS_CLIP: float = 1.0

    _MLDM_EMA_FAST: float = 0.11750309741540453  # 1 - exp(-1/8)
    _MLDM_EMA_SLOW: float = 0.015503876312911768  # 1 - exp(-1/64)
    _MLDM_EMA_OUT: float = 0.06058693718652422  # 1 - exp(-1/16)
    _MLDM_CLIP: float = 2.0
    _MLDM_WARMUP: int = 128

    def _compute_iss(self, ks: "_LobKernelState", ofi_raw: int, mid_x2: int, bid_depth: int, ask_depth: int) -> int:
        """Compute Impact Surprise Signal. Returns scaled int x1000 (milli-units)."""

        total_depth = float(max(bid_depth + ask_depth, 1))
        depth_b_eq = 1.0 / (2.0 * total_depth + 1.0)

        ks.iss_tick_count += 1

        if ks.iss_tick_count <= 1:
            ks.iss_prev_mid_x2 = mid_x2
            ks.iss_baseline_ema = depth_b_eq
            return 0

        ret = float(mid_x2 - ks.iss_prev_mid_x2)
        ks.iss_prev_mid_x2 = mid_x2
        ofi_f = float(ofi_raw)
        a = self._ISS_EMA_ALPHA

        ks.iss_ema_ofi = (1.0 - a) * ks.iss_ema_ofi + a * ofi_f
        ks.iss_ema_ret = (1.0 - a) * ks.iss_ema_ret + a * ret
        ks.iss_ema_ofi2 = (1.0 - a) * ks.iss_ema_ofi2 + a * ofi_f * ofi_f
        ks.iss_ema_ofi_ret = (1.0 - a) * ks.iss_ema_ofi_ret + a * ofi_f * ret

        cov_hat = ks.iss_ema_ofi_ret - ks.iss_ema_ofi * ks.iss_ema_ret
        var_hat = ks.iss_ema_ofi2 - ks.iss_ema_ofi * ks.iss_ema_ofi

        b_hat = cov_hat / var_hat if var_hat > self._ISS_VAR_MIN else depth_b_eq

        ba = self._ISS_BASELINE_ALPHA
        ks.iss_baseline_ema = (1.0 - ba) * ks.iss_baseline_ema + ba * b_hat
        b_eq = ks.iss_baseline_ema if ks.iss_baseline_ema > 1e-15 else depth_b_eq

        if ks.iss_tick_count < self._ISS_WARMUP:
            return 0

        deviation = (b_hat - b_eq) / b_eq if b_eq > 1e-15 else 0.0
        if abs(deviation) < self._ISS_THRESHOLD:
            return 0

        raw = math.copysign(min(abs(deviation), self._ISS_CLIP), deviation)
        return int(round(max(-self._ISS_CLIP, min(self._ISS_CLIP, raw)) * 1000))

    def _compute_mldm(
        self,
        ks: "_LobKernelState",
        event: object | None,
        best_bid: int,
        best_ask: int,
        prev_best_bid: int = 0,
        prev_best_ask: int = 0,
    ) -> int:
        """Compute Multi-Level Depth Momentum. Returns scaled int x1000."""
        # Extract L2-L5 quantities directly from event as scalars (no list/array allocations)
        cb2 = cb3 = cb4 = cb5 = 0.0
        ca2 = ca3 = ca4 = ca5 = 0.0
        n_bid_levels = 0
        n_ask_levels = 0

        if event is not None:
            bids = getattr(event, "bids", None)
            asks = getattr(event, "asks", None)
            if bids is not None:
                try:
                    n_bid_levels = min(len(bids), 5)
                    if n_bid_levels > 1:
                        cb2 = float(bids[1][1]) if len(bids[1]) > 1 else 0.0
                    if n_bid_levels > 2:
                        cb3 = float(bids[2][1]) if len(bids[2]) > 1 else 0.0
                    if n_bid_levels > 3:
                        cb4 = float(bids[3][1]) if len(bids[3]) > 1 else 0.0
                    if n_bid_levels > 4:
                        cb5 = float(bids[4][1]) if len(bids[4]) > 1 else 0.0
                except Exception:
                    pass
            if asks is not None:
                try:
                    n_ask_levels = min(len(asks), 5)
                    if n_ask_levels > 1:
                        ca2 = float(asks[1][1]) if len(asks[1]) > 1 else 0.0
                    if n_ask_levels > 2:
                        ca3 = float(asks[2][1]) if len(asks[2]) > 1 else 0.0
                    if n_ask_levels > 3:
                        ca4 = float(asks[3][1]) if len(asks[3]) > 1 else 0.0
                    if n_ask_levels > 4:
                        ca5 = float(asks[4][1]) if len(asks[4]) > 1 else 0.0
                except Exception:
                    pass

        # BBO-shift guard: zero deep_net when best price changes
        bbo_shifted = ks.initialized and (best_bid != prev_best_bid or best_ask != prev_best_ask)
        thin_book = n_bid_levels < 2 or n_ask_levels < 2

        if bbo_shifted or thin_book or event is None:
            deep_net = 0.0
        else:
            # Read prev values directly from ks fields (no list allocation)
            deep_net = (
                (cb2 - ks.mldm_prev_bid_qty_l2)
                + (cb3 - ks.mldm_prev_bid_qty_l3)
                + (cb4 - ks.mldm_prev_bid_qty_l4)
                + (cb5 - ks.mldm_prev_bid_qty_l5)
            ) - (
                (ca2 - ks.mldm_prev_ask_qty_l2)
                + (ca3 - ks.mldm_prev_ask_qty_l3)
                + (ca4 - ks.mldm_prev_ask_qty_l4)
                + (ca5 - ks.mldm_prev_ask_qty_l5)
            )

        # Update stored prev quantities AFTER computing deep_net
        ks.mldm_prev_bid_qty_l2 = cb2
        ks.mldm_prev_bid_qty_l3 = cb3
        ks.mldm_prev_bid_qty_l4 = cb4
        ks.mldm_prev_bid_qty_l5 = cb5
        ks.mldm_prev_ask_qty_l2 = ca2
        ks.mldm_prev_ask_qty_l3 = ca3
        ks.mldm_prev_ask_qty_l4 = ca4
        ks.mldm_prev_ask_qty_l5 = ca5

        ks.mldm_deep_ema_fast += self._MLDM_EMA_FAST * (deep_net - ks.mldm_deep_ema_fast)
        ks.mldm_deep_ema_slow += self._MLDM_EMA_SLOW * (deep_net - ks.mldm_deep_ema_slow)
        raw_momentum = ks.mldm_deep_ema_fast - ks.mldm_deep_ema_slow
        ks.mldm_output_ema += self._MLDM_EMA_OUT * (raw_momentum - ks.mldm_output_ema)

        ks.mldm_tick_count += 1
        if ks.mldm_tick_count < self._MLDM_WARMUP:
            return 0

        clipped = max(-self._MLDM_CLIP, min(self._MLDM_CLIP, ks.mldm_output_ema))
        return int(round(clipped * 1000))

    def _compute_toxicity(self, ks: "_LobKernelState") -> int:
        """Compute toxicity_ema50_x1000: abs(signed_vol_ema / total_vol_ema) * 1000.

        Range: 0 (balanced flow) to 1000 (fully one-sided).
        """
        if ks.tox_tick_count < 50 or ks.tox_total_vol_ema < 1.0:
            return 0
        raw = abs(ks.tox_signed_vol_ema) / ks.tox_total_vol_ema
        return int(round(min(1.0, raw) * 1000))

    def on_tick(
        self,
        symbol: str,
        price: int,
        volume: int,
        trade_direction: int,
        trade_confidence: int,
    ) -> None:
        """Update trade-signed feature state from classified tick data.

        Called per-tick with EMO classification result.
        trade_direction: +1=BUY, -1=SELL, 0=UNKNOWN
        trade_confidence: 1000=at-quote, 800=inside, 500=tick-rule, 0=unknown
        """
        if trade_direction == 0 or volume <= 0:
            return

        ks = self._lob_kernel_states.get(symbol)
        if ks is None:
            return

        # EMA alpha for ~50 tick window
        alpha = 0.04  # 2/(50+1) ≈ 0.039

        signed_vol = float(trade_direction * volume)
        ks.tox_signed_vol_ema += alpha * (signed_vol - ks.tox_signed_vol_ema)
        ks.tox_total_vol_ema += alpha * (float(volume) - ks.tox_total_vol_ema)
        ks.tox_tick_count += 1

    def _compute_values_rust(
        self, symbol: str, event: object | None, stats: LOBStatsEvent | _StatsTupleProxy
    ) -> tuple[int, ...]:
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
        return tuple(out) if not isinstance(out, tuple) else out

    def _get_rust_pipeline(self, symbol: str) -> Any:
        """Get or create a RustFeaturePipelineV1 for the given symbol."""
        pipeline = self._rust_pipelines.get(symbol)
        if pipeline is None:
            cls = _RUST_FEATURE_PIPELINE_V1
            if cls is None:
                return None
            thresholds = [int(spec.warmup_min_events) for spec in self._feature_set.features]
            pipeline = cls(thresholds)
            self._rust_pipelines[symbol] = pipeline
        return pipeline

    def _compute_fused_rust(
        self,
        symbol: str,
        event: object | None,
        stats: LOBStatsEvent | _StatsTupleProxy,
        warm_count: int,
    ) -> tuple[tuple[int, ...], int, int] | None:
        """Fused Rust pipeline: returns (values, changed_mask, warmup_ready_mask) or None."""
        if _RUST_FEATURE_PIPELINE_V1 is None:
            return None
        try:
            best_bid = int(getattr(stats, "best_bid", 0) or 0)
            best_ask = int(getattr(stats, "best_ask", 0) or 0)
            mid_price_x2 = int(getattr(stats, "mid_price_x2", 0) or 0)
            spread_scaled = int(getattr(stats, "spread_scaled", 0) or 0)
            bid_depth = int(getattr(stats, "bid_depth", 0) or 0)
            ask_depth = int(getattr(stats, "ask_depth", 0) or 0)
            l1_bid_qty, l1_ask_qty = self._extract_l1_qty(event, bid_depth, ask_depth)

            pipeline = self._get_rust_pipeline(symbol)
            values_list, changed_mask, warmup_mask = pipeline.process(
                best_bid,
                best_ask,
                mid_price_x2,
                spread_scaled,
                bid_depth,
                ask_depth,
                l1_bid_qty,
                l1_ask_qty,
                warm_count,
            )
            return (
                tuple(values_list) if not isinstance(values_list, tuple) else values_list,
                int(changed_mask),
                int(warmup_mask),
            )
        except Exception as _exc:  # noqa: BLE001
            return None

    def _extract_l1_qty(
        self, event: object | None, bid_depth_fallback: int, ask_depth_fallback: int
    ) -> tuple[int, int]:
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

    def _compute_warmup_ready_mask(self, warm_count: int, symbol: str) -> int:
        # Fast path: once all features are warm for this symbol, skip the loop entirely.
        if symbol in self._warmup_ready_symbols:
            return self._full_warmup_mask
        mask = 0
        for idx, spec in enumerate(self._feature_set.features):
            if warm_count >= int(spec.warmup_min_events):
                mask |= 1 << idx
        if mask == self._full_warmup_mask:
            self._warmup_ready_symbols.add(symbol)
        return mask
