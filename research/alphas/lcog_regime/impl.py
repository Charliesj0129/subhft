"""
L-COG Regime-Conditioned KDJ Alpha
===================================
Uses L1-L5 volume-weighted center of gravity as a regime classifier
to enhance R32b KDJ(QI_1) signals.  COG captures resting order imbalance
independent of L1 queue imbalance (empirical corr = 0.027).

NOT a standalone mean-reversion signal (CBS territory, killed in R14-R25).
"""
from __future__ import annotations

from research.registry.schemas import (
    AlphaManifest,
    AlphaProtocol,
    AlphaStatus,
)

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_KDJ_K_PERIOD = 9
_KDJ_D_PERIOD = 3
_COG_EMA_PERIOD = 9  # smooth COG deviation across bars
_WARMUP_BARS = max(_KDJ_K_PERIOD, _COG_EMA_PERIOD) + 5
_BOOK_LEVELS = 5


class LcogRegimeAlpha:
    """Regime-conditioned KDJ alpha using L-COG as regime classifier."""

    __slots__ = (
        "_signal",
        "_bar_count",
        "_k_prev",
        "_d_prev",
        "_cog_dev_ema",
        "_qi_ring",
        "_qi_high_ring",
        "_qi_low_ring",
        "_qi_idx",
        "_warmed_up",
    )

    def __init__(self) -> None:
        self._signal: float = 0.0
        self._bar_count: int = 0
        self._k_prev: float = 50.0
        self._d_prev: float = 50.0
        self._cog_dev_ema: float = 0.0
        # Pre-allocated ring for KDJ rolling window
        self._qi_ring = np.zeros(_KDJ_K_PERIOD, dtype=np.float64)
        self._qi_high_ring = np.zeros(_KDJ_K_PERIOD, dtype=np.float64)
        self._qi_low_ring = np.zeros(_KDJ_K_PERIOD, dtype=np.float64)
        self._qi_idx: int = 0
        self._warmed_up: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="lcog_regime",
            hypothesis=(
                "L1-L5 COG regime classifier enhances R32b KDJ(QI_1) signals. "
                "COG deviation from mid captures resting imbalance independent "
                "of L1 queue imbalance (corr=0.027)."
            ),
            formula=(
                "regime = sign(EMA(cog_mid - mid, N)); "
                "signal = KDJ_K * amplify(regime, KDJ_direction)"
            ),
            paper_refs=("2411.13594v1", "1907.06230v2", "2112.13213v4"),
            data_fields=(
                "bids_price", "bids_vol",
                "asks_price", "asks_vol",
                "mid_price", "exch_ts",
            ),
            complexity="O(1)",
            status=AlphaStatus.DRAFT,
            tier=None,
            rust_module=None,
            latency_profile=None,
            roles_used=("planner", "code-reviewer"),
            skills_used=("iterative-retrieval", "validation-gate"),
            feature_set_version=None,
        )

    def update_bar(
        self,
        qi_close: float,
        qi_high: float,
        qi_low: float,
        bid_prices: np.ndarray,
        bid_vols: np.ndarray,
        ask_prices: np.ndarray,
        ask_vols: np.ndarray,
        mid_price: float,
    ) -> float:
        """
        Update on each bar with:
          - qi_close/high/low: bar OHLC of QI_1
          - bid_prices/vols, ask_prices/vols: L1-L5 snapshot at bar close (scaled int)
          - mid_price: mid at bar close (float, in points)

        Returns signal value.
        """
        # --- COG computation (L1-L5 volume-weighted) ---
        bid_p = bid_prices[:_BOOK_LEVELS].astype(np.float64)
        bid_v = bid_vols[:_BOOK_LEVELS].astype(np.float64)
        ask_p = ask_prices[:_BOOK_LEVELS].astype(np.float64)
        ask_v = ask_vols[:_BOOK_LEVELS].astype(np.float64)

        total_bv = bid_v.sum()
        total_av = ask_v.sum()

        cog_bid = np.dot(bid_p, bid_v) / total_bv if total_bv > 0 else bid_p[0]
        cog_ask = np.dot(ask_p, ask_v) / total_av if total_av > 0 else ask_p[0]
        cog_mid = (cog_bid + cog_ask) / 2.0

        cog_dev = cog_mid - mid_price  # positive = demand heavier than supply

        # EMA of COG deviation
        alpha = 2.0 / (_COG_EMA_PERIOD + 1)
        if self._bar_count == 0:
            self._cog_dev_ema = cog_dev
        else:
            self._cog_dev_ema = alpha * cog_dev + (1.0 - alpha) * self._cog_dev_ema

        regime = 1.0 if self._cog_dev_ema > 0 else (-1.0 if self._cog_dev_ema < 0 else 0.0)

        # --- KDJ on QI_1 bar series ---
        idx = self._qi_idx % _KDJ_K_PERIOD
        self._qi_ring[idx] = qi_close
        self._qi_high_ring[idx] = qi_high
        self._qi_low_ring[idx] = qi_low
        self._qi_idx += 1

        filled = min(self._qi_idx, _KDJ_K_PERIOD)
        if filled < _KDJ_K_PERIOD:
            self._bar_count += 1
            self._signal = 0.0
            return self._signal

        lo = self._qi_low_ring[:filled].min()
        hi = self._qi_high_ring[:filled].max()
        if hi - lo > 1e-12:
            rsv = (qi_close - lo) / (hi - lo) * 100.0
        else:
            rsv = 50.0

        k_val = 2.0 / 3.0 * self._k_prev + 1.0 / 3.0 * rsv
        d_val = 2.0 / 3.0 * self._d_prev + 1.0 / 3.0 * k_val
        self._k_prev = k_val
        self._d_prev = d_val

        self._bar_count += 1
        if self._bar_count < _WARMUP_BARS:
            self._signal = 0.0
            return self._signal

        self._warmed_up = True

        # --- Regime conditioning ---
        # KDJ_K > 50 = bullish momentum, < 50 = bearish
        kdj_direction = 1.0 if k_val > 50.0 else -1.0

        # Amplify when regime aligns with KDJ direction, attenuate otherwise
        if regime * kdj_direction > 0:
            # Aligned: amplify signal
            self._signal = k_val
        else:
            # Misaligned: attenuate (halve deviation from 50)
            self._signal = 50.0 + (k_val - 50.0) * 0.5

        return self._signal

    # --- AlphaProtocol interface ---
    def update(self, *args, **kwargs) -> float:
        """Generic update — delegates to update_bar if kwargs match."""
        if kwargs:
            return self.update_bar(**kwargs)
        self._signal = 0.0
        return self._signal

    def reset(self) -> None:
        self._signal = 0.0
        self._bar_count = 0
        self._k_prev = 50.0
        self._d_prev = 50.0
        self._cog_dev_ema = 0.0
        self._qi_ring[:] = 0.0
        self._qi_high_ring[:] = 0.0
        self._qi_low_ring[:] = 0.0
        self._qi_idx = 0
        self._warmed_up = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = LcogRegimeAlpha
