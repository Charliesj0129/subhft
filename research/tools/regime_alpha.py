"""WU9: Regime-Conditional Alpha Wrapper.

Wraps a base AlphaProtocol and applies regime-specific parameter overrides
based on a built-in volatility/trend regime detector.

Regimes (matching synth_lob_gen.py convention):
  - "volatile"       : vol_ema > 1.5 * vol_baseline
  - "trending"       : abs(trend_ema) > trend_threshold and not volatile
  - "mean_reverting" : otherwise
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import structlog

from research.registry.schemas import AlphaManifest, AlphaProtocol

logger = structlog.get_logger(__name__)

# EMA decay constants
_VOL_WINDOW: int = 64   # ticks — volatility EMA window
_TREND_WINDOW: int = 32  # ticks — trend EMA window
_ALPHA_VOL: float = 1.0 - math.exp(-1.0 / _VOL_WINDOW)
_ALPHA_TREND: float = 1.0 - math.exp(-1.0 / _TREND_WINDOW)

# Regime thresholds
_VOL_REGIME_MULTIPLIER: float = 1.5  # vol_ema > factor * vol_baseline → volatile
_TREND_THRESHOLD: float = 0.3        # abs(trend_ema) > threshold → trending
# vol_baseline is seeded from the first observed vol_ema value; a warmup
# period of ~3*_VOL_WINDOW ticks is needed before classification stabilises.
_WARMUP_TICKS: int = _VOL_WINDOW * 3


class RegimeConditionalAlpha:
    """Wraps a base alpha and applies regime-specific parameter overrides.

    Parameters
    ----------
    base_alpha:
        Any object implementing AlphaProtocol.
    regime_configs:
        Mapping of regime name → {attr_name: value} overrides.
        Example::

            {
                "trending":      {"signal_scale": 1.5},
                "volatile":      {"signal_scale": 0.3},
                "mean_reverting": {"signal_scale": 1.0},
            }

        Attributes not present on base_alpha are skipped with a warning.
    vol_regime_multiplier:
        Threshold multiplier for volatile regime detection (default 1.5).
    trend_threshold:
        Absolute trend EMA threshold for trending detection (default 0.3).
    """

    __slots__ = (
        "_base",
        "_regime_configs",
        "_vol_regime_multiplier",
        "_trend_threshold",
        # EMA state
        "_vol_ema",
        "_trend_ema",
        "_vol_baseline",
        "_tick_count",
        "_current_regime",
        "_last_mid_price",
        # cached manifest proxy
        "_log",
    )

    def __init__(
        self,
        base_alpha: AlphaProtocol,
        regime_configs: dict[str, dict[str, Any]],
        *,
        vol_regime_multiplier: float = _VOL_REGIME_MULTIPLIER,
        trend_threshold: float = _TREND_THRESHOLD,
    ) -> None:
        self._base = base_alpha
        self._regime_configs: dict[str, dict[str, Any]] = dict(regime_configs)
        self._vol_regime_multiplier = vol_regime_multiplier
        self._trend_threshold = trend_threshold

        # EMA accumulators (float, updated each tick — not hot-path allocated)
        self._vol_ema: float = 0.0
        self._trend_ema: float = 0.0
        self._vol_baseline: float | None = None
        self._tick_count: int = 0
        self._current_regime: str = "mean_reverting"
        self._last_mid_price: float | None = None

        self._log = logger.bind(alpha_id=self._base.manifest.alpha_id)
        self._validate_regime_configs()

    @property
    def manifest(self) -> AlphaManifest:
        return self._base.manifest

    def update(self, *args: Any, **kwargs: Any) -> float:
        """Detect regime, apply config overrides, delegate to base alpha."""
        mid_price = self._extract_mid_price(*args, **kwargs)
        if mid_price is not None:
            self._update_regime_state(mid_price)
            self._apply_regime_config(self._current_regime)

        return self._base.update(*args, **kwargs)

    def reset(self) -> None:
        """Reset base alpha and all internal detector state."""
        self._base.reset()
        self._vol_ema = 0.0
        self._trend_ema = 0.0
        self._vol_baseline = None
        self._tick_count = 0
        self._current_regime = "mean_reverting"
        self._last_mid_price = None

    def get_signal(self) -> float:
        return self._base.get_signal()

    def _extract_mid_price(self, *args: Any, **kwargs: Any) -> float | None:
        """Try to read mid_price from the event argument (dict or object)."""
        event = args[0] if args else kwargs.get("event")
        if event is None:
            return None
        if isinstance(event, dict):
            raw = event.get("mid_price") or event.get("mid_px")
            return float(raw) if raw is not None else None
        if isinstance(event, np.void):
            for field in ("mid_price", "mid_px"):
                if field in event.dtype.names:
                    return float(event[field])
        for attr in ("mid_price", "mid_px"):
            val = getattr(event, attr, None)
            if val is not None:
                return float(val)
        return None

    def _update_regime_state(self, mid_price: float) -> None:
        """Update EMA accumulators and classify regime."""
        self._tick_count += 1

        if self._last_mid_price is None:
            self._last_mid_price = mid_price
            return

        ret: float = mid_price - self._last_mid_price
        self._last_mid_price = mid_price
        abs_ret = abs(ret)

        self._vol_ema += _ALPHA_VOL * (abs_ret - self._vol_ema)
        self._trend_ema += _ALPHA_TREND * (ret - self._trend_ema)

        if self._vol_baseline is None and self._tick_count >= _WARMUP_TICKS:
            self._vol_baseline = self._vol_ema if self._vol_ema > 0 else 1e-9

        if self._vol_baseline is None:
            return

        if self._vol_ema > self._vol_regime_multiplier * self._vol_baseline:
            new_regime = "volatile"
        elif abs(self._trend_ema) > self._trend_threshold:
            new_regime = "trending"
        else:
            new_regime = "mean_reverting"

        if new_regime != self._current_regime:
            self._log.debug(
                "regime_switch",
                from_regime=self._current_regime,
                to_regime=new_regime,
                vol_ema=round(self._vol_ema, 6),
                trend_ema=round(self._trend_ema, 6),
                vol_baseline=round(self._vol_baseline, 6),
            )
            self._current_regime = new_regime

    def _apply_regime_config(self, regime: str) -> None:
        """Set attributes on base alpha for the given regime."""
        overrides = self._regime_configs.get(regime)
        if not overrides:
            return
        for attr, value in overrides.items():
            if not hasattr(self._base, attr):
                self._log.warning(
                    "regime_config_attr_missing",
                    regime=regime,
                    attr=attr,
                    alpha_id=self._base.manifest.alpha_id,
                )
                continue
            setattr(self._base, attr, value)

    def _validate_regime_configs(self) -> None:
        valid_regimes = {"trending", "mean_reverting", "volatile"}
        unknown = set(self._regime_configs) - valid_regimes
        if unknown:
            self._log.warning(
                "unknown_regimes_in_config",
                unknown=sorted(unknown),
                valid=sorted(valid_regimes),
            )

    @property
    def current_regime(self) -> str:
        """Current detected regime (read-only)."""
        return self._current_regime

    @property
    def tick_count(self) -> int:
        """Number of ticks processed since last reset."""
        return self._tick_count

    def __repr__(self) -> str:
        return (
            f"RegimeConditionalAlpha("
            f"base={self._base.manifest.alpha_id!r}, "
            f"regime={self._current_regime!r}, "
            f"ticks={self._tick_count})"
        )
