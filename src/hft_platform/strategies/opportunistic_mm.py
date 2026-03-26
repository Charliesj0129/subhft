"""Opportunistic Market Maker — quotes only when spread is wide enough.

Extends SimpleMarketMaker with a spread-width gate:
- Only sends quotes when spread > threshold (default 0.8 bps)
- Cancels existing quotes when spread tightens below threshold
- Inherits imbalance-driven skewing and inventory management from SimpleMM

v2 Enhancement (Round 16, Candidates A+B):
- Depth-normalized OFI signal (Takahashi 2508.06788): modulates entry quality
- Reversal filter (Albers et al. 2502.18625): gates quoting on reversal conditions
  Features: return autocovariance, TOB survival, depth-normalized OFI
  Negative autocov + short TOB survival + elevated depth-norm OFI = reversal likely

Economics (TXFD6):
    RT cost: 0.18 bps (commission only, no sell tax on futures)
    Price ~33445 pts, tick = 1 pt
    1 tick = 0.3 bps, 2 ticks = 0.6 bps, 3 ticks = 0.9 bps
    0.8 bps threshold = ~3 ticks (positive expectancy with 2+ tick spread)
"""

from __future__ import annotations

from structlog import get_logger

from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
from hft_platform.strategies.simple_mm import SimpleMarketMaker

logger = get_logger("strategy.opportunistic_mm")

# Feature indices for lob_shared_v2
_IDX_OFI_DEPTH_NORM_PPM = 16
_IDX_RET_AUTOCOV_5S_X1E6 = 17
_IDX_TOB_SURVIVAL_MS = 18
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9

# Log sampling: emit debug log every N events to avoid flooding
_LOG_SAMPLE_INTERVAL = 500


class OpportunisticMM(SimpleMarketMaker):
    """MM that only quotes when spread exceeds a cost-viable threshold.

    Parameters
    ----------
    strategy_id : str
        Strategy identifier.
    spread_threshold_bps : float
        Minimum spread in bps to activate quoting.  Default 0.8.
    reversal_filter_enabled : bool
        Enable reversal-condition gating (v2 features required). Default False.
    reversal_autocov_threshold : int
        Autocovariance threshold (x1e6 scale). Negative = oscillating prices.
        Quote only when autocov < threshold. Default 0 (any negative autocov).
    reversal_tob_max_ms : int
        Maximum TOB survival time in ms. Short survival = volatile TOB.
        Quote only when tob_survival_ms < threshold. Default 2000 (2s).
    reversal_min_depth_ratio : float
        Minimum near-side depth / far-side depth ratio.
        Low ratio = adverse fill likely; skip. Default 0.3.
    **kwargs
        Passed through to SimpleMarketMaker (tick_size_ratio_pct, etc.).
    """

    def __init__(
        self,
        strategy_id: str = "opportunistic_mm",
        spread_threshold_bps: float = 0.8,
        reversal_filter_enabled: bool = False,
        reversal_autocov_threshold: int = 0,
        reversal_tob_max_ms: int = 2000,
        reversal_min_depth_ratio: float = 0.3,
        **kwargs: object,
    ) -> None:
        super().__init__(strategy_id=strategy_id, **kwargs)
        self._spread_threshold_bps: float = spread_threshold_bps
        self._reversal_filter_enabled: bool = reversal_filter_enabled
        self._reversal_autocov_threshold: int = reversal_autocov_threshold
        self._reversal_tob_max_ms: int = reversal_tob_max_ms
        self._reversal_min_depth_ratio: float = reversal_min_depth_ratio
        # Cache latest feature tuple per symbol
        self._feature_cache: dict[str, tuple[int | float, ...]] = {}
        # Observability counters (no hot-path allocation — plain int)
        self._stats_count: int = 0
        self._gate_passed_count: int = 0
        self._gate_blocked_count: int = 0
        self._invalid_data_count: int = 0
        self._reversal_blocked_count: int = 0

        logger.info(
            "OpportunisticMM initialized",
            strategy_id=strategy_id,
            spread_threshold_bps=spread_threshold_bps,
            reversal_filter_enabled=reversal_filter_enabled,
        )

    def on_features(self, event: FeatureUpdateEvent) -> None:
        """Cache latest feature tuple for use in on_stats."""
        if event.values is not None:
            self._feature_cache[event.symbol] = event.values

    def _check_reversal_condition(self, symbol: str) -> bool:
        """Check if current market state suggests a reversal (favorable for maker).

        Returns True if conditions favor quoting, False to skip.
        If reversal filter is disabled or features unavailable, returns True (permissive).
        """
        if not self._reversal_filter_enabled:
            return True

        features = self._feature_cache.get(symbol)
        if features is None or len(features) <= _IDX_TOB_SURVIVAL_MS:
            # No v2 features available — fall back to permissive
            return True

        autocov = int(features[_IDX_RET_AUTOCOV_5S_X1E6])
        tob_survival_ms = int(features[_IDX_TOB_SURVIVAL_MS])
        l1_bid = int(features[_IDX_L1_BID_QTY])
        l1_ask = int(features[_IDX_L1_ASK_QTY])

        # Condition 1: Negative autocovariance (oscillating prices → reversal likely)
        if autocov >= self._reversal_autocov_threshold:
            return False

        # Condition 2: TOB is unstable (short survival → volatile, reversal possible)
        if tob_survival_ms > self._reversal_tob_max_ms:
            return False

        # Condition 3: Depth ratio check — avoid quoting when near-side is too thin
        # (extreme adverse selection risk)
        total = l1_bid + l1_ask
        if total > 0:
            min_side = min(l1_bid, l1_ask)
            ratio = float(min_side) / float(total)
            if ratio < self._reversal_min_depth_ratio:
                return False

        return True

    def on_stats(self, event: LOBStatsEvent) -> None:
        symbol = event.symbol
        self._stats_count += 1

        # Guard: skip if data invalid
        if event.mid_price_x2 is None or event.spread_scaled is None:
            self._invalid_data_count += 1
            if self._stats_count % _LOG_SAMPLE_INTERVAL == 1:
                logger.debug(
                    "opmm_invalid_data",
                    symbol=symbol,
                    mid_price_x2=event.mid_price_x2,
                    spread_scaled=event.spread_scaled,
                    best_bid=event.best_bid,
                    best_ask=event.best_ask,
                    stats_n=self._stats_count,
                )
            return
        if event.mid_price_x2 <= 0 or event.spread_scaled <= 0:
            self._invalid_data_count += 1
            if self._stats_count % _LOG_SAMPLE_INTERVAL == 1:
                logger.debug(
                    "opmm_zero_price",
                    symbol=symbol,
                    mid_price_x2=event.mid_price_x2,
                    spread_scaled=event.spread_scaled,
                    best_bid=event.best_bid,
                    best_ask=event.best_ask,
                    stats_n=self._stats_count,
                )
            return

        # Compute spread in bps: spread_bps = spread_scaled / mid_price_x2 * 20000
        spread_bps = event.spread_scaled / event.mid_price_x2 * 20000.0

        # Sampled diagnostic log (every N events)
        if self._stats_count % _LOG_SAMPLE_INTERVAL == 1:
            logger.info(
                "opmm_stats_sample",
                symbol=symbol,
                spread_bps=round(spread_bps, 3),
                threshold_bps=self._spread_threshold_bps,
                mid_price_x2=event.mid_price_x2,
                spread_scaled=event.spread_scaled,
                best_bid=event.best_bid,
                best_ask=event.best_ask,
                imbalance=round(event.imbalance, 3),
                stats_n=self._stats_count,
                gate_passed=self._gate_passed_count,
                gate_blocked=self._gate_blocked_count,
                invalid=self._invalid_data_count,
                reversal_blocked=self._reversal_blocked_count,
            )

        # Spread gate: only quote when spread is wide enough to cover RT cost
        if spread_bps < self._spread_threshold_bps:
            self._gate_blocked_count += 1
            # Cancel existing quotes to avoid stale orders at tight spreads
            if hasattr(self, "_bid_oid") and self._bid_oid:
                self.cancel(symbol, self._bid_oid)
                self._bid_oid = None
            if hasattr(self, "_ask_oid") and self._ask_oid:
                self.cancel(symbol, self._ask_oid)
                self._ask_oid = None
            return

        # Reversal filter gate: only quote when conditions favor maker
        if not self._check_reversal_condition(symbol):
            self._reversal_blocked_count += 1
            return

        # Wide spread + reversal conditions met: delegate to SimpleMarketMaker
        self._gate_passed_count += 1
        if self._gate_passed_count <= 10 or self._gate_passed_count % 100 == 0:
            logger.info(
                "opmm_quoting",
                symbol=symbol,
                spread_bps=round(spread_bps, 3),
                best_bid=event.best_bid,
                best_ask=event.best_ask,
                gate_passed_n=self._gate_passed_count,
            )
        super().on_stats(event)

    @property
    def spread_threshold_bps(self) -> float:
        return self._spread_threshold_bps

    @property
    def reversal_filter_enabled(self) -> bool:
        return self._reversal_filter_enabled
