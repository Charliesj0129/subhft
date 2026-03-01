"""lob_shape alpha: LOB depth slope asymmetry + EMA OFI alignment.

Research implementation (Gates A–C). Mirrors the Rust AlphaDepthSlope
log-linear OLS formula for research/backtest parity.

Hot-path constraints:
- __slots__ on all stateful classes (Allocator Law)
- signal is float used for ranking only — never used as a price (Precision Law)
- no heap allocation per tick after construction (Allocator Law)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

try:
    from structlog import get_logger as _get_logger
    _log = _get_logger("lob_shape_alpha")
except ImportError:
    import logging as _logging
    _log = _logging.getLogger("lob_shape_alpha")

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# Feature tuple indices (lob_shared_v1 schema_version=1).
# Must stay in sync with src/hft_platform/feature/registry.py.
_IDX_OFI_L1_EMA8 = 13
_IDX_DEPTH_IMBALANCE_EMA8_PPM = 15

# Default number of LOB depth levels used for slope regression.
_LOB_DEPTH_LEVELS = 10

# Default lambda weight for the OFI sign-alignment term.
_LAMBDA_DEFAULT = 0.3


@dataclass(slots=True)
class LobShapeResult:
    """Decomposed signal components (for diagnostics / Gate C inspection)."""

    slope_bid: float
    slope_ask: float
    raw_slope_diff: float   # slope_ask - slope_bid
    sign_align_val: int     # {-1, 0, 1}
    signal: float           # final composite signal


def _compute_slope(levels: np.ndarray, n_levels: int) -> float:
    """Log-linear OLS slope: OLS(level_index, log(qty+1)).

    Mirrors Rust `compute_side_slope` in alpha.rs.

    Parameters
    ----------
    levels:
        (N, 2) int64 array where col 0 = price (scaled) and col 1 = qty.
    n_levels:
        Maximum number of rows to use from levels.

    Returns
    -------
    float
        OLS slope coefficient. 0.0 when fewer than 2 rows are available.
    """
    n = min(len(levels), n_levels)
    if n < 2:
        return 0.0
    qtys = levels[:n, 1].astype(np.float64, copy=False)
    x = np.arange(1, n + 1, dtype=np.float64)   # level indices 1..n
    y = np.log1p(qtys)                            # log(qty + 1)
    sx = x.sum()
    sy = y.sum()
    sxy = float((x * y).sum())
    sx2 = float((x * x).sum())
    denom = n * sx2 - sx * sx
    if denom == 0.0:
        return 0.0
    return float((n * sxy - sx * sy) / denom)


def _sign_align(a: int | float, b: int | float) -> int:
    """Return 1 if both nonzero with same sign, -1 if opposite, 0 if either zero."""
    sa = 1 if a > 0 else (-1 if a < 0 else 0)
    sb = 1 if b > 0 else (-1 if b < 0 else 0)
    if sa == 0 or sb == 0:
        return 0
    return 1 if sa == sb else -1


class LobShapeCore:
    """Pure-Python LOB shape slope kernel.

    Implements the same log-linear OLS computation as the Rust AlphaDepthSlope
    so that Gate B/C results are reproducible without the compiled extension.
    """

    __slots__ = ("_lambda", "_n_levels", "_signal", "_last_result")

    def __init__(self, lambda_: float = _LAMBDA_DEFAULT, n_levels: int = _LOB_DEPTH_LEVELS) -> None:
        self._lambda: float = float(lambda_)
        self._n_levels: int = int(n_levels)
        self._signal: float = 0.0
        self._last_result: LobShapeResult | None = None

    def reset(self) -> None:
        self._signal = 0.0
        self._last_result = None

    def compute(
        self,
        bids: np.ndarray,
        asks: np.ndarray,
        ofi_l1_ema8: int | float,
        depth_imbalance_ema8_ppm: int | float,
    ) -> LobShapeResult:
        """Compute the lob_shape signal from a single LOB snapshot.

        Parameters
        ----------
        bids:
            (N, 2) int64 array: col0 = bid price (scaled ×10000), col1 = bid qty.
            Expected sorted highest-price-first (level 1 = best bid).
        asks:
            (N, 2) int64 array: col0 = ask price (scaled ×10000), col1 = ask qty.
            Expected sorted lowest-price-first (level 1 = best ask).
        ofi_l1_ema8:
            EMA(8) of L1 order flow imbalance from feature tuple index 13.
        depth_imbalance_ema8_ppm:
            EMA(8) of depth imbalance in PPM from feature tuple index 15.
        """
        slope_bid = _compute_slope(bids, self._n_levels)
        slope_ask = _compute_slope(asks, self._n_levels)
        raw_diff = slope_ask - slope_bid
        sa = _sign_align(ofi_l1_ema8, depth_imbalance_ema8_ppm)
        sig = raw_diff + self._lambda * sa
        result = LobShapeResult(
            slope_bid=slope_bid,
            slope_ask=slope_ask,
            raw_slope_diff=raw_diff,
            sign_align_val=sa,
            signal=sig,
        )
        self._signal = sig
        self._last_result = result
        return result

    def get_signal(self) -> float:
        return self._signal


class LobShapeAlpha(LobShapeCore):
    """AlphaProtocol-conforming wrapper for the Gates A-E pipeline.

    Tier: TIER_1
    Rust module (live v2 target): alpha_depth_slope
    Complexity: O(N) where N = LOB depth levels (≤10)
    """

    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="lob_shape",
            hypothesis=(
                "LOB depth slope asymmetry (ask-side slope steeper than bid-side) combined "
                "with EMA OFI and depth imbalance directional alignment predicts short-term "
                "price pressure. A steeper ask-side depth slope signals supply building up "
                "deeper in the book; agreement with OFI/imbalance direction amplifies the signal."
            ),
            formula=(
                "slope_bid = OLS(level_idx, log(bid_qty+1)); "
                "slope_ask = OLS(level_idx, log(ask_qty+1)); "
                "sign_align = 1 if sign(ofi_l1_ema8)==sign(depth_imbalance_ema8_ppm) else {0,-1}; "
                "signal = (slope_ask - slope_bid) + λ × sign_align"
            ),
            paper_refs=("depth_slope_ref",),
            data_fields=("bids", "asks", "ofi_l1_ema8", "depth_imbalance_ema8_ppm"),
            complexity="O(N)",   # O(L) where L = LOB levels ≤ 10; Gate A accepts O(N)
            status=AlphaStatus.GATE_B,
            tier=AlphaTier.TIER_1,
            rust_module="alpha_depth_slope",
            # Latency realism governance (CLAUDE.md constitution requirement).
            # Profile: Shioaji sim API P95 RTT — submit≈36ms, modify≈43ms, cancel≈47ms.
            # Source: docs/architecture/latency-baseline-shioaji-sim-vs-system.md
            latency_profile="shioaji_sim_p95_v2026-02-27",
            # SOP governance: roles and skills applied during research.
            roles_used=("planner", "code-reviewer"),
            skills_used=("iterative-retrieval", "validation-gate", "hft-backtester"),
            feature_set_version="lob_shared_v1",
        )

    def update(self, *args: Any, **kwargs: Any) -> float:
        """AlphaProtocol.update — positional or keyword args.

        Positional: update(bids, asks, ofi_l1_ema8, depth_imbalance_ema8_ppm)
        Keyword:    update(bids=..., asks=..., ofi_l1_ema8=..., depth_imbalance_ema8_ppm=...)

        All four arguments are required. Providing only (bids, asks) raises ValueError.
        """
        if args:
            if len(args) < 4:
                raise ValueError(
                    f"update() requires 4 positional args "
                    f"(bids, asks, ofi_l1_ema8, depth_imbalance_ema8_ppm), "
                    f"got {len(args)}"
                )
            bids = np.asarray(args[0], dtype=np.int64)
            asks = np.asarray(args[1], dtype=np.int64)
            ofi_l1_ema8 = int(args[2])
            depth_imbalance_ema8_ppm = int(args[3])
        else:
            bids = np.asarray(kwargs["bids"], dtype=np.int64)
            asks = np.asarray(kwargs["asks"], dtype=np.int64)
            ofi_l1_ema8 = int(kwargs["ofi_l1_ema8"])
            depth_imbalance_ema8_ppm = int(kwargs["depth_imbalance_ema8_ppm"])
        self.compute(bids, asks, ofi_l1_ema8, depth_imbalance_ema8_ppm)
        return self.get_signal()

    def update_batch(self, data: Any) -> np.ndarray:
        """BatchAlphaProtocol.update_batch for Gate C backtest runner.

        Uses L1-proxy mode when only scalar depth columns are available:
        bid_depth / ask_depth are used as a single-level LOB (1 row).
        When n_levels=1, _compute_slope returns 0.0 (need ≥2 levels for OLS),
        so the signal degrades to λ × sign_align only.

        WARNING: slope term is ZERO in L1-proxy mode. Gate C backtest will
        underestimate the full alpha's predictive power. Provide multi-level
        LOB data (bids/asks columns with shape (N,2)) for complete validation.
        """
        arr = np.asarray(data)
        if arr.size == 0:
            return np.zeros(0, dtype=np.float64)
        if not arr.dtype.names:
            return np.zeros(arr.shape[0] if arr.ndim > 0 else 0, dtype=np.float64)

        names = set(arr.dtype.names)
        n = arr.shape[0]
        out = np.zeros(n, dtype=np.float64)

        ofi_key = "ofi_l1_ema8" if "ofi_l1_ema8" in names else ("ofi_l1_raw" if "ofi_l1_raw" in names else None)
        imb_key = "depth_imbalance_ema8_ppm" if "depth_imbalance_ema8_ppm" in names else (
            "depth_imbalance_ppm" if "depth_imbalance_ppm" in names else None
        )
        bid_depth_key = "bid_depth" if "bid_depth" in names else None
        ask_depth_key = "ask_depth" if "ask_depth" in names else None

        # Warn once per batch call when falling back to L1-proxy mode.
        _log.warning(
            "lob_shape update_batch: using L1-proxy (slope=0); "
            "provide multi-level LOB arrays for full signal validation",
            n_rows=n,
            has_bid_depth=bid_depth_key is not None,
            has_ofi=ofi_key is not None,
        )

        # Pre-allocate sentinel arrays once; mutate col1 in-place each row.
        # Fixes Allocator Law violation: no np.array() inside the loop.
        bids_l1 = np.zeros((1, 2), dtype=np.int64)
        asks_l1 = np.zeros((1, 2), dtype=np.int64)

        for i in range(n):
            bids_l1[0, 1] = int(arr[bid_depth_key][i]) if bid_depth_key else 0
            asks_l1[0, 1] = int(arr[ask_depth_key][i]) if ask_depth_key else 0
            ofi = int(arr[ofi_key][i]) if ofi_key else 0
            imb = int(arr[imb_key][i]) if imb_key else 0
            out[i] = self.update(bids_l1, asks_l1, ofi, imb)
        return out


ALPHA_CLASS = LobShapeAlpha

__all__ = [
    "LobShapeResult",
    "LobShapeCore",
    "LobShapeAlpha",
    "ALPHA_CLASS",
    "_compute_slope",
    "_sign_align",
]
