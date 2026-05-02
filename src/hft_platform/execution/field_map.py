"""Broker-specific execution-payload field name resolution.

Execution payloads from Shioaji / Fubon / future brokers carry the same semantic
fields (order id, sequence id, filled qty, etc.) under different key names.
`ExecutionNormalizer` consumes these payloads and previously hardcoded Shioaji
field names everywhere — MB-04 violation (`.agent/rules/26-multi-broker-governance.md`).

This module introduces a `BrokerExecFieldMap` protocol plus concrete impls so
the normalizer can resolve fields without broker-specific imports. Each tuple
lists accepted aliases in priority order; the first non-empty value wins.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class BrokerExecFieldMap(Protocol):
    """Per-broker execution payload key resolution.

    Implementations return tuples of candidate field names ordered by priority.
    The normalizer walks each tuple and selects the first non-empty value.
    """

    name: str

    def order_id_keys(self) -> tuple[str, ...]: ...
    def sequence_id_keys(self) -> tuple[str, ...]: ...
    def other_id_keys(self) -> tuple[str, ...]: ...
    def custom_field_keys(self) -> tuple[str, ...]: ...
    def symbol_keys(self) -> tuple[str, ...]: ...
    def filled_qty_keys(self) -> tuple[str, ...]: ...
    def submitted_qty_keys(self) -> tuple[str, ...]: ...
    def operation_type_keys(self) -> tuple[str, ...]: ...
    def operation_code_keys(self) -> tuple[str, ...]: ...


class _BaseFieldMap:
    """Default Shioaji-compatible field names (preserves legacy behavior)."""

    name = "shioaji"

    def order_id_keys(self) -> tuple[str, ...]:
        return ("ordno", "ord_no")

    def sequence_id_keys(self) -> tuple[str, ...]:
        return ("seqno", "seq_no")

    def other_id_keys(self) -> tuple[str, ...]:
        return ("id", "order_id")

    def custom_field_keys(self) -> tuple[str, ...]:
        return ("custom_field",)

    def symbol_keys(self) -> tuple[str, ...]:
        return ("full_code", "code")

    def filled_qty_keys(self) -> tuple[str, ...]:
        return ("deal_quantity", "cum_qty")

    def submitted_qty_keys(self) -> tuple[str, ...]:
        return ("quantity", "qty", "volume")

    def operation_type_keys(self) -> tuple[str, ...]:
        return ("op_type",)

    def operation_code_keys(self) -> tuple[str, ...]:
        return ("op_code",)


class ShioajiExecFieldMap(_BaseFieldMap):
    """Shioaji SDK field names (default)."""

    name = "shioaji"


class FubonExecFieldMap(_BaseFieldMap):
    """Fubon SDK field names. Overrides diverging keys; inherits shared ones.

    Fubon payload conventions (per `feed_adapter/fubon/` observed shapes):
    - order id: `order_no` / `orderNo`
    - sequence: `seq_no` (same as Shioaji legacy alias)
    - filled qty: `filled_qty` / `matched_qty`
    - submitted qty: `quantity`
    - symbol: `symbol` (direct), no `full_code`
    """

    name = "fubon"

    def order_id_keys(self) -> tuple[str, ...]:
        return ("order_no", "orderNo", "ordno", "ord_no")

    def sequence_id_keys(self) -> tuple[str, ...]:
        return ("seq_no", "seqno", "matched_seq")

    def symbol_keys(self) -> tuple[str, ...]:
        return ("symbol", "full_code", "code")

    def filled_qty_keys(self) -> tuple[str, ...]:
        return ("filled_qty", "matched_qty", "deal_quantity", "cum_qty")


def get_field_map(broker: str) -> BrokerExecFieldMap:
    """Return the field map for *broker* name; falls back to Shioaji default."""
    key = (broker or "").strip().lower()
    if key == "fubon":
        return FubonExecFieldMap()
    return ShioajiExecFieldMap()
