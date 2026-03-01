#!/usr/bin/env python3
"""Archive FactorBase adapters for research/archive/implementations/.

Wraps two high-relevance archive paper implementations as first-class
:class:`FactorBase` factors so they can be evaluated alongside the active
factor registry without modifying the archive source files.

Adapted papers
--------------
2408.03594v1  OFI + AR(5) forecast
    Cont-style Order Flow Imbalance with one-step-ahead AR autoregressive
    forecast.  Positive signal → expected buying pressure; negative → selling.

2506.07711v5  Square-root market impact from OFI
    Decaying square-root-impact price displacement driven by OFI.  Persistent
    positive signal → continued buy pressure.

Usage
-----
::

    from archive_adapters import ArchiveOFI_AR, ArchiveSqrtImpact
    from factor_registry import FactorRegistry

    FactorRegistry.register(ArchiveOFI_AR())
    FactorRegistry.register(ArchiveSqrtImpact())
"""

from __future__ import annotations

from typing import Dict

import numpy as np
from factor_registry import FactorBase

# ── Shared primitive (Cont-style OFI; identical logic in both archive papers) ─


def _compute_ofi(
    best_bid: np.ndarray,
    best_ask: np.ndarray,
    bid_sz: np.ndarray,
    ask_sz: np.ndarray,
) -> np.ndarray:
    """Compute Cont-style Order Flow Imbalance from best-level quote series.

    OFI_t = BidFlow_t − AskFlow_t where:
      BidFlow:  +bid_sz[t] on price improvement, −bid_sz[t−1] on deterioration,
                Δbid_sz otherwise.
      AskFlow:  symmetric on the ask side.
    """
    n = best_bid.size
    ofi = np.zeros(n, dtype=np.float64)
    for t in range(1, n):
        # Bid flow
        if best_bid[t] > best_bid[t - 1]:
            bid_flow = bid_sz[t]
        elif best_bid[t] < best_bid[t - 1]:
            bid_flow = -bid_sz[t - 1]
        else:
            bid_flow = bid_sz[t] - bid_sz[t - 1]

        # Ask flow
        if best_ask[t] < best_ask[t - 1]:
            ask_flow = ask_sz[t]
        elif best_ask[t] > best_ask[t - 1]:
            ask_flow = -ask_sz[t - 1]
        else:
            ask_flow = ask_sz[t] - ask_sz[t - 1]

        ofi[t] = float(bid_flow - ask_flow)
    return ofi


# ── 2408.03594v1: OFI + AR(L) forecast ───────────────────────────────────────


def _build_ar_design(
    ofi: np.ndarray,
    lags: int,
    horizon: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Build AR(lags) design matrix for OFI with given forecast horizon."""
    n = ofi.size
    rows = n - lags - horizon
    if rows <= 0:
        return np.empty((0, lags + 1), dtype=np.float64), np.empty(0, dtype=np.float64)
    X = np.zeros((rows, lags + 1), dtype=np.float64)
    y = np.zeros(rows, dtype=np.float64)
    for i in range(rows):
        t = i + lags
        X[i, 0] = 1.0
        X[i, 1:] = ofi[t - lags : t][::-1]
        y[i] = ofi[t + horizon]
    return X, y


class ArchiveOFI_AR(FactorBase):
    """OFI with AR(5) forecast — adapted from archive paper 2408.03594v1.

    Fits an AR(5) OLS model on the full OFI series and returns the aligned
    in-sample one-step-ahead forecast as the signal.  Falls back to raw OFI
    when there are insufficient samples.

    Paper: "High-frequency Order Flow Imbalance forecasting" (2408.03594v1)
    """

    @property
    def name(self) -> str:
        return "ArchiveOFI_AR"

    @property
    def paper_id(self) -> str:
        return "2408.03594v1"

    @property
    def description(self) -> str:
        return "OFI AR(5) one-step-ahead forecast (archive 2408.03594v1)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        best_bid = data["bid_prices"][:, 0].astype(np.float64)
        best_ask = data["ask_prices"][:, 0].astype(np.float64)
        bid_sz = data["bid_sizes"][:, 0].astype(np.float64)
        ask_sz = data["ask_sizes"][:, 0].astype(np.float64)

        ofi = _compute_ofi(best_bid, best_ask, bid_sz, ask_sz)
        n = len(ofi)

        lags, horizon = 5, 1
        X, y = _build_ar_design(ofi, lags=lags, horizon=horizon)
        if X.shape[0] < lags + 1:
            # Insufficient data — return raw OFI as fallback signal
            return ofi

        coef = np.linalg.lstsq(X, y, rcond=None)[0]

        # Reconstruct in-sample forecast, aligned to the original time axis.
        signal = np.zeros(n, dtype=np.float64)
        offset = lags + horizon
        for i in range(X.shape[0]):
            t = i + offset
            if t < n:
                signal[t] = float(X[i] @ coef)
        return signal


# ── 2506.07711v5: Square-root market impact ───────────────────────────────────


class ArchiveSqrtImpact(FactorBase):
    """Square-root OFI market impact — adapted from archive paper 2506.07711v5.

    Applies a decaying square-root impact model to the OFI series:

        impact_t = impact_{t-1} × exp(−λ·dt) + κ · sign(OFI_t) · √|OFI_t|

    Persistent positive impact → continued buy pressure; negative → sell
    pressure.  Decay prevents stale signals from persisting across regimes.

    Class-level parameters can be overridden by subclassing:
      ``impact_coef``  (κ)  default 0.6
      ``decay_lambda`` (λ)  default 0.05
      ``dt``                default 0.01

    Paper: "Square-root impact + decay driven by OFI" (2506.07711v5)
    """

    impact_coef: float = 0.6
    decay_lambda: float = 0.05
    dt: float = 0.01

    @property
    def name(self) -> str:
        return "ArchiveSqrtImpact"

    @property
    def paper_id(self) -> str:
        return "2506.07711v5"

    @property
    def description(self) -> str:
        return "Square-root OFI market impact signal (archive 2506.07711v5)"

    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        best_bid = data["bid_prices"][:, 0].astype(np.float64)
        best_ask = data["ask_prices"][:, 0].astype(np.float64)
        bid_sz = data["bid_sizes"][:, 0].astype(np.float64)
        ask_sz = data["ask_sizes"][:, 0].astype(np.float64)

        ofi = _compute_ofi(best_bid, best_ask, bid_sz, ask_sz)
        n = len(ofi)

        impact = np.zeros(n, dtype=np.float64)
        decay = float(np.exp(-self.decay_lambda * self.dt))
        for t in range(1, n):
            shock = self.impact_coef * np.sign(ofi[t]) * np.sqrt(abs(ofi[t]))
            impact[t] = impact[t - 1] * decay + shock
        return impact
