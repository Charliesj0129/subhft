"""Alpha dispatch: load alphas, feed payloads, compute composite signal."""

from __future__ import annotations

import inspect
import math
from pathlib import Path
from typing import Any

import yaml
from structlog import get_logger

from hft_platform.monitor._types import AlphaState, SymbolState

logger = get_logger("monitor.alpha_dispatcher")

_MAX_CONSECUTIVE_ERRORS = 10


class AlphaDispatcher:
    """Loads alpha implementations and dispatches tick payloads to them."""

    __slots__ = (
        "_alpha_classes",
        "_weights",
        "_alpha_ids",
        "_weighted_ids",
    )

    def __init__(self) -> None:
        self._alpha_classes: dict[str, type[Any]] = {}
        self._weights: dict[str, float] = {}  # alpha_id -> weight from promotion
        self._alpha_ids: tuple[str, ...] = ()
        self._weighted_ids: tuple[str, ...] = ()  # pre-filtered: only IDs with weight > 0

    @property
    def alpha_ids(self) -> tuple[str, ...]:
        return self._alpha_ids

    @property
    def weights(self) -> dict[str, float]:
        return dict(self._weights)

    def load_alphas(
        self,
        alpha_ids: tuple[str, ...],
        alphas_dir: str | Path = "research/alphas",
        promotions_dir: str | Path = "config/strategy_promotions",
    ) -> list[str]:
        """Discover and load requested alphas. Returns list of successfully loaded IDs."""
        from research.registry.alpha_registry import AlphaRegistry

        registry = AlphaRegistry()
        discovered = registry.discover(alphas_dir)

        loaded: list[str] = []
        for aid in alpha_ids:
            alpha = discovered.get(aid)
            if alpha is None:
                logger.warning("alpha_not_found", alpha_id=aid)
                continue
            self._alpha_classes[aid] = alpha.__class__
            loaded.append(aid)

        self._alpha_ids = tuple(loaded)

        # Load promotion weights and pre-filter weighted IDs
        self._weights = _load_promotion_weights(promotions_dir)
        self._weighted_ids = tuple(aid for aid in self._alpha_ids if self._weights.get(aid, 0.0) > 0)
        logger.info(
            "alphas_loaded",
            loaded=loaded,
            weights={k: v for k, v in self._weights.items() if k in self._alpha_classes},
        )
        return loaded

    def bind_symbol(self, sym_state: SymbolState) -> None:
        """Bind fresh alpha runtimes to a symbol state, probing signatures."""
        for alpha_id in sym_state.symbol.alpha_ids:
            if alpha_id not in self._alpha_classes:
                continue
            runtime = self._alpha_classes[alpha_id]()
            dispatch_keys = _probe_dispatch_keys(runtime)
            filtered_buf = {k: 0.0 for k in dispatch_keys} if dispatch_keys else {}
            sym_state.alpha_states[alpha_id] = AlphaState(
                alpha_id=alpha_id,
                runtime=runtime,
                _dispatch_keys=dispatch_keys,
                _filtered_buf=filtered_buf,
            )

    def reset_symbol(self, sym_state: SymbolState) -> None:
        """Reset per-symbol alpha runtimes and derived state."""
        sym_state.alpha_states.clear()
        sym_state.composite = 0.0
        sym_state.sparkline_clear()
        self.bind_symbol(sym_state)

    def dispatch(
        self,
        sym_state: SymbolState,
        payload: dict[str, Any],
    ) -> None:
        """Feed payload to all alphas for this symbol, updating AlphaState."""
        if not sym_state.alpha_states:
            self.bind_symbol(sym_state)

        for alpha_id in sym_state.symbol.alpha_ids:
            astate = sym_state.alpha_states.get(alpha_id)
            if astate is None:
                runtime = self._alpha_classes[alpha_id]() if alpha_id in self._alpha_classes else None
                dispatch_keys = _probe_dispatch_keys(runtime) if runtime is not None else None
                filtered_buf = {k: 0.0 for k in dispatch_keys} if dispatch_keys else {}
                astate = AlphaState(
                    alpha_id=alpha_id,
                    runtime=runtime,
                    _dispatch_keys=dispatch_keys,
                    _filtered_buf=filtered_buf,
                )
                sym_state.alpha_states[alpha_id] = astate

            if astate.disabled:
                continue

            if astate.runtime is None:
                continue

            signal = _call_alpha(astate, payload)
            if signal is None:
                astate.error_count += 1
                if astate.error_count >= _MAX_CONSECUTIVE_ERRORS:
                    astate.disabled = True
                    logger.warning("alpha_disabled", alpha_id=alpha_id, errors=astate.error_count)
                astate.signal = math.nan
            else:
                astate.error_count = 0
                astate.signal = signal
                astate.update_z(signal)
                astate.signal_sparkline_append(signal)

        # Compute composite
        self._update_composite(sym_state)

    def _update_composite(self, sym_state: SymbolState) -> None:
        """Compute weighted z-score composite from pre-filtered weighted alphas only."""
        w_sum = 0.0
        wz_sum = 0.0
        alpha_states = sym_state.alpha_states
        weights = self._weights

        weighted_ids = self._weighted_ids or tuple(alpha_id for alpha_id, w in weights.items() if w > 0)

        for alpha_id in weighted_ids:
            astate = alpha_states.get(alpha_id)
            if astate is None or astate.disabled or math.isnan(astate.signal):
                continue
            w = weights[alpha_id]
            w_sum += w
            wz_sum += w * astate.z_score

        if w_sum > 0:
            sym_state.composite = wz_sum / w_sum
        else:
            sym_state.composite = 0.0

        sym_state.sparkline_append(sym_state.composite)


def _probe_dispatch_keys(runtime: Any) -> tuple[str, ...] | None:
    """Probe alpha.update() signature once. Return filtered keys or None for full payload."""
    try:
        sig = inspect.signature(runtime.update)
        params = sig.parameters
        # If **kwargs present, alpha accepts full payload
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return None
        # Extract positional/keyword param names
        keys = tuple(
            name
            for name, p in params.items()
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        )
        return keys if keys else None
    except (ValueError, TypeError):
        return None


def _call_alpha(astate: AlphaState, payload: dict[str, Any]) -> float | None:
    """Call alpha.update() using pre-probed dispatch keys. No exception-based fallback."""
    try:
        if astate.runtime is None:
            return None
        keys = astate._dispatch_keys
        if keys is None:
            result = astate.runtime.update(**payload)
        else:
            buf = astate._filtered_buf
            for k in keys:
                if k in payload:
                    buf[k] = payload[k]
            result = astate.runtime.update(**buf)
        if result is not None and math.isfinite(result):
            return float(result)
        return float(result) if result is not None else None
    except Exception as exc:
        logger.debug("operation_fallback", error=str(exc))
        return None


def _load_promotion_weights(promotions_dir: str | Path) -> dict[str, float]:
    """Scan promotion YAMLs for enabled alpha weights."""
    weights: dict[str, float] = {}
    pdir = Path(promotions_dir)
    if not pdir.exists():
        return weights

    for yaml_file in sorted(pdir.glob("**/*.yaml")):
        try:
            with open(yaml_file) as f:
                cfg = yaml.safe_load(f)
            if not isinstance(cfg, dict):
                continue
            if cfg.get("enabled") and "alpha_id" in cfg and "weight" in cfg:
                weights[cfg["alpha_id"]] = float(cfg["weight"])
        except Exception as exc:
            logger.debug("promotion_yaml_error", path=str(yaml_file), error=str(exc))

    return weights
