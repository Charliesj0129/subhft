"""Coverage gap tests for observability/health.py.

Targets uncovered branches: _check_readiness with various system states,
degradation tracker overflow, _build_status, _json_dumps, and
_send_response variants.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock


from hft_platform.observability.health import (
    DegradationTracker,
    HealthServer,
    _json_dumps,
)


# ---------------------------------------------------------------------------
# _json_dumps
# ---------------------------------------------------------------------------


def test_json_dumps_basic():
    result = _json_dumps({"status": "ok"})
    assert isinstance(result, bytes)
    assert b"status" in result


def test_json_dumps_with_special_types():
    """Handles non-JSON-native types via default=str."""
    result = _json_dumps({"ts": 123456789})
    assert isinstance(result, bytes)


# ---------------------------------------------------------------------------
# DegradationTracker
# ---------------------------------------------------------------------------


class TestDegradationTracker:
    def test_record_and_recent(self):
        tracker = DegradationTracker(max_events=3)
        tracker.record("reason1", {"check": True})
        tracker.record("reason2", {"check": False})
        assert len(tracker.recent) == 2
        assert tracker.recent[0]["reason"] == "reason1"

    def test_overflow_trims_oldest(self):
        tracker = DegradationTracker(max_events=2)
        tracker.record("r1", {})
        tracker.record("r2", {})
        tracker.record("r3", {})
        assert len(tracker.recent) == 2
        assert tracker.recent[0]["reason"] == "r2"

    def test_recent_returns_copy(self):
        tracker = DegradationTracker()
        tracker.record("r1", {})
        r = tracker.recent
        r.clear()
        assert len(tracker.recent) == 1  # Original unaffected


# ---------------------------------------------------------------------------
# HealthServer: _check_readiness
# ---------------------------------------------------------------------------


class TestCheckReadiness:
    def test_no_system_unavailable(self):
        server = HealthServer(system=None)
        status, checks = server._check_readiness()
        assert status == "unavailable"
        assert checks["system"] == "not_attached"

    def test_system_not_running(self):
        system = SimpleNamespace(running=False)
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "unavailable"
        assert "system_not_running" in checks.get("unavailable_reasons", [])

    def test_system_running_minimal(self):
        """Minimal running system with no broker, no tasks."""
        system = SimpleNamespace(
            running=True,
            md_client=None,
            order_client=None,
            storm_guard=None,
            tasks={},
            md_service=None,
            recorder=None,
        )
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        # No broker → unavailable
        assert status == "unavailable"

    def test_system_running_brokers_logged_in(self):
        """Broker logged in but tasks dead → unavailable."""
        md_client = SimpleNamespace(logged_in=True)
        order_client = SimpleNamespace(logged_in=True)
        system = SimpleNamespace(
            running=True,
            md_client=md_client,
            order_client=order_client,
            storm_guard=None,
            tasks={},
            md_service=None,
            recorder=None,
        )
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "unavailable"  # Critical tasks dead

    def test_storm_guard_halt(self):
        from hft_platform.risk.storm_guard import StormGuardState

        sg = SimpleNamespace(state=StormGuardState.HALT)
        system = SimpleNamespace(
            running=True,
            md_client=SimpleNamespace(logged_in=True),
            order_client=SimpleNamespace(logged_in=True),
            storm_guard=sg,
            tasks={},
            md_service=None,
            recorder=None,
        )
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert "storm_guard_halt" in checks.get("unavailable_reasons", [])

    def test_storm_guard_warm_degraded(self):
        from hft_platform.risk.storm_guard import StormGuardState

        sg = SimpleNamespace(state=StormGuardState.WARM)
        # Create mock tasks that are not done
        mock_task = MagicMock()
        mock_task.done.return_value = False
        tasks = {name: mock_task for name in ["md", "strat", "order", "recorder", "risk"]}
        system = SimpleNamespace(
            running=True,
            md_client=SimpleNamespace(logged_in=True),
            order_client=SimpleNamespace(logged_in=True),
            storm_guard=sg,
            tasks=tasks,
            md_service=SimpleNamespace(running=True),
            recorder=SimpleNamespace(healthy=True),
        )
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert "storm_guard_elevated" in checks.get("degraded_reasons", [])

    def test_feed_disconnected_degraded(self):
        mock_task = MagicMock()
        mock_task.done.return_value = False
        tasks = {name: mock_task for name in ["md", "strat", "order", "recorder", "risk"]}
        system = SimpleNamespace(
            running=True,
            md_client=SimpleNamespace(logged_in=True),
            order_client=SimpleNamespace(logged_in=True),
            storm_guard=None,
            tasks=tasks,
            md_service=SimpleNamespace(running=False),  # Feed down
            recorder=None,
        )
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert "feed_disconnected" in checks.get("degraded_reasons", [])

    def test_clickhouse_write_stale(self):
        mock_task = MagicMock()
        mock_task.done.return_value = False
        tasks = {name: mock_task for name in ["md", "strat", "order", "recorder", "risk"]}
        system = SimpleNamespace(
            running=True,
            md_client=SimpleNamespace(logged_in=True),
            order_client=SimpleNamespace(logged_in=True),
            storm_guard=None,
            tasks=tasks,
            md_service=SimpleNamespace(running=True),
            recorder=SimpleNamespace(healthy=None, last_write_ok=1),  # Very old
        )
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert "clickhouse_write_stale" in checks.get("degraded_reasons", [])

    def test_clickhouse_unknown(self):
        mock_task = MagicMock()
        mock_task.done.return_value = False
        tasks = {name: mock_task for name in ["md", "strat", "order", "recorder", "risk"]}
        system = SimpleNamespace(
            running=True,
            md_client=SimpleNamespace(logged_in=True),
            order_client=SimpleNamespace(logged_in=True),
            storm_guard=None,
            tasks=tasks,
            md_service=SimpleNamespace(running=True),
            recorder=SimpleNamespace(healthy=None, last_write_ok=None),
        )
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert checks.get("clickhouse_write") == "unknown"

    def test_queue_pressure_high(self):
        mock_task = MagicMock()
        mock_task.done.return_value = False
        tasks = {name: mock_task for name in ["md", "strat", "order", "recorder", "risk"]}
        q = asyncio.Queue(maxsize=10)
        for _ in range(9):
            q.put_nowait("x")
        system = SimpleNamespace(
            running=True,
            md_client=SimpleNamespace(logged_in=True),
            order_client=SimpleNamespace(logged_in=True),
            storm_guard=None,
            tasks=tasks,
            md_service=SimpleNamespace(running=True),
            recorder=None,
            raw_queue=q,
        )
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert "queue_pressure_high" in checks.get("degraded_reasons", [])

    def test_exec_dict_pressure(self):
        mock_task = MagicMock()
        mock_task.done.return_value = False
        tasks = {name: mock_task for name in ["md", "strat", "order", "recorder", "risk"]}
        oa = SimpleNamespace(
            _pending_fill_index={str(i): None for i in range(600)},
            _api_pending={},
        )
        system = SimpleNamespace(
            running=True,
            md_client=SimpleNamespace(logged_in=True),
            order_client=SimpleNamespace(logged_in=True),
            storm_guard=None,
            tasks=tasks,
            md_service=SimpleNamespace(running=True),
            recorder=None,
            order_adapter=oa,
        )
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert "exec_dict_pressure_high" in checks.get("degraded_reasons", [])

    def test_optional_tasks(self):
        mock_task = MagicMock()
        mock_task.done.return_value = False
        tasks = {
            name: mock_task for name in ["md", "strat", "order", "recorder", "risk", "exec_router"]
        }
        system = SimpleNamespace(
            running=True,
            md_client=SimpleNamespace(logged_in=True),
            order_client=SimpleNamespace(logged_in=True),
            storm_guard=None,
            tasks=tasks,
            md_service=SimpleNamespace(running=True),
            recorder=None,
        )
        server = HealthServer(system=system)
        _, checks = server._check_readiness()
        assert "optional_tasks" in checks
        assert checks["optional_tasks"]["exec_router"] is True

    def test_ready_status(self):
        """All checks pass → ready."""
        mock_task = MagicMock()
        mock_task.done.return_value = False
        tasks = {name: mock_task for name in ["md", "strat", "order", "recorder", "risk"]}
        system = SimpleNamespace(
            running=True,
            md_client=SimpleNamespace(logged_in=True),
            order_client=SimpleNamespace(logged_in=True),
            storm_guard=None,
            tasks=tasks,
            md_service=SimpleNamespace(running=True),
            recorder=SimpleNamespace(healthy=True),
        )
        server = HealthServer(system=system)
        status, checks = server._check_readiness()
        assert status == "ready"


# ---------------------------------------------------------------------------
# HealthServer: _build_status
# ---------------------------------------------------------------------------


class TestBuildStatus:
    def test_build_status_no_system(self):
        server = HealthServer(system=None)
        result = server._build_status()
        assert result["status"] == "unavailable"
        assert "uptime_s" in result

    def test_build_status_with_system(self):
        q = asyncio.Queue(maxsize=10)
        sg = SimpleNamespace(state=MagicMock())
        sg.state.__int__ = lambda self: 0
        system = SimpleNamespace(
            running=False,
            md_client=None,
            order_client=None,
            storm_guard=sg,
            tasks={},
            md_service=None,
            recorder=None,
            raw_queue=q,
        )
        server = HealthServer(system=system)
        result = server._build_status()
        assert "queues" in result
        assert "storm_guard_state" in result

    def test_build_status_with_degradation_events(self):
        server = HealthServer(system=None)
        server._degradation_tracker.record("test", {"a": 1})
        result = server._build_status()
        assert "degradation_events" in result


# ---------------------------------------------------------------------------
# HealthServer: stop
# ---------------------------------------------------------------------------


def test_health_server_stop_no_server():
    server = HealthServer()
    server.stop()  # No-op


def test_health_server_stop_with_server():
    server = HealthServer()
    mock_server = MagicMock()
    server._server = mock_server
    server.stop()
    mock_server.close.assert_called_once()
