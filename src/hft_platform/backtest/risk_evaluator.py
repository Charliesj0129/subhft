"""Synchronous risk evaluator for backtest — reuses live validator classes."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from structlog import get_logger

from hft_platform.contracts.strategy import OrderIntent, RiskDecision
from hft_platform.core.pricing import PriceScaleProvider
from hft_platform.risk.validators import (
    DailyLossLimitValidator,
    MaxNotionalValidator,
    PerSymbolNotionalValidator,
    PositionLimitValidator,
    PriceBandValidator,
    RiskValidator,
)

logger = get_logger("backtest_risk")


@dataclass(frozen=True)
class BacktestRiskConfig:
    """Controls which risk validators are active during backtest."""

    enabled: bool = True
    price_band: bool = True
    max_notional: bool = True
    per_symbol_notional: bool = True
    position_limit: bool = True
    daily_loss_limit: bool = False
    storm_guard: bool = False
    config_path: str = "config/base/strategy_limits.yaml"


class BacktestRiskEvaluator:
    """Synchronous risk evaluation for backtests.

    Reuses live validator classes (PriceBandValidator, MaxNotionalValidator,
    PerSymbolNotionalValidator, PositionLimitValidator, DailyLossLimitValidator)
    so that backtest risk checks are identical to live risk checks.
    """

    __slots__ = ("_validators", "_rejection_count", "_rejection_breakdown", "_enabled")

    def __init__(
        self,
        config: BacktestRiskConfig,
        position_provider: Callable[[str, str], int],
        price_scale_provider: PriceScaleProvider | None = None,
    ) -> None:
        self._enabled: bool = config.enabled
        self._rejection_count: int = 0
        self._rejection_breakdown: dict[str, int] = {}
        self._validators: list[RiskValidator] = []

        if not self._enabled:
            return

        risk_config = self._load_risk_config(config.config_path)

        if config.price_band:
            self._validators.append(PriceBandValidator(risk_config, price_scale_provider))
        if config.max_notional:
            self._validators.append(MaxNotionalValidator(risk_config, price_scale_provider))
        if config.per_symbol_notional:
            self._validators.append(PerSymbolNotionalValidator(risk_config, price_scale_provider))
        if config.position_limit:
            self._validators.append(
                PositionLimitValidator(
                    risk_config,
                    price_scale_provider,
                    position_provider=position_provider,
                )
            )
        if config.daily_loss_limit:
            self._validators.append(DailyLossLimitValidator(risk_config, price_scale_provider))

    def evaluate(self, intent: OrderIntent) -> RiskDecision:
        """Evaluate an OrderIntent synchronously against all active validators.

        Returns RiskDecision(approved=True, ...) on pass,
        RiskDecision(approved=False, ..., reason_code=...) on first failure.
        """
        if not self._enabled:
            return RiskDecision(True, intent)

        price = getattr(intent, "price", None)
        if price is not None and not isinstance(price, int):
            return self._reject(intent, "FLOAT_PRICE")

        for v in self._validators:
            ok, reason = v.check(intent)
            if not ok:
                return self._reject(intent, reason)

        return RiskDecision(True, intent)

    def _reject(self, intent: OrderIntent, reason: str) -> RiskDecision:
        self._rejection_count += 1
        self._rejection_breakdown[reason] = self._rejection_breakdown.get(reason, 0) + 1
        return RiskDecision(False, intent, reason)

    @property
    def rejection_count(self) -> int:
        """Total number of rejected intents since creation."""
        return self._rejection_count

    @property
    def rejection_breakdown(self) -> dict[str, int]:
        """Rejection counts keyed by reason code (immutable copy)."""
        return dict(self._rejection_breakdown)

    @staticmethod
    def _load_risk_config(config_path: str) -> dict[str, Any]:
        p = Path(config_path)
        if not p.exists():
            logger.warning("backtest_risk_config_not_found", path=config_path)
            return {}
        with p.open() as f:
            return yaml.safe_load(f) or {}
