"""Structured health endpoint server (WU-17).

Provides lightweight HTTP health endpoints for liveness, readiness, and
full status introspection.

Endpoints:
    ``/healthz``  -- Liveness: always 200 if process is running.
    ``/readyz``   -- Readiness: 200/503 with three-level status.
    ``/status``   -- Full JSON status dump.

Port configured via ``HFT_HEALTH_PORT`` (default 8080).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("observability.health")

_DEFAULT_PORT = 8080
_STARTUP_TS = time.time()

# Queue pressure threshold (fraction of maxsize)
_QUEUE_PRESSURE_THRESHOLD = 0.8

# ClickHouse write staleness threshold (nanoseconds)
_CH_WRITE_STALE_NS = 60_000_000_000  # 60 seconds


def _json_dumps(obj: Any) -> bytes:
    """Serialize *obj* to JSON bytes using orjson if available, else stdlib."""
    try:
        import orjson

        return orjson.dumps(obj)
    except ImportError:
        import json

        return json.dumps(obj, default=str).encode("utf-8")


class DegradationTracker:
    """Tracks degradation events for /status introspection."""

    __slots__ = ("_events", "_max_events")

    def __init__(self, max_events: int = 10) -> None:
        self._events: list[dict[str, Any]] = []
        self._max_events = max_events

    def record(self, reason: str, checks: dict[str, Any]) -> None:
        """Record a degradation event."""
        event: dict[str, Any] = {
            "timestamp_ns": timebase.now_ns(),
            "reason": reason,
            "checks": dict(checks),
        }
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]

    @property
    def recent(self) -> list[dict[str, Any]]:
        """Return a copy of recent degradation events."""
        return list(self._events)


class HealthServer:
    """Async HTTP health check server using raw asyncio (no third-party deps).

    Parameters
    ----------
    system : HFTSystem | None
        The running system instance for readiness / status probes.
        May be ``None`` during testing (liveness always returns 200).
    """

    __slots__ = ("_port", "_system", "_server", "_version", "_degradation_tracker")

    def __init__(self, system: Any = None) -> None:
        self._port = int(os.getenv("HFT_HEALTH_PORT", str(_DEFAULT_PORT)))
        self._system = system
        self._server: asyncio.Server | None = None
        self._version = os.getenv("HFT_VERSION", "unknown")
        self._degradation_tracker = DegradationTracker()

    # -- Checks -------------------------------------------------------------

    def _check_readiness(self) -> tuple[str, dict[str, Any]]:  # noqa: C901
        """Evaluate readiness based on system state.

        Returns (status, checks_dict) where status is one of:
        ``"ready"``, ``"degraded"``, ``"unavailable"``.
        """
        checks: dict[str, Any] = {}
        unavailable_reasons: list[str] = []
        degraded_reasons: list[str] = []

        if self._system is None:
            return "unavailable", {"system": "not_attached"}

        # 1. System running flag
        system_running: bool = getattr(self._system, "running", False)
        checks["system_running"] = system_running
        if not system_running:
            unavailable_reasons.append("system_not_running")

        # 2. Broker login
        md_client = getattr(self._system, "md_client", None)
        order_client = getattr(self._system, "order_client", None)
        md_logged_in = getattr(md_client, "logged_in", False) if md_client else False
        order_logged_in = getattr(order_client, "logged_in", False) if order_client else False
        checks["broker_login"] = {
            "md_client": md_logged_in,
            "order_client": order_logged_in,
        }
        if not (md_logged_in and order_logged_in):
            unavailable_reasons.append("broker_not_logged_in")

        # 3. StormGuard
        storm_guard = getattr(self._system, "storm_guard", None)
        if storm_guard is not None:
            from hft_platform.risk.storm_guard import StormGuardState

            sg_state = storm_guard.state
            checks["storm_guard"] = sg_state.name
            if sg_state == StormGuardState.HALT:
                unavailable_reasons.append("storm_guard_halt")
            elif sg_state in (StormGuardState.WARM, StormGuardState.STORM):
                degraded_reasons.append("storm_guard_elevated")
        else:
            checks["storm_guard"] = "unknown"

        # 4. Critical tasks alive
        tasks: dict[str, Any] = getattr(self._system, "tasks", {})
        critical_tasks = ["md", "strat", "order", "recorder"]
        if os.getenv("HFT_GATEWAY_ENABLED", "0") == "1":
            critical_tasks.append("gateway")
        else:
            critical_tasks.append("risk")
        tasks_alive: dict[str, bool] = {}
        for name in critical_tasks:
            task = tasks.get(name)
            alive = task is not None and not task.done()
            tasks_alive[name] = alive
        checks["tasks"] = tasks_alive
        dead_tasks = [n for n, alive in tasks_alive.items() if not alive]
        if dead_tasks:
            unavailable_reasons.append(f"critical_tasks_dead:{','.join(dead_tasks)}")

        # 5. Optional tasks (execution)
        optional_tasks: dict[str, bool] = {}
        for name in ("exec_router", "exec_gateway"):
            task = tasks.get(name)
            if task is not None:
                optional_tasks[name] = not task.done()
        if optional_tasks:
            checks["optional_tasks"] = optional_tasks

        # 6. Feed connected
        md_service = getattr(self._system, "md_service", None)
        feed_ok: bool = getattr(md_service, "running", False) if md_service else False
        checks["feed_connected"] = feed_ok
        if not feed_ok and system_running:
            degraded_reasons.append("feed_disconnected")

        # 7. ClickHouse write health
        recorder_service = getattr(self._system, "recorder", None)
        if recorder_service is not None:
            ch_healthy = getattr(recorder_service, "healthy", None)
            last_write_ok = getattr(recorder_service, "last_write_ok", None)
            if ch_healthy is not None:
                checks["clickhouse_write"] = ch_healthy
                if not ch_healthy:
                    degraded_reasons.append("clickhouse_unhealthy")
            elif last_write_ok is not None:
                # last_write_ok is expected to be a nanosecond timestamp
                now_ns = timebase.now_ns()
                stale = (now_ns - last_write_ok) > _CH_WRITE_STALE_NS if last_write_ok > 0 else True
                checks["clickhouse_write"] = not stale
                if stale:
                    degraded_reasons.append("clickhouse_write_stale")
            else:
                checks["clickhouse_write"] = "unknown"

        # 8. Queue pressure
        queue_names = ("raw_queue", "raw_exec_queue", "risk_queue", "order_queue", "recorder_queue")
        queue_pressure: dict[str, Any] = {}
        any_pressure = False
        for qname in queue_names:
            q = getattr(self._system, qname, None)
            if q is not None:
                qsize = q.qsize()
                maxsize = getattr(q, "maxsize", 0)
                if maxsize > 0:
                    ratio = qsize / maxsize
                    queue_pressure[qname] = {"size": qsize, "max": maxsize}
                    if ratio > _QUEUE_PRESSURE_THRESHOLD:
                        any_pressure = True
                else:
                    queue_pressure[qname] = {"size": qsize, "max": 0}
        if queue_pressure:
            checks["queue_pressure"] = queue_pressure
        if any_pressure:
            degraded_reasons.append("queue_pressure_high")

        # 9. Order path: task alive AND broker connected
        order_task = tasks.get("order")
        order_alive = order_task is not None and not order_task.done()
        if not order_alive or not order_logged_in:
            if "critical_tasks_dead:order" not in str(unavailable_reasons):
                # Only add if not already captured by critical task check
                if not order_alive and "order" not in dead_tasks:
                    unavailable_reasons.append("order_path_down")
                elif order_alive and not order_logged_in:
                    # broker_not_logged_in already covers this
                    pass

        # Determine final status
        checks["unavailable_reasons"] = unavailable_reasons
        checks["degraded_reasons"] = degraded_reasons

        if unavailable_reasons:
            status = "unavailable"
        elif degraded_reasons:
            status = "degraded"
            self._degradation_tracker.record(reason="; ".join(degraded_reasons), checks=checks)
        else:
            status = "ready"

        return status, checks

    def _build_status(self) -> dict[str, Any]:
        """Build the full ``/status`` response payload."""
        status, checks = self._check_readiness()

        result: dict[str, Any] = {
            "status": status,
            "checks": checks,
            "uptime_s": round(time.time() - _STARTUP_TS, 1),
            "version": self._version,
            "timestamp_ns": timebase.now_ns(),
        }

        # Queue depths (legacy compat + detail)
        if self._system is not None:
            queues: dict[str, int] = {}
            for qname in ("raw_queue", "raw_exec_queue", "risk_queue", "order_queue", "recorder_queue"):
                q = getattr(self._system, qname, None)
                if q is not None:
                    queues[qname] = q.qsize()
            result["queues"] = queues

            # StormGuard state value
            sg = getattr(self._system, "storm_guard", None)
            if sg is not None:
                result["storm_guard_state"] = int(sg.state)

        # Recent degradation events
        recent_degradations = self._degradation_tracker.recent
        if recent_degradations:
            result["degradation_events"] = recent_degradations

        return result

    # -- HTTP protocol handler (minimal HTTP/1.1) ---------------------------

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle a single HTTP connection."""
        try:
            # Read request line (up to 4 KiB)
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                return

            parts = request_line.decode("utf-8", errors="replace").split()
            if len(parts) < 2:
                self._send_response(writer, 400, b'{"error":"bad_request"}')
                return

            path = parts[1].split("?")[0]  # Strip query string

            # Consume remaining headers (read until blank line)
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break

            if path == "/healthz":
                self._send_response(writer, 200, _json_dumps({"status": "ok"}))
            elif path == "/readyz":
                status, checks = self._check_readiness()
                if status == "unavailable":
                    code = 503
                else:
                    code = 200
                body = _json_dumps({"status": status, "checks": checks})
                extra_headers: dict[str, str] = {}
                if status == "degraded":
                    extra_headers["X-Health-Status"] = "degraded"
                self._send_response(writer, code, body, extra_headers=extra_headers)
            elif path == "/status":
                body = _json_dumps(self._build_status())
                self._send_response(writer, 200, body)
            else:
                self._send_response(writer, 404, b'{"error":"not_found"}')

        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            logger.debug("health_request_error", error=str(exc))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    def _send_response(
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """Write a minimal HTTP/1.1 response."""
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 503: "Service Unavailable"}.get(status, "Unknown")
        header_lines = [
            f"HTTP/1.1 {status} {reason}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        if extra_headers:
            for key, value in extra_headers.items():
                header_lines.append(f"{key}: {value}")
        header_lines.append("")
        header_lines.append("")
        header = "\r\n".join(header_lines)
        writer.write(header.encode("utf-8") + body)

    # -- Lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        """Start the health HTTP server and serve until cancelled."""
        try:
            self._server = await asyncio.start_server(self._handle_connection, "0.0.0.0", self._port)  # nosec B104
        except OSError as exc:
            logger.error("health_server_bind_failed", port=self._port, error=str(exc))
            return

        logger.info("health_server_started", port=self._port)
        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("health_server_stopped")

    def stop(self) -> None:
        """Close the server."""
        if self._server is not None:
            self._server.close()
