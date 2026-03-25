"""Per-contract fee calculator for Taiwan futures."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

from hft_platform.contracts.strategy import Side
from hft_platform.tca.types import FeeBreakdown, FeeSchedule

logger = structlog.get_logger(__name__)

_STOCK_FUTURES_SUFFIX = "F"


class FeeCalculator:
    """Calculate per-trade fees from fee_schedules.yaml.

    All monetary outputs are in NTD scaled x10000.
    """

    __slots__ = ("_schedules", "_overrides", "_stock_default")

    def __init__(self, fee_config: dict[str, Any]) -> None:
        futures = fee_config.get("futures", {})
        self._overrides: dict[str, int] = {}
        self._stock_default: FeeSchedule | None = None
        self._schedules: dict[str, FeeSchedule] = {}

        for key, val in futures.items():
            if key == "overrides":
                for sym, ovr in val.items():
                    self._overrides[sym] = ovr.get("commission_per_contract", 0)
            elif key == "stock_futures_default":
                self._stock_default = FeeSchedule(
                    symbol="stock_futures_default",
                    commission_per_contract=val["commission_per_contract"],
                    tax_rate_bps=val["tax_rate_bps"],
                    tax_side=val.get("tax_side", "sell"),
                    tick_size=val.get("tick_size", 0.01),
                    point_value=val["point_value"],
                )
            else:
                self._schedules[key] = FeeSchedule(
                    symbol=key,
                    commission_per_contract=val["commission_per_contract"],
                    tax_rate_bps=val["tax_rate_bps"],
                    tax_side=val.get("tax_side", "sell"),
                    tick_size=val.get("tick_size", 1),
                    point_value=val["point_value"],
                )

    @classmethod
    def from_yaml(cls, path: str | Path) -> FeeCalculator:
        with open(path) as f:
            config = yaml.safe_load(f)
        return cls(config)

    def _resolve(self, symbol: str) -> FeeSchedule:
        if symbol in self._schedules:
            return self._schedules[symbol]
        if symbol.endswith(_STOCK_FUTURES_SUFFIX) and self._stock_default is not None:
            sched = self._stock_default
            if symbol in self._overrides:
                sched = FeeSchedule(
                    symbol=symbol,
                    commission_per_contract=self._overrides[symbol],
                    tax_rate_bps=sched.tax_rate_bps,
                    tax_side=sched.tax_side,
                    tick_size=sched.tick_size,
                    point_value=sched.point_value,
                )
            return sched
        raise KeyError(f"No fee schedule for symbol: {symbol}")

    def calculate(
        self,
        symbol: str,
        side: Side,
        qty: int,
        fill_price: int,
    ) -> FeeBreakdown:
        sched = self._resolve(symbol)
        commission_scaled = sched.commission_per_contract * qty * 10_000
        tax_scaled = 0
        if side == Side.SELL and sched.tax_side == "sell":
            notional_ntd = (fill_price / 10_000) * sched.point_value * qty
            tax_ntd = notional_ntd * (sched.tax_rate_bps / 10_000)
            tax_scaled = int(tax_ntd * 10_000)
        return FeeBreakdown(
            commission=commission_scaled,
            tax=tax_scaled,
            total=commission_scaled + tax_scaled,
        )
