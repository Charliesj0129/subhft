"""Opportunistic Market Maker — quotes only when spread is wide enough.

Extends SimpleMarketMaker with a spread-width gate:
- Only sends quotes when spread > threshold (in points, not bps)
- Cancels existing quotes when spread tightens below threshold
- Inherits imbalance-driven skewing and inventory management from SimpleMM

Point-based threshold rationale:
    Breakeven is a FIXED point cost (RT fee / point_value), independent of price level.
    Using bps incorrectly ties the threshold to the index level, causing:
    - At high prices: threshold too lax (below breakeven)
    - At low prices: threshold too strict (misses profitable trades)
    Points-based threshold is stable across all price levels.

v2 Enhancement (Round 16, Candidates A+B):
- Depth-normalized OFI signal (Takahashi 2508.06788): modulates entry quality
- Reversal filter (Albers et al. 2502.18625): gates quoting on reversal conditions
  Features: return autocovariance, TOB survival, depth-normalized OFI
  Negative autocov + short TOB survival + elevated depth-norm OFI = reversal likely

Economics (TMFD6 — 微台指):
    1 point = 10 NTD, RT cost = 40 NTD = 4 points
    Breakeven spread: > 4 points
    Default threshold: 5 points (1 point edge over breakeven)
"""

from __future__ import annotations

from structlog import get_logger

from hft_platform.events import FeatureUpdateEvent, GapEvent, LOBStatsEvent
from hft_platform.strategies.simple_mm import SimpleMarketMaker
from hft_platform.strategy.base import QUALITY_FLAGS_CORRUPT

logger = get_logger("strategy.opportunistic_mm")

# Feature indices for lob_shared_v2/v3 (v3 is superset; indices [0]-[21] unchanged)
_IDX_OFI_DEPTH_NORM_PPM = 16
_IDX_RET_AUTOCOV_5S_X1E6 = 17
_IDX_TOB_SURVIVAL_MS = 18
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9
_IDX_TOXICITY_EMA50_X1000 = 21

# Log sampling: emit debug log every N events to avoid flooding
_LOG_SAMPLE_INTERVAL = 500

# Price scale factor (all prices are int x10000)
_PRICE_SCALE = 10000


class OpportunisticMM(SimpleMarketMaker):
    """MM that only quotes when spread exceeds a cost-viable threshold.

    Parameters
    ----------
    strategy_id : str
        Strategy identifier.
    spread_threshold_pts : int
        Minimum spread in index points to activate quoting.  Default 5.
        This is the number of raw price points (ticks), NOT bps.
        Example: TMFD6 breakeven = 4 pts (40 NTD / 10 NTD per pt),
        so threshold = 5 gives 1 pt edge.
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
        spread_threshold_pts: int = 5,
        reversal_filter_enabled: bool = False,
        reversal_autocov_threshold: int = 0,
        reversal_tob_max_ms: int = 2000,
        reversal_min_depth_ratio: float = 0.3,
        toxicity_filter_enabled: bool = False,
        toxicity_max_threshold: int = 700,
        **kwargs: object,
    ) -> None:
        super().__init__(strategy_id=strategy_id, **kwargs)
        self._spread_threshold_pts: int = int(spread_threshold_pts)
        # Pre-compute scaled threshold to avoid per-tick multiplication
        self._spread_threshold_scaled: int = self._spread_threshold_pts * _PRICE_SCALE
        self._reversal_filter_enabled: bool = reversal_filter_enabled
        self._reversal_autocov_threshold: int = reversal_autocov_threshold
        self._reversal_tob_max_ms: int = reversal_tob_max_ms
        self._reversal_min_depth_ratio: float = reversal_min_depth_ratio
        self._toxicity_filter_enabled: bool = toxicity_filter_enabled
        self._toxicity_max_threshold: int = toxicity_max_threshold
        self._toxicity_blocked_count: int = 0
        # Cache latest feature tuple per symbol
        self._feature_cache: dict[str, tuple[int | float, ...]] = {}
        # Observability counters (no hot-path allocation — plain int)
        self._stats_count: int = 0
        self._gate_passed_count: int = 0
        self._gate_blocked_count: int = 0
        self._invalid_data_count: int = 0
        self._reversal_blocked_count: int = 0
        # Tracked order IDs for spread-gate cancellation
        self._bid_oid: str | None = None
        self._ask_oid: str | None = None

        logger.info(
            "OpportunisticMM initialized",
            strategy_id=strategy_id,
            spread_threshold_pts=self._spread_threshold_pts,
            spread_threshold_scaled=self._spread_threshold_scaled,
            reversal_filter_enabled=reversal_filter_enabled,
            toxicity_filter_enabled=toxicity_filter_enabled,
        )

    def on_features(self, event: FeatureUpdateEvent) -> None:
        """Cache latest feature tuple for use in on_stats."""
        if event.values is None:
            return
        # Skip corrupted features (GAP, STATE_RESET, OUT_OF_ORDER)
        if event.quality_flags & QUALITY_FLAGS_CORRUPT:
            logger.debug(
                "opmm_features_skipped_corrupt",
                symbol=event.symbol,
                quality_flags=event.quality_flags,
            )
            return
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

    def _check_toxicity_condition(self, symbol: str) -> bool:
        """Check if current flow toxicity is low enough to safely quote.

        Returns True if conditions favor quoting, False to skip.
        If toxicity filter is disabled or features unavailable, returns True (permissive).
        """
        if not self._toxicity_filter_enabled:
            return True

        features = self._feature_cache.get(symbol)
        if features is None or len(features) <= _IDX_TOXICITY_EMA50_X1000:
            return True

        toxicity = int(features[_IDX_TOXICITY_EMA50_X1000])
        if toxicity > self._toxicity_max_threshold:
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

        spread_pts = event.spread_scaled // _PRICE_SCALE

        # Sampled diagnostic log (every N events)
        if self._stats_count % _LOG_SAMPLE_INTERVAL == 1:
            logger.info(
                "opmm_stats_sample",
                symbol=symbol,
                spread_pts=spread_pts,
                spread_scaled=event.spread_scaled,
                threshold_pts=self._spread_threshold_pts,
                mid_price_x2=event.mid_price_x2,
                best_bid=event.best_bid,
                best_ask=event.best_ask,
                imbalance=round(event.imbalance, 3),
                stats_n=self._stats_count,
                gate_passed=self._gate_passed_count,
                gate_blocked=self._gate_blocked_count,
                invalid=self._invalid_data_count,
                reversal_blocked=self._reversal_blocked_count,
                toxicity_blocked=self._toxicity_blocked_count,
            )

        # Spread gate: only quote when spread >= threshold (integer comparison, no float)
        if event.spread_scaled < self._spread_threshold_scaled:
            self._gate_blocked_count += 1
            # Cancel existing quotes to avoid stale orders at tight spreads
            if self._bid_oid:
                self.cancel(symbol, self._bid_oid)
                self._bid_oid = None
            if self._ask_oid:
                self.cancel(symbol, self._ask_oid)
                self._ask_oid = None
            return

        # Reversal filter gate: only quote when conditions favor maker
        if not self._check_reversal_condition(symbol):
            self._reversal_blocked_count += 1
            return

        # Toxicity filter gate: skip quoting when flow is too toxic (adverse selection)
        if not self._check_toxicity_condition(symbol):
            self._toxicity_blocked_count += 1
            return

        # Wide spread + reversal conditions met: delegate to SimpleMarketMaker
        self._gate_passed_count += 1
        if self._gate_passed_count <= 10 or self._gate_passed_count % 100 == 0:
            logger.info(
                "opmm_quoting",
                symbol=symbol,
                spread_pts=spread_pts,
                best_bid=event.best_bid,
                best_ask=event.best_ask,
                gate_passed_n=self._gate_passed_count,
            )
        super().on_stats(event)

    def on_gap(self, event: GapEvent) -> None:
        """Reset stale streaming state after bus overflow."""
        self._feature_cache.clear()
        self._bid_oid = None
        self._ask_oid = None
        logger.warning(
            "opmm_gap_event_state_reset",
            missed=event.missed_count,
            strategy=self.strategy_id,
        )

    @property
    def spread_threshold_pts(self) -> int:
        return self._spread_threshold_pts

    @property
    def reversal_filter_enabled(self) -> bool:
        return self._reversal_filter_enabled
