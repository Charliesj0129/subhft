"""Tests for WU-17: Structured Health Endpoint."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from hft_platform.observability.health import HealthServer


def _make_mock_system(
    running: bool = True,
    storm_state: int = 0,
    tasks_alive: bool = True,
    md_running: bool = True,
) -> MagicMock:
    """Create a mock HFTSystem for health checks."""
    from hft_platform.risk.storm_guard import StormGuardState

    system = MagicMock()
    system.running = running

    sg = MagicMock()
    sg.state = StormGuardState(storm_state)
    system.storm_guard = sg

    # Mock tasks
    tasks = {}
    for name in ("md", "strat", "order", "recorder"):
        t = MagicMock()
        t.done.return_value = not tasks_alive
        tasks[name] = t
    system.tasks = tasks

    # Mock md_service
    system.md_service = MagicMock()
    system.md_service.running = md_running

    # Mock queues
    for qname in ("raw_queue", "raw_exec_queue", "risk_queue", "order_queue", "recorder_queue"):
        q = MagicMock()
        q.qsize.return_value = 0
        setattr(system, qname, q)

    return system


class TestHealthServerReadiness:
    """Test readiness check logic."""

    def test_ready_when_system_healthy(self) -> None:
        system = _make_mock_system()
        server = HealthServer(system=system)
        is_ready, checks = server._check_readiness()
        assert is_ready is True
        assert checks["system_running"] is True
        assert checks["storm_guard"] == "NORMAL"

    def test_not_ready_when_system_not_running(self) -> None:
        system = _make_mock_system(running=False)
        server = HealthServer(system=system)
        is_ready, checks = server._check_readiness()
        assert is_ready is False

    def test_not_ready_when_storm_halt(self) -> None:
        from hft_platform.risk.storm_guard import StormGuardState

        system = _make_mock_system(storm_state=StormGuardState.HALT)
        server = HealthServer(system=system)
        is_ready, checks = server._check_readiness()
        assert is_ready is False
        assert checks["storm_guard"] == "HALT"

    def test_not_ready_when_tasks_dead(self) -> None:
        system = _make_mock_system(tasks_alive=False)
        server = HealthServer(system=system)
        is_ready, checks = server._check_readiness()
        assert is_ready is False
        assert all(v is False for v in checks["tasks"].values())

    def test_not_ready_when_no_system(self) -> None:
        server = HealthServer(system=None)
        is_ready, checks = server._check_readiness()
        assert is_ready is False
        assert checks == {"system": "not_attached"}


class TestHealthServerStatus:
    """Test full status response building."""

    def test_status_ok(self) -> None:
        system = _make_mock_system()
        server = HealthServer(system=system)
        status = server._build_status()
        assert status["status"] == "ok"
        assert "uptime_s" in status
        assert "version" in status
        assert "timestamp_ns" in status
        assert "queues" in status
        assert "storm_guard_state" in status
        assert status["storm_guard_state"] == 0

    def test_status_degraded(self) -> None:
        system = _make_mock_system(tasks_alive=False)
        server = HealthServer(system=system)
        status = server._build_status()
        assert status["status"] == "degraded"

    def test_status_unhealthy_when_no_system(self) -> None:
        server = HealthServer(system=None)
        status = server._build_status()
        assert status["status"] == "unhealthy"


class TestHealthServerHTTP:
    """Test HTTP server handling (integration-style)."""

    @pytest.mark.asyncio
    async def test_healthz_returns_200(self) -> None:
        system = _make_mock_system()
        server = HealthServer(system=system)
        server._port = 0  # Let OS pick a free port

        # Start server
        srv = await asyncio.start_server(
            server._handle_connection, "127.0.0.1", 0
        )
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

        srv = await asyncio.start_server(
            server._handle_connection, "127.0.0.1", 0
        )
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
        finally:
            srv.close()
            await srv.wait_closed()

    @pytest.mark.asyncio
    async def test_readyz_returns_503_when_unhealthy(self) -> None:
        system = _make_mock_system(running=False)
        server = HealthServer(system=system)

        srv = await asyncio.start_server(
            server._handle_connection, "127.0.0.1", 0
        )
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
        finally:
            srv.close()
            await srv.wait_closed()

    @pytest.mark.asyncio
    async def test_status_endpoint(self) -> None:
        system = _make_mock_system()
        server = HealthServer(system=system)

        srv = await asyncio.start_server(
            server._handle_connection, "127.0.0.1", 0
        )
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
            assert body["status"] == "ok"
            assert "queues" in body
            assert "checks" in body
        finally:
            srv.close()
            await srv.wait_closed()

    @pytest.mark.asyncio
    async def test_404_for_unknown_path(self) -> None:
        server = HealthServer(system=None)

        srv = await asyncio.start_server(
            server._handle_connection, "127.0.0.1", 0
        )
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

    def test_port_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_HEALTH_PORT", "9999")
        server = HealthServer()
        assert server._port == 9999

    def test_default_port(self) -> None:
        server = HealthServer()
        assert server._port == 8080
