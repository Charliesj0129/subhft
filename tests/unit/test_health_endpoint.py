"""Tests for WU-17: Structured Health Endpoint."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from hft_platform.observability.health import DegradationTracker, HealthServer


def _make_mock_system(
    running: bool = True,
    storm_state: int = 0,
    tasks_alive: bool = True,
    md_running: bool = True,
    broker_logged_in: bool = True,
    recorder_healthy: bool = True,
    queue_pressure: float = 0.0,
) -> MagicMock:
    """Create a mock HFTSystem for health checks."""
    from hft_platform.risk.storm_guard import StormGuardState

    system = MagicMock()
    system.running = running

    sg = MagicMock()
    sg.state = StormGuardState(storm_state)
    system.storm_guard = sg

    # Mock broker clients
    md_client = MagicMock()
    md_client.logged_in = broker_logged_in
    system.md_client = md_client

    order_client = MagicMock()
    order_client.logged_in = broker_logged_in
    system.order_client = order_client

    # Mock tasks (include 'risk')
    tasks = {}
    for name in ("md", "strat", "order", "recorder", "risk"):
        t = MagicMock()
        t.done.return_value = not tasks_alive
        tasks[name] = t
    system.tasks = tasks

    # Mock md_service
    system.md_service = MagicMock()
    system.md_service.running = md_running

    # Mock recorder_service
    recorder = MagicMock()
    recorder.healthy = recorder_healthy
    # Remove last_write_ok to avoid MagicMock auto-creating it
    del recorder.last_write_ok
    system.recorder_service = recorder

    # Mock queues with maxsize
    for qname in ("raw_queue", "raw_exec_queue", "risk_queue", "order_queue", "recorder_queue"):
        q = MagicMock()
        q.maxsize = 1000
        q.qsize.return_value = int(1000 * queue_pressure)
        setattr(system, qname, q)

    return system


class TestHealthServerReadiness:
    """Test readiness check logic."""

    def test_ready_when_system_healthy(self) -> None:
        system = _make_mock_system()
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "ready"
        assert checks["system_running"] is True
        assert checks["storm_guard"] == "NORMAL"

    def test_unavailable_when_system_not_running(self) -> None:
        system = _make_mock_system(running=False)
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "unavailable"
        assert "system_not_running" in checks["unavailable_reasons"]

    def test_unavailable_when_storm_halt(self) -> None:
        from hft_platform.risk.storm_guard import StormGuardState

        system = _make_mock_system(storm_state=StormGuardState.HALT)
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "unavailable"
        assert checks["storm_guard"] == "HALT"

    def test_degraded_when_storm_warm(self) -> None:
        from hft_platform.risk.storm_guard import StormGuardState

        system = _make_mock_system(storm_state=StormGuardState.WARM)
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "degraded"
        assert "storm_guard_elevated" in checks["degraded_reasons"]

    def test_degraded_when_storm_storm(self) -> None:
        from hft_platform.risk.storm_guard import StormGuardState

        system = _make_mock_system(storm_state=StormGuardState.STORM)
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "degraded"
        assert "storm_guard_elevated" in checks["degraded_reasons"]

    def test_unavailable_when_tasks_dead(self) -> None:
        system = _make_mock_system(tasks_alive=False)
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "unavailable"
        assert all(v is False for v in checks["tasks"].values())

    def test_unavailable_when_no_system(self) -> None:
        server = HealthServer(system=None)
        status, checks = server._check_readiness()
        assert status == "unavailable"
        assert checks == {"system": "not_attached"}

    def test_unavailable_when_broker_not_logged_in(self) -> None:
        system = _make_mock_system(broker_logged_in=False)
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "unavailable"
        assert "broker_not_logged_in" in checks["unavailable_reasons"]

    def test_degraded_when_feed_disconnected(self) -> None:
        system = _make_mock_system(md_running=False)
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "degraded"
        assert "feed_disconnected" in checks["degraded_reasons"]

    def test_degraded_when_clickhouse_unhealthy(self) -> None:
        system = _make_mock_system(recorder_healthy=False)
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "degraded"
        assert "clickhouse_unhealthy" in checks["degraded_reasons"]

    def test_degraded_when_queue_pressure_high(self) -> None:
        system = _make_mock_system(queue_pressure=0.9)
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "degraded"
        assert "queue_pressure_high" in checks["degraded_reasons"]

    def test_gateway_checked_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_GATEWAY_ENABLED", "1")
        system = _make_mock_system()
        # Gateway task not present → unavailable
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "unavailable"
        assert checks["tasks"].get("gateway") is False

    def test_gateway_not_checked_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_GATEWAY_ENABLED", "0")
        system = _make_mock_system()
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert "gateway" not in checks["tasks"]

    def test_optional_exec_tasks_reported(self) -> None:
        system = _make_mock_system()
        # Add exec_router task
        exec_task = MagicMock()
        exec_task.done.return_value = False
        system.tasks["exec_router"] = exec_task
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "ready"
        assert checks["optional_tasks"]["exec_router"] is True


class TestHealthServerStatus:
    """Test full status response building."""

    def test_status_ready(self) -> None:
        system = _make_mock_system()
        server = HealthServer(system=system)
        status = server._build_status()
        assert status["status"] == "ready"
        assert "uptime_s" in status
        assert "version" in status
        assert "timestamp_ns" in status
        assert "queues" in status
        assert "storm_guard_state" in status
        assert status["storm_guard_state"] == 0

    def test_status_unavailable_when_tasks_dead(self) -> None:
        system = _make_mock_system(tasks_alive=False)
        server = HealthServer(system=system)
        status = server._build_status()
        assert status["status"] == "unavailable"

    def test_status_unavailable_when_no_system(self) -> None:
        server = HealthServer(system=None)
        status = server._build_status()
        assert status["status"] == "unavailable"

    def test_status_degraded_includes_events(self) -> None:
        system = _make_mock_system(md_running=False)
        server = HealthServer(system=system)
        # First call records degradation
        server._build_status()
        status = server._build_status()
        assert status["status"] == "degraded"
        assert "degradation_events" in status
        assert len(status["degradation_events"]) >= 1


class TestDegradationTracker:
    """Test DegradationTracker."""

    def test_record_and_recent(self) -> None:
        tracker = DegradationTracker(max_events=3)
        tracker.record("test1", {"a": 1})
        tracker.record("test2", {"b": 2})
        assert len(tracker.recent) == 2
        assert tracker.recent[0]["reason"] == "test1"
        assert tracker.recent[1]["reason"] == "test2"

    def test_evicts_old_events(self) -> None:
        tracker = DegradationTracker(max_events=2)
        tracker.record("first", {})
        tracker.record("second", {})
        tracker.record("third", {})
        assert len(tracker.recent) == 2
        assert tracker.recent[0]["reason"] == "second"
        assert tracker.recent[1]["reason"] == "third"

    def test_recent_returns_copy(self) -> None:
        tracker = DegradationTracker()
        tracker.record("test", {})
        events = tracker.recent
        events.clear()
        assert len(tracker.recent) == 1

    def test_event_has_timestamp(self) -> None:
        tracker = DegradationTracker()
        tracker.record("test", {"key": "val"})
        event = tracker.recent[0]
        assert "timestamp_ns" in event
        assert isinstance(event["timestamp_ns"], int)


class TestHealthServerHTTP:
    """Test HTTP server handling (integration-style)."""

    @pytest.mark.asyncio
    async def test_healthz_returns_200(self) -> None:
        system = _make_mock_system()
        server = HealthServer(system=system)
        server._port = 0  # Let OS pick a free port

        # Start server
        srv = await asyncio.start_server(server._handle_connection, "127.0.0.1", 0)
        addr = srv.sockets[0].getsockname()
        port = addr[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()
            await writer.wait_closed()

            response_str = response.decode("utf-8")
            assert "200 OK" in response_str
            assert '"status"' in response_str
        finally:
            srv.close()
            await srv.wait_closed()

    @pytest.mark.asyncio
    async def test_readyz_returns_200_when_healthy(self) -> None:
        system = _make_mock_system()
        server = HealthServer(system=system)

        srv = await asyncio.start_server(server._handle_connection, "127.0.0.1", 0)
        addr = srv.sockets[0].getsockname()
        port = addr[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /readyz HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()
            await writer.wait_closed()

            response_str = response.decode("utf-8")
            assert "200 OK" in response_str
            body_start = response_str.index("\r\n\r\n") + 4
            body = json.loads(response_str[body_start:])
            assert body["status"] == "ready"
        finally:
            srv.close()
            await srv.wait_closed()

    @pytest.mark.asyncio
    async def test_readyz_returns_503_when_unavailable(self) -> None:
        system = _make_mock_system(running=False)
        server = HealthServer(system=system)

        srv = await asyncio.start_server(server._handle_connection, "127.0.0.1", 0)
        addr = srv.sockets[0].getsockname()
        port = addr[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /readyz HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()
            await writer.wait_closed()

            response_str = response.decode("utf-8")
            assert "503" in response_str
            body_start = response_str.index("\r\n\r\n") + 4
            body = json.loads(response_str[body_start:])
            assert body["status"] == "unavailable"
        finally:
            srv.close()
            await srv.wait_closed()

    @pytest.mark.asyncio
    async def test_readyz_degraded_returns_200_with_header(self) -> None:
        system = _make_mock_system(md_running=False)
        server = HealthServer(system=system)

        srv = await asyncio.start_server(server._handle_connection, "127.0.0.1", 0)
        addr = srv.sockets[0].getsockname()
        port = addr[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /readyz HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()
            await writer.wait_closed()

            response_str = response.decode("utf-8")
            assert "200 OK" in response_str
            assert "X-Health-Status: degraded" in response_str
            body_start = response_str.index("\r\n\r\n") + 4
            body = json.loads(response_str[body_start:])
            assert body["status"] == "degraded"
        finally:
            srv.close()
            await srv.wait_closed()

    @pytest.mark.asyncio
    async def test_status_endpoint(self) -> None:
        system = _make_mock_system()
        server = HealthServer(system=system)

        srv = await asyncio.start_server(server._handle_connection, "127.0.0.1", 0)
        addr = srv.sockets[0].getsockname()
        port = addr[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /status HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()
            await writer.wait_closed()

            response_str = response.decode("utf-8")
            assert "200 OK" in response_str
            # Parse the JSON body
            body_start = response_str.index("\r\n\r\n") + 4
            body = json.loads(response_str[body_start:])
            assert body["status"] == "ready"
            assert "queues" in body
            assert "checks" in body
        finally:
            srv.close()
            await srv.wait_closed()

    @pytest.mark.asyncio
    async def test_404_for_unknown_path(self) -> None:
        server = HealthServer(system=None)

        srv = await asyncio.start_server(server._handle_connection, "127.0.0.1", 0)
        addr = srv.sockets[0].getsockname()
        port = addr[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /unknown HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()
            await writer.wait_closed()

            assert b"404" in response
        finally:
            srv.close()
            await srv.wait_closed()


class TestHealthServerLifecycle:
    """Test server lifecycle."""

    def test_stop_closes_server(self) -> None:
        server = HealthServer()
        mock_srv = MagicMock()
        server._server = mock_srv
        server.stop()
        mock_srv.close.assert_called_once()

    def test_stop_without_server_is_safe(self) -> None:
        server = HealthServer()
        server.stop()  # Should not raise
        assert server._server is None

    def test_port_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_HEALTH_PORT", "9999")
        server = HealthServer()
        assert server._port == 9999

    def test_default_port(self) -> None:
        server = HealthServer()
        assert server._port == 8080
