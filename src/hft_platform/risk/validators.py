import os
import time
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, StormGuardState
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

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        """Return (Approved, Reason)."""
        return True, "OK"


class PriceBandValidator(RiskValidator):
    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        if intent.price <= 0:
            return False, "PRICE_ZERO_OR_NEG"

        # Fat Finger Protection: Absolute price cap
        max_price_raw = self.defaults.get("max_price_cap", 5000.0)
        scale = self.price_codec.scale_factor(intent.symbol)
        max_price_scaled = int(max_price_raw * scale)

        if intent.price > max_price_scaled:
            return False, f"PRICE_EXCEEDS_CAP: {intent.price} > {max_price_scaled}"

        # LOB-relative price band validation
        if self.lob is not None:
            mid_price = self._get_mid_price(intent.symbol)
            if mid_price is not None and mid_price > 0:
                strat_cfg = self.strat_configs.get(intent.strategy_id, {})
                band_ticks = strat_cfg.get("price_band_ticks", self.defaults.get("price_band_ticks", 20))
                tick_size_raw = self.defaults.get("tick_size", 0.01)
                tick_size_scaled = int(tick_size_raw * scale)

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
    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        strat_cfg = self.strat_configs.get(intent.strategy_id, {})
        max_notional_raw = strat_cfg.get("max_notional", self.defaults.get("max_notional", 10_000_000))
        scale = self.price_codec.scale_factor(intent.symbol)
        max_notional_scaled = int(max_notional_raw * scale)

        notional_scaled = intent.price * intent.qty
        if notional_scaled > max_notional_scaled:
            return False, f"MAX_NOTIONAL_EXCEEDED: {notional_scaled} > {max_notional_scaled}"

        return True, "OK"


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
        self._storm_cooldown_s: float = float(os.getenv("HFT_STORMGUARD_STORM_COOLDOWN_S", "30"))
        self._de_escalate_threshold: int = int(os.getenv("HFT_STORMGUARD_DE_ESCALATE_N", "5"))
        self._de_escalate_count: int = 0
        self._storm_entry_ts: float = 0.0

    def update_pnl(self, pnl: int):
        self.pnl_drawdown = pnl
        self._transition()

    def _transition(self):
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
            self.state = target_state
        elif target_state < old_state:
            # De-escalation: requires (a) cooldown elapsed AND (b) N consecutive clear evals
            cooldown_ok = (
                (now - self._storm_entry_ts) >= self._storm_cooldown_s if old_state >= StormGuardState.STORM else True
            )
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
