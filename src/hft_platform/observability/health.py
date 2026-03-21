"""Structured health endpoint server (WU-17).

Provides lightweight HTTP health endpoints for liveness, readiness, and
full status introspection.

Endpoints:
    ``/healthz``  -- Liveness: always 200 if process is running.
    ``/readyz``   -- Readiness: 200 if services healthy, 503 otherwise.
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


def _json_dumps(obj: Any) -> bytes:
    """Serialize *obj* to JSON bytes using orjson if available, else stdlib."""
    try:
        import orjson

        return orjson.dumps(obj)
    except ImportError:
        import json

        return json.dumps(obj, default=str).encode("utf-8")


class HealthServer:
    """Async HTTP health check server using raw asyncio (no third-party deps).

    Parameters
    ----------
    system : HFTSystem | None
        The running system instance for readiness / status probes.
        May be ``None`` during testing (liveness always returns 200).
    """

    __slots__ = ("_port", "_system", "_server", "_version")

    def __init__(self, system: Any = None) -> None:
        self._port = int(os.getenv("HFT_HEALTH_PORT", str(_DEFAULT_PORT)))
        self._system = system
        self._server: asyncio.Server | None = None
        self._version = os.getenv("HFT_VERSION", "unknown")

    # -- Checks -------------------------------------------------------------

    def _check_readiness(self) -> tuple[bool, dict[str, Any]]:
        """Evaluate readiness based on system state.

        Returns (is_ready, checks_dict).
        """
        checks: dict[str, Any] = {}

        if self._system is None:
            return False, {"system": "not_attached"}

        # 1. System running flag
        system_running = getattr(self._system, "running", False)
        checks["system_running"] = system_running

        # 2. StormGuard not in HALT
        storm_guard = getattr(self._system, "storm_guard", None)
        if storm_guard is not None:
            from hft_platform.risk.storm_guard import StormGuardState

            sg_state = storm_guard.state
            checks["storm_guard"] = sg_state.name
            storm_ok = sg_state != StormGuardState.HALT
        else:
            storm_ok = True
            checks["storm_guard"] = "unknown"

        # 3. Critical tasks alive
        tasks = getattr(self._system, "tasks", {})
        critical_tasks = ["md", "strat", "order", "recorder"]
        tasks_alive: dict[str, bool] = {}
        for name in critical_tasks:
            task = tasks.get(name)
            alive = task is not None and not task.done()
            tasks_alive[name] = alive
        checks["tasks"] = tasks_alive
        all_tasks_ok = all(tasks_alive.values())

        # 4. Feed connected (md_service.running)
        md_service = getattr(self._system, "md_service", None)
        feed_ok = getattr(md_service, "running", False) if md_service else False
        checks["feed_connected"] = feed_ok

        is_ready = system_running and storm_ok and all_tasks_ok
        return is_ready, checks

    def _build_status(self) -> dict[str, Any]:
        """Build the full ``/status`` response payload."""
        is_ready, checks = self._check_readiness()

        status_label = "ok" if is_ready else "unhealthy"
        # Detect degraded: system running but some tasks down or storm != NORMAL
        if not is_ready and self._system and getattr(self._system, "running", False):
            status_label = "degraded"

        result: dict[str, Any] = {
            "status": status_label,
            "checks": checks,
            "uptime_s": round(time.time() - _STARTUP_TS, 1),
            "version": self._version,
            "timestamp_ns": timebase.now_ns(),
        }

        # Queue depths
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
                is_ready, checks = self._check_readiness()
                code = 200 if is_ready else 503
                status_label = "ok" if is_ready else "unavailable"
                self._send_response(writer, code, _json_dumps({"status": status_label, "checks": checks}))
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
    def _send_response(writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
        """Write a minimal HTTP/1.1 response."""
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 503: "Service Unavailable"}.get(status, "Unknown")
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        writer.write(header.encode("utf-8") + body)

    # -- Lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        """Start the health HTTP server and serve until cancelled."""
        try:
            self._server = await asyncio.start_server(self._handle_connection, "0.0.0.0", self._port)  # nosec B104 — intentional bind-all for Docker container
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
