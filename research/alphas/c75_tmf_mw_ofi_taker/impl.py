"""c75_tmf_mw_ofi_taker -- Multi-Window OFI Taker (FE-v3, zero free parameters).

Live signal (2-term, post-D2 form per
``docs/runbooks/c75-depth-parity-decision-2026-05-06.md``):

    flow_signal = 0.667 * ofi_l1_ema5s
                + 0.333 * ofi_l1_ema30s

Threshold-crossing taker: fires IOC market-aggressing orders when
``abs(flow_signal) > 1.5 * rolling_stdev(flow_signal, last 300 ticks)`` AND
``spread_ema30s <= 1.5 * spread_ema300s`` (i.e. the spread is not in an
anomalous-widen regime). Otherwise quiet.

Why 2-term and not 3? The original draft included a third term
``0.1 * deep_depth_momentum_x1000`` (FE-v3 idx 20, MLDM). The HFT-platform
backtest adapter currently emits only L1 quotes when constructing
``BidAskEvent`` (see ``src/hft_platform/backtest/adapter.py:436``), so MLDM
collapses to zero in Gate C and the third term silently dropped. Per the
2026-05-06 depth-parity decision, the alpha was rebalanced to two terms
(weights renormalised 0.6:0.3 -> 0.667:0.333) so the manifest tells the
truth about what's tested. See the cited runbook for D1 follow-up.

Frozen weights are taken from Cont-Kukanov OFI 2014 (multi-window flow
lineage) without per-day calibration: this is a zero-free-parameter alpha.

FE-v3 indices consumed:
    22  ofi_l1_ema5s
    23  ofi_l1_ema30s
    25  spread_ema30s    (regime gate)
    26  spread_ema300s   (regime gate baseline)

State machine:
    1. Warm-up: until ``_update_count >= WARMUP_TICKS`` (300), should_fire
       always returns 0. Signal is recorded but the gate suppresses it.
    2. Spread-regime gate: if ``spread_ema30s > 1.5 * spread_ema300s``,
       should_fire returns 0 regardless of signal magnitude.
    3. Threshold gate: signal crosses +/- 1.5-sigma (rolling stdev over
       last 300 ticks). Returns +1 (BUY) or -1 (SELL); else 0.

This file conforms to ``research.registry.schemas.AlphaProtocol``
(manifest property + reset/update methods). The discrete fire decision
is exposed via ``should_fire()``; ``research/backtest/alpha_strategy_bridge.py``
calls it after ``update()`` to gate the recorded signal -- see Codex
adversarial-review 2026-05-06 finding 4.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from research.registry.schemas import (
    AlphaManifest,
    AlphaStatus,
    AlphaTier,
)

# FE-v3 indices (mirror of src/hft_platform/feature/registry.py
# build_default_lob_feature_set_v3 layout).
IDX_OFI_L1_EMA5S: int = 22
IDX_OFI_L1_EMA30S: int = 23
IDX_SPREAD_EMA30S: int = 25
IDX_SPREAD_EMA300S: int = 26

# Frozen weights (post-D2 rebalanced from 0.6/0.3 to 0.667/0.333 to
# preserve the original 6:3 ratio after dropping the deep-momentum term).
W_OFI_5S: float = 0.667
W_OFI_30S: float = 0.333

# Fire-gate constants.
FIRE_THRESHOLD_STDEV_MULTIPLIER: float = 1.5
FIRE_THRESHOLD_WINDOW_TICKS: int = 300
SPREAD_REGIME_MULTIPLIER: float = 1.5
WARMUP_TICKS: int = 300


def _coerce_features(payload: Mapping[str, Any] | None) -> tuple[float, ...] | None:
    """Pull a feature tuple out of an alpha.update() payload.

    Accepts either a Mapping with key ``features`` or a positional Sequence /
    Iterable. Returns None if no usable tuple is found.
    """
    if isinstance(payload, Mapping):
        feats = payload.get("features")
        if isinstance(feats, (Sequence, Iterable)) and not isinstance(feats, (str, bytes)):
            return tuple(feats)
        return None
    if isinstance(payload, (Sequence, Iterable)) and not isinstance(payload, (str, bytes)):
        return tuple(payload)
    return None


def _read(features: tuple[float, ...] | None, idx: int, key: str, payload: Mapping[str, Any]) -> float:
    """Read a single FE-v3 feature index, falling back to a named kwarg.

    The bridge populates BOTH ``features=tuple(ft)`` and
    ``<canonical_name>=value`` (see _FE_KEYS_V3 in alpha_strategy_bridge);
    this helper accepts either path.
    """
    if features is not None and idx < len(features):
        try:
            return float(features[idx])
        except (TypeError, ValueError):
            pass
    value = payload.get(key, 0.0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class C75TmfMwOfiTakerAlpha:
    """C75 -- Multi-Window OFI Taker (TMFD6, FE-v3, 2-term composite).

    Conforms to ``research.registry.schemas.AlphaProtocol``.
    """

    __slots__ = (
        "_signal",
        "_signal_history",
        "_last_spread_ema30s",
        "_last_spread_ema300s",
        "_update_count",
    )

    def __init__(self) -> None:
        self._signal: float = 0.0
        self._signal_history: deque[float] = deque(maxlen=FIRE_THRESHOLD_WINDOW_TICKS)
        self._last_spread_ema30s: float = 0.0
        self._last_spread_ema300s: float = 0.0
        self._update_count: int = 0

    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="c75_tmf_mw_ofi_taker",
            hypothesis=(
                "Composite multi-window OFI signal (0.667 ema5s + 0.333 ema30s) "
                "predicts informed flow when spread is not in anomalous-widen "
                "regime. Fires IOC taker on the favorable side at 1.5-sigma "
                "threshold. Frozen weights from Cont-Kukanov 2014 (post-D2 "
                "rebalance from 0.6/0.3 after dropping deep-momentum term per "
                "docs/runbooks/c75-depth-parity-decision-2026-05-06.md)."
            ),
            formula="alpha_t = 0.667 * ofi_l1_ema5s_t + 0.333 * ofi_l1_ema30s_t",
            paper_refs=("Cont-Kukanov 2014 OFI",),
            data_fields=(),  # FE-v3 derived features; no raw data_fields required
            complexity="O(1)",
            status=AlphaStatus.GATE_B,
            tier=AlphaTier.TIER_2,
            latency_profile="r47_maker_shioaji_p95_v2026-04-24_measured",
            roles_used=("planner",),
            skills_used=("hft-backtester",),
            feature_set_version="lob_shared_v3",
            strategy_type="taker",
            instrument="TMFD6",
            dsl_formula="0.667 * ofi_l1_ema5s + 0.333 * ofi_l1_ema30s",
            parent_alpha_id=None,
        )

    def update(self, *args: Any, **kwargs: Any) -> float:
        # Bridge passes payload as kwargs; pick out the feature tuple either
        # from kwargs.features or the first positional arg.
        payload: Mapping[str, Any]
        if args and isinstance(args[0], Mapping):
            payload = args[0]
        else:
            payload = kwargs

        features = _coerce_features(payload)

        ofi_5s = _read(features, IDX_OFI_L1_EMA5S, "ofi_l1_ema5s", payload)
        ofi_30s = _read(features, IDX_OFI_L1_EMA30S, "ofi_l1_ema30s", payload)
        self._last_spread_ema30s = _read(features, IDX_SPREAD_EMA30S, "spread_ema30s", payload)
        self._last_spread_ema300s = _read(features, IDX_SPREAD_EMA300S, "spread_ema300s", payload)

        self._signal = W_OFI_5S * ofi_5s + W_OFI_30S * ofi_30s
        self._signal_history.append(self._signal)
        self._update_count += 1
        return self._signal

    def reset(self) -> None:
        self._signal = 0.0
        self._signal_history.clear()
        self._last_spread_ema30s = 0.0
        self._last_spread_ema300s = 0.0
        self._update_count = 0

    def get_signal(self) -> float:
        return self._signal

    def is_warm(self) -> bool:
        return self._update_count >= WARMUP_TICKS

    def spread_regime_ok(self) -> bool:
        # If we have no baseline yet, treat as anomalous (gate closed).
        if self._last_spread_ema300s <= 0.0:
            return False
        return self._last_spread_ema30s <= SPREAD_REGIME_MULTIPLIER * self._last_spread_ema300s

    def fire_threshold(self) -> float:
        """Return the 1.5-sigma fire threshold over the last 300 ticks."""
        n = len(self._signal_history)
        if n < FIRE_THRESHOLD_WINDOW_TICKS:
            return float("inf")
        # Population stdev (no Bessel correction; consistent with frozen
        # window length).
        mean = sum(self._signal_history) / n
        var = sum((x - mean) ** 2 for x in self._signal_history) / n
        stdev = var ** 0.5
        return FIRE_THRESHOLD_STDEV_MULTIPLIER * stdev

    def should_fire(self) -> int:
        """Return +1 (BUY), -1 (SELL), or 0 (no fire)."""
        if not self.is_warm():
            return 0
        if not self.spread_regime_ok():
            return 0
        threshold = self.fire_threshold()
        if abs(self._signal) <= threshold:
            return 0
        return 1 if self._signal > 0 else -1


ALPHA_CLASS = C75TmfMwOfiTakerAlpha
