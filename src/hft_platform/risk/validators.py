import os
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, StormGuardState
from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec, PriceScaleProvider, SymbolMetadataPriceScaleProvider

if TYPE_CHECKING:
    from hft_platform.feed_adapter.lob_engine import LOBEngine

logger = get_logger("risk_validators")


class RiskValidator:
    def __init__(
        self,
        config: Dict[str, Any],
        price_scale_provider: PriceScaleProvider | None = None,
        lob: Optional["LOBEngine"] = None,
    ):
        self.config = config
        self.defaults = config.get("global_defaults", {})
        self.strat_configs = config.get("strategies", {})
        provider = price_scale_provider or SymbolMetadataPriceScaleProvider()
        self.price_codec = PriceCodec(provider)
        self.lob = lob
        self._shared_scale_cache: Dict[str, int] = {}

    def _scale_factor(self, symbol: str) -> int:
        cache = self._shared_scale_cache
        value = cache.get(symbol)
        if value is None:
            value = int(self.price_codec.scale_factor(symbol))
            cache[symbol] = value
        return value or 1

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        """Return (Approved, Reason)."""
        return True, "OK"


class PriceBandValidator(RiskValidator):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._max_price_cap_raw = float(self.defaults.get("max_price_cap", 5000.0))  # precision-config
        self._tick_size_raw = float(self.defaults.get("tick_size", 0.01))  # precision-config
        self._max_price_scaled_cache: Dict[str, int] = {}
        self._tick_size_scaled_cache: Dict[str, int] = {}
        self._band_ticks_cache: Dict[str, int] = {}

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        if intent.price <= 0:
            return False, "PRICE_ZERO_OR_NEG"

        # Fat Finger Protection: Absolute price cap
        scale = self._scale_factor(intent.symbol)
        max_price_scaled = self._max_price_scaled_cache.get(intent.symbol)
        if max_price_scaled is None:
            max_price_scaled = int(self._max_price_cap_raw * scale)
            self._max_price_scaled_cache[intent.symbol] = max_price_scaled

        if intent.price > max_price_scaled:
            return False, f"PRICE_EXCEEDS_CAP: {intent.price} > {max_price_scaled}"

        # LOB-relative price band validation
        if self.lob is not None:
            mid_price = self._get_mid_price(intent.symbol)
            if mid_price is not None and mid_price > 0:
                strat_cfg = self.strat_configs.get(intent.strategy_id, {})
                band_ticks = self._band_ticks_cache.get(intent.strategy_id)
                if band_ticks is None:
                    band_ticks = int(strat_cfg.get("price_band_ticks", self.defaults.get("price_band_ticks", 20)))
                    self._band_ticks_cache[intent.strategy_id] = band_ticks
                tick_size_scaled = self._tick_size_scaled_cache.get(intent.symbol)
                if tick_size_scaled is None:
                    tick_size_scaled = int(self._tick_size_raw * scale)
                    self._tick_size_scaled_cache[intent.symbol] = tick_size_scaled

                # Calculate allowed band: mid_price +/- band_ticks * tick_size
                band_width = band_ticks * tick_size_scaled
                lower_bound = mid_price - band_width
                upper_bound = mid_price + band_width

                if intent.price < lower_bound or intent.price > upper_bound:
                    return False, (
                        f"PRICE_OUTSIDE_BAND: price={intent.price} mid={mid_price} band=[{lower_bound}, {upper_bound}]"
                    )

        return True, "OK"

    def _get_mid_price(self, symbol: str) -> Optional[int]:
        """Get mid price from LOB as scaled integer.

        Note: LOB stores prices in scaled format already. The mid_price from
        get_book_snapshot is mid_price_x2/2.0, which is already scaled.
        """
        if self.lob is None:
            return None
        try:
            get_l1_scaled = getattr(self.lob, "get_l1_scaled", None)
            if callable(get_l1_scaled):
                l1 = get_l1_scaled(symbol)
                if l1 is not None and len(l1) >= 4:
                    mid_price_x2 = int(l1[3] or 0)
                    if mid_price_x2 > 0:
                        return mid_price_x2 // 2
            book = self.lob.get_book_snapshot(symbol)
            if book and book.get("mid_price"):
                # mid_price from LOB is already in scaled units (as float)
                # Just convert to int
                return int(book["mid_price"])
        except Exception as e:
            logger.warning("Failed to get mid price from LOB", symbol=symbol, error=str(e))
        return None


class MaxNotionalValidator(RiskValidator):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._default_max_notional_raw = self.defaults.get("max_notional", 10_000_000)
        self._max_notional_scaled_cache: Dict[tuple[str, str], int] = {}

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        cache_key = (intent.strategy_id, intent.symbol)
        max_notional_scaled = self._max_notional_scaled_cache.get(cache_key)
        if max_notional_scaled is None:
            strat_cfg = self.strat_configs.get(intent.strategy_id, {})
            max_notional_raw = strat_cfg.get("max_notional", self._default_max_notional_raw)
            scale = self._scale_factor(intent.symbol)
            max_notional_scaled = int(max_notional_raw * scale)
            self._max_notional_scaled_cache[cache_key] = max_notional_scaled

        notional_scaled = intent.price * intent.qty
        if notional_scaled > max_notional_scaled:
            return False, f"MAX_NOTIONAL_EXCEEDED: {notional_scaled} > {max_notional_scaled}"

        return True, "OK"


class PositionLimitValidator(RiskValidator):
    """Stateless validator: rejects orders where abs(qty) exceeds max_position_lots."""

    __slots__ = ("_default_max_position_lots", "_max_position_cache")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._default_max_position_lots: int = int(self.defaults.get("max_position_lots", 1_000))
        self._max_position_cache: Dict[str, int] = {}

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        cache_key = intent.strategy_id
        max_lots = self._max_position_cache.get(cache_key)
        if max_lots is None:
            strat_cfg = self.strat_configs.get(intent.strategy_id, {})
            max_lots = int(strat_cfg.get("max_position_lots", self._default_max_position_lots))
            self._max_position_cache[cache_key] = max_lots

        if abs(intent.qty) > max_lots:
            return False, f"POSITION_LIMIT_EXCEEDED: abs({intent.qty}) > {max_lots}"

        return True, "OK"


class DailyLossLimitValidator(RiskValidator):
    """Stateful validator: rejects orders when accumulated daily loss exceeds limit.

    Tracks cumulative PnL updates per strategy (realized) plus a single platform-wide
    unrealized PnL value supplied by the caller via update_unrealized().  Both are
    combined when evaluating the limit.

    Reset boundary: 05:00 Taiwan Standard Time (UTC+8) = 21:00 UTC of the
    *previous* calendar day.  This aligns with Taiwan futures settlement.

    Prices are scaled int x10000 per platform conventions.
    Uses timebase.now_ns() for time — never datetime.now().
    """

    __slots__ = (
        "_default_max_daily_loss",
        "_accumulated_loss",
        "_current_reset_boundary_ns",
        "_unrealized_pnl",
        "halt_triggered",
    )

    # Nanoseconds per calendar day
    _NS_PER_DAY: int = 86_400 * 1_000_000_000
    # 05:00 Taiwan (UTC+8) = 21:00 UTC = 21 * 3600 seconds into the UTC day
    _RESET_OFFSET_NS: int = 21 * 3600 * 1_000_000_000

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Stored as a positive threshold; loss is compared as abs value
        self._default_max_daily_loss: int = int(self.defaults.get("max_daily_loss", 500_000_000))
        # Accumulated realized PnL per strategy (negative = loss, positive = gain); scaled int
        self._accumulated_loss: Dict[str, int] = {}
        # Epoch-ns of the last 21:00 UTC reset boundary (cached)
        self._current_reset_boundary_ns: int = self._current_boundary_ns()
        # Platform-wide unrealized PnL (scaled int, updated externally)
        self._unrealized_pnl: int = 0
        # Set to True when limit is breached; cleared only by _force_reset()
        self.halt_triggered: bool = False

    @staticmethod
    def _current_boundary_ns() -> int:
        """Return epoch-ns of the most recent 21:00 UTC reset boundary."""
        now_ns = timebase.now_ns()
        ns_per_day = 86_400 * 1_000_000_000
        offset = 21 * 3600 * 1_000_000_000
        # Shift time back by offset so that floor-to-day gives us the last 21:00 UTC
        return ((now_ns - offset) // ns_per_day) * ns_per_day + offset

    def _maybe_reset(self) -> None:
        """Reset accumulated losses if the 05:00 Taiwan (21:00 UTC) boundary has passed."""
        boundary_ns = self._current_boundary_ns()
        if boundary_ns != self._current_reset_boundary_ns:
            logger.info(
                "DailyLossLimitValidator: daily reset (05:00 TST)",
                prev_boundary_ns=self._current_reset_boundary_ns,
                new_boundary_ns=boundary_ns,
                strategies_reset=list(self._accumulated_loss.keys()),
            )
            self._accumulated_loss.clear()
            self._unrealized_pnl = 0
            self.halt_triggered = False
            self._current_reset_boundary_ns = boundary_ns

    def _force_reset(self) -> None:
        """Unconditionally clear all accumulated state (e.g. for testing or manual override)."""
        self._accumulated_loss.clear()
        self._unrealized_pnl = 0
        self.halt_triggered = False
        self._current_reset_boundary_ns = self._current_boundary_ns()

    def update_unrealized(self, unrealized_scaled: int) -> None:
        """Update the platform-wide unrealized PnL used in loss calculations.

        Args:
            unrealized_scaled: Total unrealized PnL in scaled int (x10000).
                               Negative = unrealized loss; positive = unrealized gain.
        """
        self._unrealized_pnl = unrealized_scaled

    def record_pnl(self, strategy_id: str, pnl_delta: int) -> None:
        """Record a realized PnL delta (negative = loss) for a strategy.

        Args:
            strategy_id: Strategy identifier string.
            pnl_delta: Realized PnL change in scaled int (x10000). Negative = loss.
        """
        self._maybe_reset()
        current = self._accumulated_loss.get(strategy_id, 0)
        self._accumulated_loss[strategy_id] = current + pnl_delta

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        self._maybe_reset()

        accumulated = self._accumulated_loss.get(intent.strategy_id, 0)
        # Combine realized + unrealized PnL for the total loss picture
        total_pnl = accumulated + self._unrealized_pnl

        if total_pnl >= 0:
            # Net gain or breakeven — no loss to check
            return True, "OK"

        loss_magnitude = -total_pnl  # positive value representing combined loss

        strat_cfg = self.strat_configs.get(intent.strategy_id, {})
        max_daily_loss = int(strat_cfg.get("max_daily_loss", self._default_max_daily_loss))

        if loss_magnitude >= max_daily_loss:
            self.halt_triggered = True
            logger.warning(
                "DailyLossLimitValidator: daily loss limit exceeded",
                strategy_id=intent.strategy_id,
                accumulated_loss=accumulated,
                unrealized_pnl=self._unrealized_pnl,
                total_pnl=total_pnl,
                max_daily_loss=max_daily_loss,
            )
            return False, f"DAILY_LOSS_LIMIT_EXCEEDED: loss={loss_magnitude} >= limit={max_daily_loss}"

        return True, "OK"


class PerSymbolNotionalValidator(RiskValidator):
    """Reject orders where per-symbol notional (price * qty / scale) exceeds the configured limit.

    Config resolution order:
      1. strategies.<id>.symbol_limits.<symbol>.max_notional
      2. global_defaults.per_symbol_max_notional
      3. Hard-coded fallback (50_000_000)

    Cache is keyed by (strategy_id, symbol) with a bounded cardinality of
    ``_MAX_CACHE_ENTRIES`` (default 10_000) per CE2-12 governance rule.
    """

    __slots__ = (
        "_default_per_symbol_max_notional_raw",
        "_per_symbol_notional_cache",
        "_MAX_CACHE_ENTRIES",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._default_per_symbol_max_notional_raw: int = int(self.defaults.get("per_symbol_max_notional", 50_000_000))
        self._per_symbol_notional_cache: Dict[tuple[str, str], int] = {}
        self._MAX_CACHE_ENTRIES: int = int(os.getenv("HFT_RISK_PER_SYMBOL_CACHE_MAX", "10000"))

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        cache_key = (intent.strategy_id, intent.symbol)
        max_notional_scaled = self._per_symbol_notional_cache.get(cache_key)
        if max_notional_scaled is None:
            # Resolve config: strategy-level symbol_limits > global default
            strat_cfg = self.strat_configs.get(intent.strategy_id, {})
            symbol_limits = strat_cfg.get("symbol_limits", {})
            sym_cfg = symbol_limits.get(intent.symbol, {})
            max_notional_raw = sym_cfg.get(
                "max_notional",
                self._default_per_symbol_max_notional_raw,
            )

            scale = self._scale_factor(intent.symbol)
            max_notional_scaled = int(int(max_notional_raw) * scale)

            # Bounded cache — evict all on overflow (simple, safe)
            if len(self._per_symbol_notional_cache) >= self._MAX_CACHE_ENTRIES:
                logger.warning(
                    "PerSymbolNotionalValidator cache overflow, clearing",
                    size=len(self._per_symbol_notional_cache),
                    max=self._MAX_CACHE_ENTRIES,
                )
                self._per_symbol_notional_cache.clear()

            self._per_symbol_notional_cache[cache_key] = max_notional_scaled

        # notional = price * qty (both in scaled-int space).
        # To compare against a raw-currency limit that was also pre-scaled,
        # the comparison is direct: price_scaled * qty vs max_notional_raw * scale.
        notional_scaled = intent.price * intent.qty
        if notional_scaled > max_notional_scaled:
            return False, (f"PER_SYMBOL_NOTIONAL_EXCEEDED: {notional_scaled} > {max_notional_scaled}")

        return True, "OK"

    def clear_cache(self) -> None:
        """Clear the per-symbol notional cache (used by config hot-reload)."""
        self._per_symbol_notional_cache.clear()


class StormGuardFSM:
    """Thin shim that delegates to StormGuard (storm_guard.py)."""

    def __init__(self, config: Dict[str, Any]):
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

        self.config = config
        sg_cfg = config.get("storm_guard", {})
        warm = sg_cfg.get("warm_threshold", -200_000)
        storm = sg_cfg.get("storm_threshold", -500_000)
        halt = sg_cfg.get("halt_threshold", -1_000_000)
        thresholds = RiskThresholds(
            warm_drawdown_bps=warm,
            storm_drawdown_bps=storm,
            halt_drawdown_bps=halt,
        )
        self._guard = StormGuard(thresholds=thresholds)

    @property
    def state(self) -> StormGuardState:
        return self._guard.state

    @state.setter
    def state(self, value: int | StormGuardState) -> None:
        self._guard.state = StormGuardState(int(value))

    def update_pnl(self, pnl: int) -> None:
        self._guard.update(drawdown_bps=pnl)

    def trigger_halt(self, reason: str) -> None:
        self._guard.trigger_halt(reason)

    def validate(self, intent: OrderIntent) -> Tuple[bool, str]:
        return self._guard.validate(intent)

    def reload_thresholds(self, config: dict) -> None:
        self._guard.reload_thresholds(config)
