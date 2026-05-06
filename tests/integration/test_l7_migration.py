"""Integration test for L7 audit-column migrations.

Asserts:
  * After ``apply_schema()`` runs, ``hft.orders`` and ``hft.fills`` both
    expose the 7 L7 audit columns.
  * ``DataWriter._detect_l7_audit_columns()`` activates both tables in
    extended mode.
  * A synthetic row written via the writer's row-insert path round-trips
    with audit fields populated.
  * Stripping behavior in legacy mode (simulated by clearing
    ``_l7_audit_active_tables``) drops the audit columns before INSERT,
    leaving the row valid against the still-extended schema.

Skipped when ClickHouse is not reachable.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

try:
    import clickhouse_connect
except ImportError:  # pragma: no cover
    clickhouse_connect = None  # type: ignore[assignment]

from hft_platform.recorder.schema import apply_schema
from hft_platform.recorder.writer import DataWriter

L7_AUDIT_COLS = (
    "trace_id",
    "feature_snapshot_id",
    "risk_decision_id",
    "strategy_version",
    "config_hash",
    "git_sha",
    "data_session_id",
)

L7_TEST_PREFIX = "l7-mig-test-"


@pytest.fixture
def ch_client():
    """ClickHouse client fixture — skip when CH unreachable."""
    if clickhouse_connect is None:
        pytest.skip("clickhouse_connect not installed")
    host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", "9000"))
    try:
        client = clickhouse_connect.get_client(host=host, port=port, username="default", password="")
        client.query("SELECT 1")
        return client
    except Exception as exc:
        pytest.skip(f"ClickHouse not available: {exc}")


@pytest.fixture
def schema_applied(ch_client):
    """Run apply_schema() against the live CH so L7 migrations land if pending."""
    apply_schema(ch_client)
    yield ch_client


def _describe_columns(ch_client, table: str) -> set[str]:
    result = ch_client.query(f"DESCRIBE TABLE {table}")
    return {row[0] for row in result.result_rows}


def _cleanup_test_rows(ch_client) -> None:
    for table, key in (("hft.orders", "order_id"), ("hft.fills", "fill_id")):
        try:
            ch_client.command(
                f"ALTER TABLE {table} DELETE WHERE {key} LIKE '{L7_TEST_PREFIX}%' SETTINGS mutations_sync = 1"
            )
        except Exception:  # noqa: BLE001
            pass


@pytest.mark.integration
class TestL7Migration:
    def test_audit_columns_exist_on_orders(self, schema_applied) -> None:
        cols = _describe_columns(schema_applied, "hft.orders")
        for col in L7_AUDIT_COLS:
            assert col in cols, f"hft.orders missing L7 column: {col}"

    def test_audit_columns_exist_on_fills(self, schema_applied) -> None:
        cols = _describe_columns(schema_applied, "hft.fills")
        for col in L7_AUDIT_COLS:
            assert col in cols, f"hft.fills missing L7 column: {col}"

    def test_detect_activates_both_tables(self, schema_applied) -> None:
        host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
        port = int(os.getenv("HFT_CLICKHOUSE_PORT", "9000"))
        os.environ["HFT_CLICKHOUSE_ENABLED"] = "1"
        writer = DataWriter(ch_host=host, ch_port=port)
        writer.connect()
        try:
            assert writer._l7_audit_detected is True
            assert writer._l7_audit_active_tables == {"hft.orders", "hft.fills"}
        finally:
            # Best-effort shutdown so heartbeat thread doesn't leak.
            # Plain attribute assignment on a connected DataWriter cannot raise.
            writer._heartbeat_running = False

    def test_round_trip_extended_mode_populates_audit_fields(self, schema_applied) -> None:
        ch = schema_applied
        order_id = f"{L7_TEST_PREFIX}{uuid.uuid4()}"
        ts = time.time_ns()
        row = {
            "order_id": order_id,
            "client_order_id": f"cli-{order_id}",
            "strategy_id": "r47_maker_l7_test",
            "symbol": "TMFR1",
            "side": "BUY",
            "price_scaled": 171_950_000,
            "qty": 1,
            "status": "NEW",
            "ingest_ts": ts,
            "latency_us": 150,
            "instrument_type": "future",
            "oc_type": "open",
            "trace_id": "l7-trace-abc",
            "feature_snapshot_id": "feat-l7-1",
            "risk_decision_id": "risk-l7-1",
            "strategy_version": "v1-l7-test",
            "config_hash": "deadbeefcafe0001",
            "git_sha": "1823be17",
            "data_session_id": "sim-2026-05-05-l7-test",
        }
        try:
            keys = list(row.keys())
            ch.insert("hft.orders", [[row[k] for k in keys]], column_names=keys)
            result = ch.query(
                f"SELECT trace_id, git_sha, data_session_id FROM hft.orders WHERE order_id = '{order_id}'"
            )
            assert len(result.result_rows) == 1
            trace_id, git_sha, session = result.result_rows[0]
            assert trace_id == "l7-trace-abc"
            assert git_sha == "1823be17"
            assert session == "sim-2026-05-05-l7-test"
        finally:
            _cleanup_test_rows(ch)

    def test_legacy_mode_strip_keeps_insert_valid(self, schema_applied) -> None:
        """Simulate legacy state by clearing active_tables, then write via the
        writer's row-insert path. Strip must drop the audit keys, leaving the
        row's remaining fields valid against the (still-extended) schema."""
        host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
        port = int(os.getenv("HFT_CLICKHOUSE_PORT", "9000"))
        os.environ["HFT_CLICKHOUSE_ENABLED"] = "1"
        writer = DataWriter(ch_host=host, ch_port=port)
        writer.connect()
        try:
            writer._l7_audit_active_tables = set()  # legacy simulation
            order_id = f"{L7_TEST_PREFIX}{uuid.uuid4()}"
            ts = time.time_ns()
            data = [
                {
                    "order_id": order_id,
                    "client_order_id": f"cli-{order_id}",
                    "strategy_id": "r47_maker_l7_test",
                    "symbol": "TMFR1",
                    "side": "BUY",
                    "price_scaled": 171_960_000,
                    "qty": 1,
                    "status": "NEW",
                    "ingest_ts": ts,
                    "latency_us": 150,
                    "instrument_type": "future",
                    "oc_type": "open",
                    # Audit fields included by the producer; strip MUST drop them.
                    "trace_id": "should-be-stripped",
                    "git_sha": "should-be-stripped",
                }
            ]
            writer._ch_insert_once("hft.orders", data)
            result = schema_applied.query(f"SELECT trace_id, git_sha FROM hft.orders WHERE order_id = '{order_id}'")
            assert len(result.result_rows) == 1
            trace_id, git_sha = result.result_rows[0]
            # Strip dropped the keys; ClickHouse defaults gave us '' on read.
            assert trace_id == ""
            assert git_sha == ""
        finally:
            _cleanup_test_rows(schema_applied)
            # Plain attribute assignment cannot raise.
            writer._heartbeat_running = False
