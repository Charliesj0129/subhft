"""feature_precompute.py — Pre-compute alpha signals on research .npy data.

Enables MM strategies to look up features by timestamp during hftbacktest
simulation without per-tick alpha computation overhead. Two paths: generic
(alpha.update() per tick) and vectorized (scipy lfilter EMA, O(n)).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
import structlog
from scipy.signal import lfilter

if TYPE_CHECKING:
    from numpy.typing import NDArray

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Required fields in .npy structured arrays
# ---------------------------------------------------------------------------
REQUIRED_FIELDS: frozenset[str] = frozenset(
    ["bid_px", "ask_px", "bid_qty", "ask_qty", "mid_price", "spread_bps", "volume", "local_ts"]
)

KNOWN_ALPHA_IDS: frozenset[str] = frozenset(
    [
        "toxicity_timescale_div",
        "microprice_spread_ratio",
        "ofi_asymmetry",
    ]
)
# EMA decay constants (matching production alpha implementations)
_EMA_ALPHA_4: float = 0.2212
_EMA_ALPHA_8: float = 0.1175  # ~= 2/(8+1) ≈ 0.222, tuned to match prod
_EMA_ALPHA_16: float = 0.0606
_EMA_ALPHA_32: float = 0.0308
_EMA_ALPHA_64: float = 0.0154


# ---------------------------------------------------------------------------
# Protocol for generic alpha path
# ---------------------------------------------------------------------------
class AlphaProtocol(Protocol):
    """Minimal alpha interface for generic precompute."""

    def update(self, *args: Any, **kwargs: Any) -> float: ...

    def reset(self) -> None: ...

    @property
    def manifest(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class AlphaConfig:
    """Configuration for a single alpha in precompute."""

    alpha_id: str
    alpha_instance: AlphaProtocol | None = field(default=None)


# ---------------------------------------------------------------------------
# Vectorized EMA helper
# ---------------------------------------------------------------------------
def _ema_vectorized(x: NDArray[np.float64], alpha: float) -> NDArray[np.float64]:
    """Compute EMA using scipy.signal.lfilter — O(n), no Python loop.

    EMA: y[n] = alpha * x[n] + (1 - alpha) * y[n-1]
    Implemented as a first-order IIR filter.
    """
    if len(x) == 0:
        return np.empty(0, dtype=np.float64)
    b = np.array([alpha], dtype=np.float64)
    a = np.array([1.0, -(1.0 - alpha)], dtype=np.float64)
    zi = np.array([x[0] * (1.0 - alpha)], dtype=np.float64)
    out, _ = lfilter(b, a, x, zi=zi)
    return np.asarray(out, dtype=np.float64)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def _validate_data(data: np.ndarray) -> None:
    """Validate that data has required fields."""
    if not isinstance(data, np.ndarray):
        msg = f"Expected np.ndarray, got {type(data).__name__}"
        raise TypeError(msg)
    if data.dtype.names is None:
        msg = "Expected structured array with named fields"
        raise ValueError(msg)
    missing = REQUIRED_FIELDS - set(data.dtype.names)
    if missing:
        msg = f"Missing required fields: {sorted(missing)}"
        raise ValueError(msg)
    if len(data) == 0:
        msg = "Data array is empty"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Generic precompute (any AlphaProtocol)
# ---------------------------------------------------------------------------
def precompute_alpha_features(
    data_path: str | Path,
    alpha_configs: list[AlphaConfig],
) -> tuple[NDArray[np.int64], NDArray[np.float64], list[str]]:
    """Pre-compute alpha signals by iterating ticks and calling alpha.update().

    Args:
        data_path: Path to .npy structured array.
        alpha_configs: List of AlphaConfig with alpha_instance set.

    Returns:
        (timestamps_ns, features, feature_names) where features has shape
        (n_ticks, n_alphas).
    """
    data_path = Path(data_path)
    if not data_path.exists():
        msg = f"Data file not found: {data_path}"
        raise FileNotFoundError(msg)

    data = np.load(str(data_path), allow_pickle=False)
    _validate_data(data)

    n = len(data)
    n_alphas = len(alpha_configs)
    feature_names = [cfg.alpha_id for cfg in alpha_configs]

    log.info(
        "precompute_alpha_features.start",
        n_ticks=n,
        n_alphas=n_alphas,
        alpha_ids=feature_names,
    )

    # Pre-allocate output arrays
    timestamps_ns = np.empty(n, dtype=np.int64)
    features = np.empty((n, n_alphas), dtype=np.float64)

    # Reset all alphas before run
    for cfg in alpha_configs:
        if cfg.alpha_instance is None:
            msg = f"alpha_instance is None for {cfg.alpha_id}"
            raise ValueError(msg)
        cfg.alpha_instance.reset()

    # Iterate ticks
    for i in range(n):
        row = data[i]
        timestamps_ns[i] = int(row["local_ts"])
        tick_fields = {name: float(row[name]) for name in REQUIRED_FIELDS if name != "local_ts"}
        tick_fields["local_ts"] = int(row["local_ts"])

        for j, cfg in enumerate(alpha_configs):
            assert cfg.alpha_instance is not None  # validated above  # noqa: S101
            features[i, j] = cfg.alpha_instance.update(**tick_fields)

    log.info("precompute_alpha_features.done", n_ticks=n, n_alphas=n_alphas)
    return timestamps_ns, features, feature_names


# ---------------------------------------------------------------------------
# Vectorized precompute (known alphas only)
# ---------------------------------------------------------------------------
def precompute_alpha_features_vectorized(
    data: np.ndarray,
    alpha_id: str,
) -> NDArray[np.float64]:
    """Vectorized O(n) precompute for known alphas.

    Args:
        data: Structured numpy array with required fields.
        alpha_id: One of 'queue_imbalance', 'microprice_momentum', 'ofi_regime'.

    Returns:
        Signal array of shape (n,).
    """
    _validate_data(data)

    if alpha_id not in KNOWN_ALPHA_IDS:
        msg = f"Unknown alpha_id for vectorized path: {alpha_id!r}. Known: {sorted(KNOWN_ALPHA_IDS)}"
        raise ValueError(msg)

    bid_qty = np.asarray(data["bid_qty"], dtype=np.float64)
    ask_qty = np.asarray(data["ask_qty"], dtype=np.float64)

    if alpha_id == "toxicity_timescale_div":
        spread = np.asarray(data["spread_bps"], dtype=np.float64)
        return _vectorized_toxicity_timescale_div(bid_qty, ask_qty, spread)
    if alpha_id == "microprice_spread_ratio":
        bid_px = np.asarray(data["bid_px"], dtype=np.float64)
        ask_px = np.asarray(data["ask_px"], dtype=np.float64)
        return _vectorized_microprice_spread_ratio(bid_px, ask_px, bid_qty, ask_qty)
    if alpha_id == "ofi_asymmetry":
        return _vectorized_ofi_asymmetry(bid_qty, ask_qty)
    msg = f"Vectorized path not implemented for {alpha_id!r}"
    raise NotImplementedError(msg)


# ---------------------------------------------------------------------------
# Vectorized: toxicity_timescale_div (fast/slow QI gated by spread excess)
# ---------------------------------------------------------------------------
def _vectorized_toxicity_timescale_div(
    bid_qty: NDArray[np.float64],
    ask_qty: NDArray[np.float64],
    spread: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Fast/slow QI divergence gated by spread excess. IC_IR=1.90, 26/26."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + 1e-8)
    fast = _ema_vectorized(qi, _EMA_ALPHA_4)
    slow = _ema_vectorized(qi, _EMA_ALPHA_32)
    divergence = fast - slow
    spread_ratio = spread / np.maximum(_ema_vectorized(spread, _EMA_ALPHA_64), 1.0)
    gate = np.clip((spread_ratio - 1.0), 0.0, None) + 0.1
    gate = np.minimum(gate, 1.0)
    return np.clip(divergence * gate, -1.0, 1.0)


# ---------------------------------------------------------------------------
# Vectorized: microprice_spread_ratio
# ---------------------------------------------------------------------------
def _vectorized_microprice_spread_ratio(
    bid_px: NDArray[np.float64],
    ask_px: NDArray[np.float64],
    bid_qty: NDArray[np.float64],
    ask_qty: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Microprice adjustment as fraction of spread. IC=+0.109, 26/26."""
    total = bid_qty + ask_qty
    safe_total = np.where(total > 0, total, 1.0)
    microprice = (bid_px * ask_qty + ask_px * bid_qty) / safe_total
    mid = (bid_px + ask_px) / 2.0
    raw_spread = np.maximum(ask_px - bid_px, 1e-8)
    return np.clip((microprice - mid) / raw_spread, -1.0, 1.0)


# ---------------------------------------------------------------------------
# Vectorized: ofi_asymmetry
# ---------------------------------------------------------------------------
def _vectorized_ofi_asymmetry(
    bid_qty: NDArray[np.float64],
    ask_qty: NDArray[np.float64],
) -> NDArray[np.float64]:
    """2nd-moment OFI decomposition. IC=+0.093, IR=1.72, 92/94 symbols."""
    ofi = np.diff(bid_qty, prepend=bid_qty[0]) - np.diff(ask_qty, prepend=ask_qty[0])
    pos_sq = np.maximum(ofi, 0.0) ** 2
    neg_sq = np.maximum(-ofi, 0.0) ** 2
    ema_pos = _ema_vectorized(pos_sq, _EMA_ALPHA_16)
    ema_neg = _ema_vectorized(neg_sq, _EMA_ALPHA_16)
    denom = ema_pos + ema_neg + 1e-8
    return np.clip((ema_pos - ema_neg) / denom, -1.0, 1.0)


# ---------------------------------------------------------------------------
# Multi-alpha precompute (batch all features at once)
# ---------------------------------------------------------------------------
def precompute_all_mm_features(
    data: np.ndarray,
    alpha_ids: list[str] | None = None,
) -> tuple[NDArray[np.int64], NDArray[np.float64], list[str]]:
    """Precompute multiple alpha features in a single pass.

    Returns (timestamps, features_2d, feature_names) ready for
    save_precomputed_features() or direct use with AlphaDrivenMMStrategy.
    """
    _validate_data(data)

    if alpha_ids is None:
        alpha_ids = [
            "toxicity_timescale_div",
            "microprice_spread_ratio",
            "ofi_asymmetry",
        ]

    timestamps = np.asarray(data["local_ts"], dtype=np.int64)
    n = len(data)
    features = np.empty((n, len(alpha_ids)), dtype=np.float64)

    for i, alpha_id in enumerate(alpha_ids):
        features[:, i] = precompute_alpha_features_vectorized(data, alpha_id)

    log.info(
        "precompute_all_mm_features.done",
        n_ticks=n,
        n_features=len(alpha_ids),
        alpha_ids=alpha_ids,
    )
    return timestamps, features, alpha_ids


# ---------------------------------------------------------------------------
# Persistence (save / load)
# ---------------------------------------------------------------------------
def save_precomputed_features(
    path: str | Path,
    timestamps: NDArray[np.int64],
    features: NDArray[np.float64],
    feature_names: list[str],
    metadata: dict[str, Any] | None = None,
) -> Path:
    """Save precomputed features to .npz file.

    Returns the resolved path written to.
    """
    path = Path(path)
    save_dict: dict[str, Any] = {
        "timestamps": timestamps,
        "features": features,
        "feature_names": np.array(feature_names, dtype="U64"),
    }
    if metadata is not None:
        import json

        save_dict["metadata_json"] = np.array([json.dumps(metadata)], dtype="U4096")

    np.savez_compressed(str(path), **save_dict)
    log.info("save_precomputed_features", path=str(path), n_ticks=len(timestamps))
    return path


def load_precomputed_features(
    path: str | Path,
) -> tuple[NDArray[np.int64], NDArray[np.float64], list[str]]:
    """Load precomputed features from .npz file.

    Returns:
        (timestamps, features, feature_names).
    """
    path = Path(path)
    if not path.exists():
        msg = f"Precomputed features file not found: {path}"
        raise FileNotFoundError(msg)

    npz = np.load(str(path), allow_pickle=False)
    timestamps = np.asarray(npz["timestamps"], dtype=np.int64)
    features = np.asarray(npz["features"], dtype=np.float64)
    feature_names = [str(s) for s in npz["feature_names"]]

    log.info(
        "load_precomputed_features",
        path=str(path),
        n_ticks=len(timestamps),
        n_features=len(feature_names),
    )
    return timestamps, features, feature_names
