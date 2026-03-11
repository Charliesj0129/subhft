from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class BrokerExecFieldMap:
    """Maps broker-specific execution callback field names to canonical names.

    Used by ExecutionNormalizer to resolve fields from raw broker callbacks
    without hardcoding broker-specific names.
    """

    # Order identification fields (tried in order until one resolves)
    order_id_fields: tuple[str, ...] = ("ordno", "ord_no", "seqno", "seq_no")

    # Field containing strategy/user-defined ID
    strategy_id_field: str = "custom_field"

    # Action/side value sets
    action_buy_values: frozenset[str] = frozenset({"Buy"})
    action_sell_values: frozenset[str] = frozenset({"Sell"})
    action_field: str = "action"

    # Price and quantity fields
    price_field: str = "price"
    quantity_field: str = "quantity"

    # Symbol resolution path (dot-separated or tuple for nested access)
    symbol_path: tuple[str, ...] = ("contract", "code")

    # Order status field
    status_field: str = "status"

    # Deal/fill specific fields
    deal_quantity_field: str = "quantity"
    deal_price_field: str = "price"

    def resolve_order_id(self, data: dict[str, object]) -> str:
        """Resolve order ID from raw callback data, trying fields in order."""
        for field in self.order_id_fields:
            val = data.get(field)
            if val is not None and str(val).strip():
                return str(val)
        return ""

    def resolve_strategy_id(self, data: dict[str, object]) -> str:
        """Resolve strategy ID from raw callback data."""
        val = data.get(self.strategy_id_field)
        return str(val) if val is not None else ""

    def resolve_symbol(self, data: dict[str, object]) -> str:
        """Resolve symbol from nested path in raw callback data."""
        current: object = data
        for key in self.symbol_path:
            if isinstance(current, dict):
                current = current.get(key)
            elif hasattr(current, key):
                current = getattr(current, key)
            else:
                return ""
        return str(current) if current is not None else ""

    def is_buy(self, data: dict[str, object]) -> bool:
        """Check if action represents a buy."""
        action = str(data.get(self.action_field, ""))
        return action in self.action_buy_values

    def is_sell(self, data: dict[str, object]) -> bool:
        """Check if action represents a sell."""
        action = str(data.get(self.action_field, ""))
        return action in self.action_sell_values


# Pre-built field maps for supported brokers

SHIOAJI_FIELD_MAP = BrokerExecFieldMap()  # Defaults match Shioaji

FUBON_FIELD_MAP = BrokerExecFieldMap(
    order_id_fields=("ord_no", "order_id", "seq_no"),
    strategy_id_field="user_def",
    action_buy_values=frozenset({"B", "Buy"}),
    action_sell_values=frozenset({"S", "Sell"}),
    action_field="buy_sell",
    price_field="price",
    quantity_field="qty",
    symbol_path=("stock_no",),
    status_field="status",
    deal_quantity_field="mat_qty",
    deal_price_field="mat_price",
)
