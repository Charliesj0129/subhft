"""Resilient Prometheus metrics HTTP server.

The Shioaji Solace C library intermittently corrupts Python heap objects,
causing MutexValue._lock on prometheus_client Histogram buckets to be
replaced with arbitrary types (dict, tuple, etc.). This makes the default
/metrics endpoint return 500 on every scrape.

This module wraps the WSGI app to catch TypeError from corrupted collectors
and return partial metrics (skipping broken ones) rather than a full 500.
"""

from __future__ import annotations

import threading
from http.server import HTTPServer
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, make_server

from prometheus_client import REGISTRY
from prometheus_client.exposition import _bake_output
from structlog import get_logger

logger = get_logger("metrics_server")

_corruption_logged: bool = False


def _resilient_metrics_app(
    environ: dict[str, Any],
    start_response: Any,
) -> list[bytes]:
    """WSGI app that tolerates corrupted prometheus collectors."""
    global _corruption_logged  # noqa: PLW0603

    accept_header = environ.get("HTTP_ACCEPT", "")
    accept_encoding = environ.get("HTTP_ACCEPT_ENCODING", "")
    params = environ.get("QUERY_STRING", "")

    try:
        status, headers, output = _bake_output(
            REGISTRY,
            accept_header,
            accept_encoding,
            params,
            False,
        )
        start_response(status, headers)
        return [output]
    except TypeError as exc:
        # Heap corruption from Shioaji Solace C library — collect individually
        if not _corruption_logged:
            logger.warning(
                "metrics_collector_corruption_detected",
                error=str(exc),
                hint="Shioaji Solace C heap corruption — serving partial metrics",
            )
            _corruption_logged = True

        return _collect_partial(start_response)


def _collect_partial(start_response: Any) -> list[bytes]:
    """Collect metrics one-by-one, skipping corrupted collectors."""
    lines: list[str] = []
    skipped = 0

    # Direct access to the registry's internal collector list
    collectors = list(REGISTRY._names_to_collectors.values())
    seen = set()

    for collector in collectors:
        if id(collector) in seen:
            continue
        seen.add(id(collector))
        try:
            for metric_family in collector.collect():
                for sample in metric_family.samples:
                    labels = ",".join(f'{k}="{v}"' for k, v in sorted(sample.labels.items()))
                    name = f"{metric_family.name}{sample.name}" if sample.name else metric_family.name
                    if labels:
                        lines.append(f"{name}{{{labels}}} {sample.value}")
                    else:
                        lines.append(f"{name} {sample.value}")
        except (TypeError, AttributeError):
            skipped += 1

    if skipped:
        lines.append(f"# skipped {skipped} corrupted collector(s)")

    output = "\n".join(lines).encode("utf-8") + b"\n"
    start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
    return [output]


class _SilentHandler(WSGIRequestHandler):
    """Suppress request logging."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


def start_resilient_metrics_server(
    port: int,
    addr: str = "0.0.0.0",  # nosec B104
) -> tuple[HTTPServer, threading.Thread]:
    """Start a Prometheus-compatible metrics server with corruption tolerance."""
    httpd = make_server(addr, port, _resilient_metrics_app, handler_class=_SilentHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, t
