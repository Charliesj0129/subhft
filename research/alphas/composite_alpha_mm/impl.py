"""composite_alpha_mm — Research-side alpha for backtest parity.

Composite signal combining OFI EMA, depth imbalance EMA, and LOB slope asymmetry.
Mirrors the live CompositeAlphaMM strategy signal computation for research/backtest use.

Signal:
    composite = w_ofi * normalize(ofi_l1_ema8)
              + w_depth * normalize(depth_imbalance_ema8_ppm)
              + w_slope * (slope_ask - slope_bid)
    signal = (composite - ema(composite)) / std(composite)

Data fields (lob_shared_v1):
    - ofi_l1_ema8: Order flow imbalance L1 EMA(8)
    - depth_imbalance_ema8_ppm: Depth imbalance EMA(8) in ppm
    - l1_bid_qty, l1_ask_qty: Level 1 quantities (for slope proxy)
    - spread_scaled: Scaled spread
    - mid_price_x2: Mid price x 2

Complexity: O(1) per tick
Paper refs: (composite, no single paper)
"""

from __future__ import annotations

import numpy as np


def compute_alpha(
    data: np.ndarray,
    *,
    w_ofi: float = 0.4,
    w_depth: float = 0.3,
    w_slope: float = 0.3,
    ema_alpha: float = 0.01,
) -> np.ndarray:
    """Compute composite alpha signal from structured LOB data.

    Parameters
    ----------
    data : np.ndarray
        Structured array with fields: ofi_l1_ema8, depth_imbalance_ema8_ppm,
        l1_bid_qty, l1_ask_qty, spread_scaled, mid_price_x2
    w_ofi, w_depth, w_slope : float
        Component weights (should sum to 1.0)
    ema_alpha : float
        EMA smoothing factor for signal normalization

    Returns
    -------
    np.ndarray
        Signal array (float64), same length as data.
    """
    n = len(data)
    signal = np.zeros(n, dtype=np.float64)

    # Extract fields
    if data.dtype.names is not None:
        ofi_ema8 = np.asarray(data["ofi_l1_ema8"], dtype=np.float64)
        depth_imb = np.asarray(data["depth_imbalance_ema8_ppm"], dtype=np.float64)
        bid_qty = np.asarray(data["l1_bid_qty"], dtype=np.float64)
        ask_qty = np.asarray(data["l1_ask_qty"], dtype=np.float64)
    else:
        # Fallback: assume column order
        ofi_ema8 = np.asarray(data[:, 0], dtype=np.float64)
        depth_imb = np.asarray(data[:, 1], dtype=np.float64)
        bid_qty = np.asarray(data[:, 2], dtype=np.float64)
        ask_qty = np.asarray(data[:, 3], dtype=np.float64)

    # Normalize OFI: clip to [-1, 1]
    ofi_max = np.maximum(np.abs(ofi_ema8), 1.0)
    ofi_norm = ofi_ema8 / ofi_max

    # Normalize depth imbalance: ppm scale
    depth_norm = depth_imb / 10000.0

    # Slope proxy: log qty ratio (simplified from full LOB slope)
    bid_log = np.log1p(np.maximum(bid_qty, 0))
    ask_log = np.log1p(np.maximum(ask_qty, 0))
    slope_proxy = ask_log - bid_log

    # Raw composite
    raw = w_ofi * ofi_norm + w_depth * depth_norm + w_slope * slope_proxy

    # Online EMA normalization
    ema = 0.0
    emvar = 1.0
    for i in range(n):
        ema = (1 - ema_alpha) * ema + ema_alpha * raw[i]
        diff = raw[i] - ema
        emvar = (1 - ema_alpha) * emvar + ema_alpha * diff * diff
        sigma = max(emvar**0.5, 1e-8)
        signal[i] = max(-3.0, min(3.0, (raw[i] - ema) / sigma))

    return signal
