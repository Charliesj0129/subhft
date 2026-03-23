import os
import time
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, StormGuardState
from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec, PriceScaleProvider, SymbolMetadataPriceScaleProvider
from hft_platform.observability.metrics import MetricsRegistry

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
    """Stateful validator: rejects orders when accumulated daily realized loss exceeds limit.

    Tracks cumulative PnL updates per strategy. Caller must invoke record_pnl() to
    register realized PnL changes. Date-based reset occurs automatically on check().

    Prices are scaled int x10000 per platform conventions.
    Uses timebase.now_ns() for time — never datetime.now().
    """

    __slots__ = (
        "_default_max_daily_loss",
        "_accumulated_loss",
        "_current_date_ns",
        "_ns_per_day",
    )

    # Nanoseconds per calendar day
    _NS_PER_DAY: int = 86_400 * 1_000_000_000

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Stored as a positive threshold; loss is compared as abs value
        self._default_max_daily_loss: int = int(self.defaults.get("max_daily_loss", 500_000_000))
        # Accumulated PnL per strategy (negative = loss, positive = gain); scaled int
        self._accumulated_loss: Dict[str, int] = {}
        # Epoch-ns of midnight UTC for the current trading day (cached)
        self._current_date_ns: int = self._today_midnight_ns()

    @staticmethod
    def _today_midnight_ns() -> int:
        """Return epoch nanoseconds for UTC midnight of the current day."""
        now_ns = timebase.now_ns()
        # Floor to day boundary
        ns_per_day = 86_400 * 1_000_000_000
        return (now_ns // ns_per_day) * ns_per_day

    def _maybe_reset(self) -> None:
        """Reset accumulated losses if the calendar date has rolled over."""
        today_ns = self._today_midnight_ns()
        if today_ns != self._current_date_ns:
            logger.info(
                "DailyLossLimitValidator: daily reset",
                prev_date_ns=self._current_date_ns,
                new_date_ns=today_ns,
                strategies_reset=list(self._accumulated_loss.keys()),
            )
            self._accumulated_loss.clear()
            self._current_date_ns = today_ns

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
        if accumulated >= 0:
            # Net gain or breakeven — no loss to check
            return True, "OK"

        loss_magnitude = -accumulated  # positive value representing loss

        strat_cfg = self.strat_configs.get(intent.strategy_id, {})
        max_daily_loss = int(strat_cfg.get("max_daily_loss", self._default_max_daily_loss))

        if loss_magnitude >= max_daily_loss:
            logger.warning(
                "DailyLossLimitValidator: daily loss limit exceeded",
                strategy_id=intent.strategy_id,
                accumulated_loss=accumulated,
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
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.state = StormGuardState.NORMAL
        self.pnl_drawdown: int = 0
        self.metrics = MetricsRegistry.get()

        sg_cfg = config.get("storm_guard", {})
        self.warm = sg_cfg.get("warm_threshold", -200_000)
        self.storm = sg_cfg.get("storm_threshold", -500_000)
        self.halt = sg_cfg.get("halt_threshold", -1_000_000)
        self._storm_cooldown_s: float = float(os.getenv("HFT_STORMGUARD_STORM_COOLDOWN_S", "30"))  # precision-time
        self._halt_cooldown_s: float = float(os.getenv("HFT_STORMGUARD_HALT_COOLDOWN_S", "60"))  # precision-time
        self._de_escalate_threshold: int = int(os.getenv("HFT_STORMGUARD_DE_ESCALATE_N", "5"))
        self._de_escalate_count: int = 0
        self._storm_entry_ts: float = 0.0  # precision-time
        self._halt_entry_ts: float = 0.0  # precision-time

    def update_pnl(self, pnl: int) -> None:
        self.pnl_drawdown = pnl
        self._transition()

    def _transition(self) -> None:
        old_state = self.state
        # Determine target state from PnL thresholds
        if self.pnl_drawdown <= self.halt:
            target_state = StormGuardState.HALT
        elif self.pnl_drawdown <= self.storm:
            target_state = StormGuardState.STORM
        elif self.pnl_drawdown <= self.warm:
            target_state = StormGuardState.WARM
        else:
            target_state = StormGuardState.NORMAL

        now = time.monotonic()
        if target_state > old_state:
            # Escalation: always instant (safety-first)
            self._de_escalate_count = 0
            if target_state >= StormGuardState.STORM and old_state < StormGuardState.STORM:
                self._storm_entry_ts = now
            if target_state == StormGuardState.HALT:
                self._halt_entry_ts = now
            self.state = target_state
        elif target_state < old_state:
            # De-escalation from any elevated state requires cooldown + N consecutive clears
            if old_state == StormGuardState.HALT:
                cooldown_ok = (now - self._halt_entry_ts) >= self._halt_cooldown_s
            elif old_state >= StormGuardState.STORM:
                cooldown_ok = (now - self._storm_entry_ts) >= self._storm_cooldown_s
            else:
                cooldown_ok = True

            if cooldown_ok:
                self._de_escalate_count += 1
                if self._de_escalate_count >= self._de_escalate_threshold:
                    self._de_escalate_count = 0
                    self.state = target_state
            else:
                self._de_escalate_count = 0
        else:
            if target_state >= StormGuardState.STORM:
                self._de_escalate_count = 0

        if self.state != old_state:
            logger.warning("StormGuard Transition", old=old_state, new=self.state, pnl=self.pnl_drawdown)
            # Update Gauge (Global or per strategy if FSM is per strategy - assuming global here)
            self.metrics.stormguard_mode.labels(strategy="global").set(int(self.state))

    def validate(self, intent: OrderIntent) -> Tuple[bool, str]:
        if self.state == StormGuardState.HALT:
            if intent.intent_type == IntentType.CANCEL:
                return True, "OK"  # Allow cancels in HALT
            return False, "STORMGUARD_HALT"

        if self.state == StormGuardState.STORM:
            # Rejection logic for increasing position could go here.
            # Simplified: Reject all NEW for safety in prototype
            if intent.intent_type == IntentType.NEW:
                return False, "STORMGUARD_STORM_NEW_BLOCKED"

        return True, "OK"
