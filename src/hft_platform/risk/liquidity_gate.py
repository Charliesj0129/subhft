"""Liquidity gate validator: rejects new orders when spread is abnormally wide."""
from __future__ import annotations
from typing import Any, Tuple
from structlog import get_logger
from hft_platform.contracts.strategy import IntentType, OrderIntent
from hft_platform.risk.validators import RiskValidator
from hft_platform.core import timebase

logger = get_logger("risk.liquidity_gate")


class LiquidityGateValidator(RiskValidator):
    __slots__ = (
        "_spread_reject_scaled", "_spread_warn_scaled",
        "_cooldown_ns", "_gate_start_offset_ns",
        "_lob", "_last_reject_ns", "_gate_active",
    )

    def __init__(self, config: dict, price_scale_provider: Any,
                 lob: Any = None, tick_size_scaled: int = 10000) -> None:
        super().__init__(config, price_scale_provider)
        gate_cfg = config.get("liquidity_gate", {})
        reject_ticks = int(gate_cfg.get("spread_reject_ticks", 3))
        warn_ticks = int(gate_cfg.get("spread_warn_ticks", 2))
        self._spread_reject_scaled = reject_ticks * tick_size_scaled
        self._spread_warn_scaled = warn_ticks * tick_size_scaled
        self._cooldown_ns = int(gate_cfg.get("cooldown_s", 5)) * 1_000_000_000
        self._gate_start_offset_ns = int(gate_cfg.get("gate_start_offset_s", 60)) * 1_000_000_000
        self._lob = lob
        self._last_reject_ns: int = 0
        self._gate_active: bool = False

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT):
            return True, "OK"
        if self._lob is None:
            return True, "OK"
        stats = getattr(self._lob, "last_stats", None)
        if stats is None:
            return True, "OK"
        if not self._gate_active:
            return True, "OK"
        spread = getattr(stats, "spread_scaled", 0)
        if spread > self._spread_reject_scaled:
            now_ns = timebase.now_ns()
            if now_ns - self._last_reject_ns < self._cooldown_ns:
                return False, "SPREAD_TOO_WIDE_COOLDOWN"
            self._last_reject_ns = now_ns
            logger.warning("Liquidity gate: spread too wide",
                symbol=intent.symbol, spread_scaled=spread,
                threshold=self._spread_reject_scaled)
            return False, f"SPREAD_TOO_WIDE: {spread} > {self._spread_reject_scaled}"
        if spread > self._spread_warn_scaled:
            logger.info("Liquidity gate: spread warning",
                symbol=intent.symbol, spread_scaled=spread)
        return True, "OK"
