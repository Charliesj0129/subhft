from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

_VALID_SIDES = frozenset(("buy", "sell"))


def _descale_price(scaled: int, price_scale: int) -> float:
    """Descale an integer price to float using Decimal to avoid precision loss."""
    if price_scale <= 0:
        return float(scaled)
    return float(Decimal(scaled) / Decimal(price_scale))


@dataclass(slots=True, frozen=True)
class TranslatedOrder:
    """Broker-agnostic order representation after translation."""

    action: str  # Broker-specific action string
    price: float  # Descaled price for broker API
    quantity: int
    order_type: str  # Broker-specific order type (e.g., "LMT", "MKT")
    time_in_force: str  # Broker-specific TIF (e.g., "ROD", "IOC", "FOK")
    symbol: str
    custom_field: str  # Strategy ID or user-defined field
    extra: dict[str, Any]  # Broker-specific additional fields


class BrokerOrderTranslator(ABC):
    """Translates platform OrderIntent to broker-specific order params."""

    @abstractmethod
    def translate_new_order(
        self,
        symbol: str,
        side: str,  # "buy" | "sell"
        price: int,  # Scaled price (x10000)
        quantity: int,
        tif: str,  # "ROD" | "IOC" | "FOK"
        strategy_id: str,
        price_scale: int,
    ) -> TranslatedOrder:
        """Translate a new order intent to broker format."""
        ...

    @abstractmethod
    def translate_cancel(self, trade_ref: Any) -> dict[str, Any]:
        """Translate a cancel request."""
        ...

    @abstractmethod
    def translate_amend(
        self,
        trade_ref: Any,
        new_price: int | None,
        new_qty: int | None,
        price_scale: int,
    ) -> dict[str, Any]:
        """Translate an amend/update request."""
        ...

    def validate_pre_submit(self, symbol: str, side: str, price: int, quantity: int) -> tuple[bool, str]:
        """Validate order before submission. Returns (is_valid, reason)."""
        if quantity <= 0:
            return False, "quantity must be positive"
        if price <= 0:
            return False, "price must be positive"
        if side not in _VALID_SIDES:
            return False, f"invalid side: {side}"
        return True, ""

    @abstractmethod
    def max_custom_field_len(self) -> int:
        """Maximum length for custom/strategy ID field."""
        ...


class ShioajiOrderTranslator(BrokerOrderTranslator):
    """Order translator for Shioaji."""

    def translate_new_order(self, symbol, side, price, quantity, tif, strategy_id, price_scale):
        action = "Buy" if side == "buy" else "Sell"
        return TranslatedOrder(
            action=action,
            price=_descale_price(price, price_scale),
            quantity=quantity,
            order_type="LMT",
            time_in_force=tif,
            symbol=symbol,
            custom_field=strategy_id[: self.max_custom_field_len()],
            extra={},
        )

    def translate_cancel(self, trade_ref):
        return {"trade": trade_ref}

    def translate_amend(self, trade_ref, new_price, new_qty, price_scale):
        result: dict[str, Any] = {"trade": trade_ref}
        if new_price is not None:
            result["price"] = _descale_price(new_price, price_scale)
        if new_qty is not None:
            result["qty"] = new_qty
        return result

    def max_custom_field_len(self) -> int:
        return 6


class FubonOrderTranslator(BrokerOrderTranslator):
    """Order translator for Fubon."""

    def translate_new_order(self, symbol, side, price, quantity, tif, strategy_id, price_scale):
        action = "B" if side == "buy" else "S"
        truncated_id = strategy_id[: self.max_custom_field_len()]
        return TranslatedOrder(
            action=action,
            price=_descale_price(price, price_scale),
            quantity=quantity,
            order_type="L",
            time_in_force=tif,
            symbol=symbol,
            custom_field=truncated_id,
            extra={"user_def": truncated_id},
        )

    def translate_cancel(self, trade_ref):
        return {"order_id": trade_ref}

    def translate_amend(self, trade_ref, new_price, new_qty, price_scale):
        result: dict[str, Any] = {"order_id": trade_ref}
        if new_price is not None:
            result["price"] = _descale_price(new_price, price_scale)
        if new_qty is not None:
            result["quantity"] = new_qty
        return result

    def max_custom_field_len(self) -> int:
        return 32
