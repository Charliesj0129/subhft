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

    Intraday PnL watermark extension
    ---------------------------------
    In addition to the hard daily-loss limit, this validator implements two
    complementary soft-limit mechanisms based on an intraday PnL watermark:

    1. **Absolute soft limit** – rejects new orders when total PnL falls below
       ``intraday_soft_limit_scaled`` (default −500_000, i.e. −500 NTD for TMF
       where 1 NTD = 1_000 scaled).  Once triggered, the soft limit stays active
       for ``soft_limit_cooldown_s`` seconds (default 60 s) even if PnL recovers.

    2. **Peak-drawdown guard** – if the session peak PnL exceeded
       ``peak_pnl_min_scaled`` (default 200_000 = +200 NTD) AND the current PnL
       has drawn down by more than ``peak_drawdown_pct`` (default 40 %) from that
       peak, the soft limit is also triggered.

    Hard limit behaviour (``halt_triggered``)
    -------------------------------------------
    When total PnL ≤ −``max_daily_loss`` the validator latches ``halt_triggered``
    to True.  This flag is *sticky* – it survives PnL recovery and is only cleared
    by an explicit call to ``_force_reset()``.

    Reset boundary: 05:00 Taiwan Standard Time (UTC+8) = 21:00 UTC of the
    *previous* calendar day.  This aligns with Taiwan futures settlement.

    Prices are scaled int x10000 per platform conventions.
    Uses timebase.now_ns() for time — never datetime.now().
    """

    __slots__ = (
        "_default_max_daily_loss",
        "_default_soft_limit_scaled",
        "_default_peak_pnl_min_scaled",
        "_default_peak_drawdown_pct",
        "_default_soft_limit_cooldown_ns",
        "_accumulated_loss",
        "_current_reset_boundary_ns",
        "_unrealized_pnl",
        "_halt_triggered",
        "_halted_strategies",
        "soft_limit_active",
        "_peak_pnl_scaled",
        "_soft_limit_cooldown_until_ns",
    )

    @property
    def halt_triggered(self) -> bool:  # type: ignore[override]
        """True when at least one strategy has hit the hard daily-loss limit."""
        return self._halt_triggered

    @halt_triggered.setter
    def halt_triggered(self, value: bool) -> None:
        """Setting halt_triggered=False also clears all per-strategy latches."""
        self._halt_triggered = value
        if not value:
            self._halted_strategies.clear()

    # Nanoseconds per calendar day
    _NS_PER_DAY: int = 86_400 * 1_000_000_000
    # 05:00 Taiwan (UTC+8) = 21:00 UTC = 21 * 3600 seconds into the UTC day
    _RESET_OFFSET_NS: int = 21 * 3600 * 1_000_000_000
    # Nanoseconds per second
    _NS_PER_S: int = 1_000_000_000

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Stored as a positive threshold; loss is compared as abs value
        self._default_max_daily_loss: int = int(self.defaults.get("max_daily_loss", 500_000_000))

        # Intraday PnL watermark config — read from the ``intraday_pnl`` config section.
        # If absent, the soft-limit and peak-drawdown features are disabled (thresholds
        # are set to sentinel values that can never be reached).
        intraday_cfg: Dict[str, Any] = self.config.get("intraday_pnl", {})

        if intraday_cfg:
            # Convert NTD-based thresholds to scaled-int using the configured conversion.
            price_scale: int = int(intraday_cfg.get("price_scale", 10_000))
            point_value: int = int(intraday_cfg.get("point_value", 10))
            # 1 NTD = price_scale / point_value scaled units
            ntd_to_scaled: int = price_scale // point_value  # integer division

            soft_limit_ntd: int = int(intraday_cfg.get("soft_limit_ntd", 500))
            self._default_soft_limit_scaled = -(soft_limit_ntd * ntd_to_scaled)

            peak_min_ntd: int = int(intraday_cfg.get("peak_drawdown_min_peak_ntd", 200))
            self._default_peak_pnl_min_scaled = peak_min_ntd * ntd_to_scaled

            self._default_peak_drawdown_pct = float(intraday_cfg.get("peak_drawdown_pct", 0.40))
            cooldown_s: int = int(intraday_cfg.get("soft_limit_cooldown_s", 60))
            self._default_soft_limit_cooldown_ns = cooldown_s * self._NS_PER_S
        else:
            # Disabled: soft limit at an unreachably low value; peak min at a
            # value that can never be exceeded in practice.
            self._default_soft_limit_scaled = -(10 ** 18)
            self._default_peak_pnl_min_scaled = 10 ** 18
            self._default_peak_drawdown_pct = 1.0
            self._default_soft_limit_cooldown_ns = 60 * self._NS_PER_S
        # Accumulated realized PnL per strategy (negative = loss, positive = gain); scaled int
        self._accumulated_loss: Dict[str, int] = {}
        # Epoch-ns of the last 21:00 UTC reset boundary (cached)
        self._current_reset_boundary_ns: int = self._current_boundary_ns()
        # Platform-wide unrealized PnL (scaled int, updated externally)
        self._unrealized_pnl: int = 0
        # Set to True when ANY strategy's hard limit is breached; cleared only by _force_reset()
        self._halt_triggered: bool = False
        # Per-strategy halt latch: strategies that have hit the hard limit (sticky)
        self._halted_strategies: set = set()
        # Soft limit flag: True while the soft limit is active (including cooldown)
        self.soft_limit_active: bool = False
        # Highest total PnL seen this session (watermark); used for drawdown guard
        self._peak_pnl_scaled: int = 0
        # Epoch-ns at which the soft limit cooldown expires (0 = not in cooldown)
        self._soft_limit_cooldown_until_ns: int = 0

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
            self._halt_triggered = False
            self._halted_strategies.clear()
            self.soft_limit_active = False
            self._peak_pnl_scaled = 0
            self._soft_limit_cooldown_until_ns = 0
            self._current_reset_boundary_ns = boundary_ns

    def _force_reset(self) -> None:
        """Unconditionally clear all accumulated state (e.g. for testing or manual override)."""
        self._accumulated_loss.clear()
        self._unrealized_pnl = 0
        self._halt_triggered = False
        self._halted_strategies.clear()
        self.soft_limit_active = False
        self._peak_pnl_scaled = 0
        self._soft_limit_cooldown_until_ns = 0
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
        # Update the session peak watermark using all accumulated strategies
        total_realized = sum(self._accumulated_loss.values()) + self._unrealized_pnl
        if total_realized > self._peak_pnl_scaled:
            self._peak_pnl_scaled = total_realized

    def _evaluate_soft_limit(self, total_pnl: int) -> Tuple[bool, str]:
        """Evaluate whether the soft limit should be active.

        Updates ``soft_limit_active`` and ``_soft_limit_cooldown_until_ns`` as a
        side effect.

        Returns (active, reason) where active=True means new orders should be blocked.
        reason is a short tag string identifying the trigger cause.
        """
        now_ns = timebase.now_ns()

        # Check whether the cooldown window has expired — if so, tentatively clear
        if self.soft_limit_active and now_ns >= self._soft_limit_cooldown_until_ns:
            # Re-evaluate: only clear if PnL has actually recovered above the threshold
            if total_pnl > self._default_soft_limit_scaled:
                # Also verify the peak-drawdown condition is no longer met
                if not self._peak_drawdown_triggered(total_pnl):
                    self.soft_limit_active = False

        # Check absolute soft limit
        if total_pnl <= self._default_soft_limit_scaled:
            if not self.soft_limit_active:
                self.soft_limit_active = True
                self._soft_limit_cooldown_until_ns = now_ns + self._default_soft_limit_cooldown_ns
                logger.warning(
                    "DailyLossLimitValidator: soft limit triggered",
                    total_pnl=total_pnl,
                    threshold=self._default_soft_limit_scaled,
                )
            return True, f"SOFT_LIMIT: total_pnl={total_pnl}"

        # Check peak-drawdown guard
        if self._peak_drawdown_triggered(total_pnl):
            if not self.soft_limit_active:
                self.soft_limit_active = True
                self._soft_limit_cooldown_until_ns = now_ns + self._default_soft_limit_cooldown_ns
                logger.warning(
                    "DailyLossLimitValidator: peak-drawdown soft limit triggered",
                    total_pnl=total_pnl,
                    peak_pnl=self._peak_pnl_scaled,
                )
            return True, f"PEAK_DRAWDOWN: total_pnl={total_pnl} peak={self._peak_pnl_scaled}"

        if self.soft_limit_active:
            return True, f"SOFT_LIMIT: cooldown active until_ns={self._soft_limit_cooldown_until_ns}"

        return False, ""

    def _peak_drawdown_triggered(self, total_pnl: int) -> bool:
        """Return True if the peak-drawdown guard condition is met."""
        if self._peak_pnl_scaled < self._default_peak_pnl_min_scaled:
            return False
        # drawdown = peak - current (positive when pnl fell from peak)
        drawdown = self._peak_pnl_scaled - total_pnl
        threshold = int(self._peak_pnl_scaled * self._default_peak_drawdown_pct)
        return drawdown >= threshold

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT):
            return True, "OK"

        self._maybe_reset()

        accumulated = self._accumulated_loss.get(intent.strategy_id, 0)
        # Combine realized + unrealized PnL for the total loss picture
        total_pnl = accumulated + self._unrealized_pnl

        # Update peak watermark
        if total_pnl > self._peak_pnl_scaled:
            self._peak_pnl_scaled = total_pnl

        # Per-strategy hard limit check (sticky per strategy)
        if intent.strategy_id in self._halted_strategies:
            return False, "DAILY_LOSS_LIMIT_EXCEEDED: hard limit latched"

        strat_cfg = self.strat_configs.get(intent.strategy_id, {})
        max_daily_loss = int(strat_cfg.get("max_daily_loss", self._default_max_daily_loss))

        if total_pnl < 0:
            loss_magnitude = -total_pnl
            if loss_magnitude >= max_daily_loss:
                self._halted_strategies.add(intent.strategy_id)
                self._halt_triggered = True  # aggregate flag for observability
                logger.warning(
                    "DailyLossLimitValidator: daily loss limit exceeded",
                    strategy_id=intent.strategy_id,
                    accumulated_loss=accumulated,
                    unrealized_pnl=self._unrealized_pnl,
                    total_pnl=total_pnl,
                    max_daily_loss=max_daily_loss,
                )
                return False, f"DAILY_LOSS_LIMIT_EXCEEDED: loss={loss_magnitude} >= limit={max_daily_loss}"

        # Soft limit / watermark check
        soft_active, soft_reason = self._evaluate_soft_limit(total_pnl)
        if soft_active:
            return False, f"INTRADAY_SOFT_LIMIT_ACTIVE: {soft_reason}"

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
