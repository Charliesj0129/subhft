"""Audit writer persistence wiring tests (P1-a 2026-04-27).

Pins the contract that:
  1. ``AuditWriter.set_writer()`` attaches a ClickHouse writer post-construction
     so the engine can wire the recorder DataWriter once both objects exist.
  2. ``_normalize_row()`` fills missing producer fields with type-appropriate
     defaults and JSON-encodes extra fields into ``details`` (orders_log only),
     so DataWriter's "infer columns from first row" path doesn't silently drop
     fields.
  3. A failed CH write increments ``audit_persist_failures_total`` instead of
     being only structlog-visible.

Background: prior to P1-a, ``services.system._run_internal`` invoked
``get_audit_writer()`` with no writer arg — every audit batch fell through
to the structlog ``audit_fallback`` path. Even if the writer were attached,
the producer payload schemas (RiskEngine / StormGuard / OrderAdapter) did
not match the legacy DDL columns, so every CH INSERT would have failed.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from hft_platform.recorder.audit import (
    _AUDIT_SCHEMA_DEFAULTS,
    AuditWriter,
    _normalize_row,
)

# ---------------------------------------------------------------------------
# set_writer wiring
# ---------------------------------------------------------------------------


class TestSetWriter:
    def test_set_writer_attaches_post_construction(self) -> None:
        """Writer can be attached after __init__ — services.system needs this."""
        audit = AuditWriter(queue_size=10, writer=None)
        assert audit._writer is None

        mock_writer = AsyncMock()
        audit.set_writer(mock_writer)

        assert audit._writer is mock_writer

    def test_set_writer_replaces_existing_writer(self) -> None:
        first = AsyncMock()
        second = AsyncMock()
        audit = AuditWriter(queue_size=10, writer=first)
        assert audit._writer is first

        audit.set_writer(second)
        assert audit._writer is second

    @pytest.mark.asyncio
    async def test_set_writer_then_log_then_flush_reaches_writer(self) -> None:
        """End-to-end: log → start → wait → stop. Writer.write must be called
        at least once with a normalized payload for audit.risk_log."""
        mock_writer = AsyncMock()
        audit = AuditWriter(queue_size=100, flush_interval_ms=10, writer=None)
        audit.set_writer(mock_writer)

        # Simulate a RiskDecision audit emit (fields match RiskEngine producer)
        audit.log_risk_decision(
            {
                "strategy_id": "r47",
                "symbol": "TXFD6",
                "intent_type": 1,
                "price": 1730000,
                "qty": 1,
                "approved": True,
                "reason_code": "OK",
            }
        )

        await audit.start()
        await asyncio.sleep(0.05)
        await audit.stop()

        mock_writer.write.assert_awaited()
        # First positional arg is the table name
        tables = [c.args[0] for c in mock_writer.write.await_args_list]
        assert "audit.risk_log" in tables
        # Locate the risk_log call and inspect its rows
        risk_call = next(c for c in mock_writer.write.await_args_list if c.args[0] == "audit.risk_log")
        rows = risk_call.args[1]
        assert len(rows) >= 1
        row = rows[0]
        # Schema-aligned fields present
        assert row["strategy_id"] == "r47"
        assert row["symbol"] == "TXFD6"
        assert row["approved"] == 1  # bool→UInt8 coercion
        assert "reason_code" in row


# ---------------------------------------------------------------------------
# _normalize_row schema enforcement
# ---------------------------------------------------------------------------


class TestNormalizeRow:
    def test_orders_log_fills_missing_optional_fields(self) -> None:
        """OrderAdapter NEW dispatch row omits target_key/error/new_price.
        Normalizer must fill them with defaults so the batch shape is
        consistent."""
        row = {
            "ts_ns": 123,
            "event": "dispatched",
            "intent_type": "NEW",
            "order_key": "r47:abc",
            "symbol": "TXFD6",
            "side": "BUY",
            "price": 1730000,
            "qty": 1,
            "strategy_id": "r47",
            "cmd_id": 99,
        }
        out = _normalize_row("audit.orders_log", row)
        assert out["target_key"] == ""
        assert out["new_price"] == 0
        assert out["error"] == ""
        assert out["details"] == ""
        # Original fields preserved
        assert out["event"] == "dispatched"
        assert out["price"] == 1730000

    def test_orders_log_extras_go_into_details(self) -> None:
        """Producer emits a key not in DDL (e.g. cancel_outcome) — it must
        flow into ``details`` JSON, not be silently dropped."""
        row = {
            "ts_ns": 123,
            "event": "cancel_no_op_already_inflight",
            "intent_type": "CANCEL",
            "order_key": "r47:def",
            "symbol": "TXFD6",
            "strategy_id": "r47",
            "cmd_id": 100,
            "cancel_outcome": "already_inflight",  # extra
            "extra_metadata": {"foo": "bar"},  # extra, non-string
        }
        out = _normalize_row("audit.orders_log", row)
        assert out["event"] == "cancel_no_op_already_inflight"
        details = json.loads(out["details"])
        assert details["cancel_outcome"] == "already_inflight"
        assert "extra_metadata" in details

    def test_risk_log_drops_extras_not_in_schema(self) -> None:
        """audit.risk_log has no `details` column → extras dropped (logged
        elsewhere if needed). Covers DDL contract."""
        row = {
            "ts_ns": 123,
            "strategy_id": "r47",
            "symbol": "TXFD6",
            "intent_type": 1,
            "price": 1730000,
            "qty": 1,
            "approved": True,
            "reason_code": "OK",
            "extra_field": "should-be-dropped",
        }
        out = _normalize_row("audit.risk_log", row)
        assert "extra_field" not in out
        assert set(out.keys()) == set(_AUDIT_SCHEMA_DEFAULTS["audit.risk_log"].keys())

    def test_normalize_coerces_approved_bool_to_uint8(self) -> None:
        for raw, expected in [(True, 1), (False, 0), (1, 1), (0, 0), (None, 0)]:
            out = _normalize_row(
                "audit.risk_log",
                {
                    "ts_ns": 1,
                    "strategy_id": "x",
                    "symbol": "y",
                    "intent_type": 0,
                    "price": 0,
                    "qty": 0,
                    "approved": raw,
                    "reason_code": "",
                },
            )
            assert out["approved"] == expected, f"approved={raw!r} → {out['approved']}"

    def test_normalize_unknown_table_passthrough(self) -> None:
        row = {"foo": "bar"}
        out = _normalize_row("audit.unknown_table", row)
        assert out is row  # no-op identity


# ---------------------------------------------------------------------------
# persist_failures metric
# ---------------------------------------------------------------------------


class TestPersistFailureMetric:
    @pytest.mark.asyncio
    async def test_writer_exception_increments_persist_failures_metric(self) -> None:
        """When the CH writer raises, audit_persist_failures_total{table,reason}
        must increment exactly once per failed batch flush."""
        from hft_platform.observability.metrics import MetricsRegistry

        metrics = MetricsRegistry.get()
        # Snapshot before
        before_metric = metrics.audit_persist_failures_total.labels(
            table="audit.risk_log", reason="ConnectionError"
        )
        before_val = before_metric._value.get()

        mock_writer = AsyncMock()
        mock_writer.write.side_effect = ConnectionError("CH down")

        audit = AuditWriter(queue_size=100, flush_interval_ms=10, writer=mock_writer)
        audit.log_risk_decision(
            {
                "strategy_id": "r47",
                "symbol": "TXFD6",
                "intent_type": 1,
                "price": 1730000,
                "qty": 1,
                "approved": True,
                "reason_code": "OK",
            }
        )
        await audit.start()
        await asyncio.sleep(0.05)
        await audit.stop()

        after_val = before_metric._value.get()
        assert after_val > before_val, (
            f"audit_persist_failures_total did not increment "
            f"(before={before_val}, after={after_val})"
        )
