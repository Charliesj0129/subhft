from typing import Any, Dict, Tuple

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, StormGuardState
from hft_platform.core.pricing import PriceCodec, PriceScaleProvider, SymbolMetadataPriceScaleProvider
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("risk_validators")


class RiskValidator:
    def __init__(self, config: Dict[str, Any], price_scale_provider: PriceScaleProvider | None = None):
        self.config = config
        self.defaults = config.get("global_defaults", {})
        self.strat_configs = config.get("strategies", {})
        provider = price_scale_provider or SymbolMetadataPriceScaleProvider()
        self.price_codec = PriceCodec(provider)

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        """Return (Approved, Reason)."""
        return True, "OK"


class PriceBandValidator(RiskValidator):
    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        # Get limits
        # strat_cfg = self.strat_configs.get(intent.strategy_id, {})
        # band_ticks = strat_cfg.get("price_band_ticks", self.defaults.get("price_band_ticks", 20))

        # In real impl, we need access to LOB reference price.
        # For now, we assume simple static bounds or placeholder.
        # Mocking check:
        if intent.price <= 0:
            return False, "PRICE_ZERO_OR_NEG"

        # Fat Finger Protection
        # Reject if price is outrageously high/low compared to standard market prices
        # Assuming price is standard equities, > 10000 or < 0.1 might be wrong (context dependent)
        # Better: if we had a reference price.
        # Without ref price (since RiskEngine doesn't have LOB yet), we can enforce max price cap from config
        # or just simple sanity.
        # But if we assume this is a refinement for "Deep Defects", we should add at least a sanity check.

        max_price_raw = self.defaults.get("max_price_cap", 5000.0)
        scale = self.price_codec.scale_factor(intent.symbol)
        max_price_scaled = int(max_price_raw * scale)

        if intent.price > max_price_scaled:
            return False, f"PRICE_EXCEEDS_CAP: {intent.price} > {max_price_scaled}"

        # TODO: Compare with LOB mid_price +/- band_ticks * tick_size
        return True, "OK"


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

    def update_pnl(self, pnl: int):
        self.pnl_drawdown = pnl
        self._transition()

    def _transition(self):
        old_state = self.state
        if self.pnl_drawdown <= self.halt:
            self.state = StormGuardState.HALT
        elif self.pnl_drawdown <= self.storm:
            self.state = StormGuardState.STORM
        elif self.pnl_drawdown <= self.warm:
            self.state = StormGuardState.WARM
        else:
            self.state = StormGuardState.NORMAL

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
