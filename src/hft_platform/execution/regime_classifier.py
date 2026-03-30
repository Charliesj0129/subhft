"""Regime classifier for execution timing (Direction C, R24).

Classifies the current LOB microstate into FAVORABLE / NEUTRAL / ADVERSE
regimes based on FeatureEngine v2+ features:
  - tob_survival_ms [18]: TOB stability (strongest predictor, rho=-0.21)
  - ret_autocov_5s_x1e6 [17]: return autocovariance (rho=-0.12)
  - spread_ema300s [26]: long-term spread regime (v3 only)
  - toxicity_ema50_x1000 [21]: informed flow intensity (R23 validated)

Decision logic:
  ADVERSE  = burst active OR toxicity > high_threshold OR tob_survival < short_threshold
  FAVORABLE = tob_survival > long_threshold AND |ret_autocov| < calm_threshold
  NEUTRAL  = everything else

Empirical basis:
  - Diagnostic 0a (R24): tob_survival and ret_autocov correlate with 30s forward
    price movement magnitude at rho=-0.21 and rho=-0.12 respectively.
  - R23: toxicity Q5-Q1 = +3.5 pts adverse movement on TXFD6.
  - R14/R16: regime bifurcation is the primary execution bottleneck.

Allocator Law: __slots__, no heap allocations in classify().
Precision Law: all thresholds are int (matching FeatureEngine output scale).
"""

from __future__ import annotations

from enum import IntEnum

import structlog

logger = structlog.get_logger(__name__)


class Regime(IntEnum):
    """Execution regime classification."""

    FAVORABLE = 1
    NEUTRAL = 0
    ADVERSE = -1


class RegimeClassifier:
    """Classify LOB microstate into execution regimes.

    Consumes a feature tuple from ``FeatureEngine.get_feature_tuple()``
    and an optional burst flag from ``BurstDetector``.

    Parameters
    ----------
    tob_survival_adverse_ms : int
        TOB survival below this threshold → ADVERSE (fast price changes).
        Default: 50ms (from Diagnostic 0a: low survival = high forward vol).
    tob_survival_favorable_ms : int
        TOB survival above this threshold → candidate for FAVORABLE.
        Default: 500ms (stable TOB = calm market).
    ret_autocov_calm_threshold : int
        |ret_autocov_5s_x1e6| below this → calm (no strong autocorrelation).
        Default: 500 (low autocovariance = no directional pressure).
    toxicity_adverse_threshold : int
        toxicity_ema50_x1000 above this → ADVERSE (strong informed flow).
        Default: 400 (= 0.4 toxicity ratio, R23 Q4-Q5 boundary).
    spread_wide_threshold : int
        spread_ema300s above this (scaled x10000) → ADVERSE context.
        If 0, this check is disabled. Default: 0 (disabled until calibrated).
    enabled : bool
        If False, always returns NEUTRAL. Default: True.

    Feature Indices (FeatureEngine v2+)
    ------------------------------------
    These are positional indices into the feature tuple returned by
    ``FeatureEngine.get_feature_tuple()``. They correspond to the
    ``lob_shared_v2`` / ``lob_shared_v3`` registry definitions.

    Index 17: ret_autocov_5s_x1e6
    Index 18: tob_survival_ms
    Index 21: toxicity_ema50_x1000
    Index 26: spread_ema300s (v3 only, may not exist in v2 tuple)
    """

    __slots__ = (
        "_tob_adverse_ms",
        "_tob_favorable_ms",
        "_autocov_calm",
        "_tox_adverse",
        "_spread_wide",
        "_enabled",
        "_last_regime",
        "_transition_count",
        "_holdoff_ns",
        "_last_transition_ts_ns",
    )

    # Feature tuple indices (from FeatureEngine registry)
    _IDX_RET_AUTOCOV: int = 17
    _IDX_TOB_SURVIVAL: int = 18
    _IDX_TOXICITY: int = 21
    _IDX_SPREAD_EMA300S: int = 26

    def __init__(
        self,
        tob_survival_adverse_ms: int = 50,
        tob_survival_favorable_ms: int = 500,
        ret_autocov_calm_threshold: int = 500,
        toxicity_adverse_threshold: int = 400,
        spread_wide_threshold: int = 0,
        holdoff_ns: int = 5_000_000_000,
        enabled: bool = True,
    ) -> None:
        self._tob_adverse_ms: int = tob_survival_adverse_ms
        self._tob_favorable_ms: int = tob_survival_favorable_ms
        self._autocov_calm: int = ret_autocov_calm_threshold
        self._tox_adverse: int = toxicity_adverse_threshold
        self._spread_wide: int = spread_wide_threshold
        self._holdoff_ns: int = holdoff_ns
        self._enabled: bool = enabled
        self._last_regime: Regime = Regime.NEUTRAL
        self._transition_count: int = 0
        self._last_transition_ts_ns: int = 0

    def classify(
        self,
        feature_tuple: tuple[int | float, ...] | None,
        burst_active: bool = False,
        ts_ns: int = 0,
    ) -> Regime:
        """Classify current microstate into execution regime.

        Parameters
        ----------
        feature_tuple
            Feature values from ``FeatureEngine.get_feature_tuple(symbol)``.
            None if FeatureEngine has no state for this symbol (returns NEUTRAL).
        burst_active
            True if BurstDetector signals a tick intensity burst.
        ts_ns
            Current timestamp in nanoseconds. Used for holdoff debouncing.
            If 0, holdoff is not applied.

        Returns
        -------
        Regime
            FAVORABLE, NEUTRAL, or ADVERSE.
        """
        if not self._enabled:
            return Regime.NEUTRAL

        # No features available → conservative NEUTRAL
        if feature_tuple is None:
            return Regime.NEUTRAL

        candidate = self._compute_raw_regime(feature_tuple, burst_active)

        # Holdoff: suppress transitions within holdoff window
        if candidate != self._last_regime and ts_ns > 0 and self._holdoff_ns > 0:
            elapsed = ts_ns - self._last_transition_ts_ns
            if elapsed < self._holdoff_ns:
                return self._last_regime  # suppress transition

        return self._set_regime(candidate, ts_ns)

    def _compute_raw_regime(
        self,
        feature_tuple: tuple[int | float, ...],
        burst_active: bool,
    ) -> Regime:
        """Compute regime without holdoff logic."""
        if burst_active:
            return Regime.ADVERSE

        if self._check_adverse(feature_tuple):
            return Regime.ADVERSE

        if self._check_favorable(feature_tuple):
            return Regime.FAVORABLE

        return Regime.NEUTRAL

    def _check_adverse(self, feature_tuple: tuple[int | float, ...]) -> bool:  # noqa: C901
        """Return True if any ADVERSE condition is met."""
        n = len(feature_tuple)

        # High toxicity → informed flow dominance
        if n > self._IDX_TOXICITY and self._tox_adverse > 0:
            tox = feature_tuple[self._IDX_TOXICITY]
            if isinstance(tox, (int, float)) and abs(int(tox)) > self._tox_adverse:
                return True

        # Very short TOB survival → fast price changes
        if n > self._IDX_TOB_SURVIVAL:
            tob = feature_tuple[self._IDX_TOB_SURVIVAL]
            if isinstance(tob, (int, float)) and int(tob) < self._tob_adverse_ms:
                return True

        # Wide spread regime (optional)
        if self._spread_wide > 0 and n > self._IDX_SPREAD_EMA300S:
            spread = feature_tuple[self._IDX_SPREAD_EMA300S]
            if isinstance(spread, (int, float)) and int(spread) > self._spread_wide:
                return True

        return False

    def _check_favorable(self, feature_tuple: tuple[int | float, ...]) -> bool:
        """Return True if all FAVORABLE conditions are met."""
        n = len(feature_tuple)

        # TOB survival must be high (stable book)
        if n <= self._IDX_TOB_SURVIVAL:
            return False
        tob = feature_tuple[self._IDX_TOB_SURVIVAL]
        if not isinstance(tob, (int, float)) or int(tob) < self._tob_favorable_ms:
            return False

        # Return autocovariance must be calm (no strong serial correlation)
        if n > self._IDX_RET_AUTOCOV:
            autocov = feature_tuple[self._IDX_RET_AUTOCOV]
            if not isinstance(autocov, (int, float)) or abs(int(autocov)) > self._autocov_calm:
                return False

        return True

    def _set_regime(self, regime: Regime, ts_ns: int = 0) -> Regime:
        """Track regime transitions for monitoring."""
        if regime != self._last_regime:
            self._transition_count += 1
            self._last_regime = regime
            if ts_ns > 0:
                self._last_transition_ts_ns = ts_ns
        return regime

    # --- Properties for observability ---

    @property
    def last_regime(self) -> Regime:
        """Most recently classified regime."""
        return self._last_regime

    @property
    def transition_count(self) -> int:
        """Total number of regime transitions since creation."""
        return self._transition_count

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def reset(self) -> None:
        """Reset state (e.g. on symbol change or session boundary)."""
        self._last_regime = Regime.NEUTRAL
        self._transition_count = 0
        self._last_transition_ts_ns = 0

    def status(self) -> dict[str, object]:
        """Runtime status for observability."""
        return {
            "enabled": self._enabled,
            "last_regime": self._last_regime.name,
            "transition_count": self._transition_count,
            "thresholds": {
                "tob_survival_adverse_ms": self._tob_adverse_ms,
                "tob_survival_favorable_ms": self._tob_favorable_ms,
                "ret_autocov_calm_threshold": self._autocov_calm,
                "toxicity_adverse_threshold": self._tox_adverse,
                "spread_wide_threshold": self._spread_wide,
                "holdoff_ns": self._holdoff_ns,
            },
        }
