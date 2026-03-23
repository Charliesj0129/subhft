import functools
from typing import Any, Callable, TypeVar

from structlog import get_logger

_logger = get_logger("utils.metrics")

F = TypeVar("F", bound=Callable[..., Any])


def suppress_metrics_errors(fn: F) -> F:
    """Decorator: suppress exceptions in metrics/observability code.

    Metrics must never crash the hot path. This centralizes the common
    ``try: ... except Exception: return`` pattern (H4 audit finding).
    Errors are logged at DEBUG level for debuggability.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            _logger.debug("metrics_error_suppressed", fn=fn.__qualname__, error=str(exc))
            return None

    return wrapper  # type: ignore[return-value]


class Metrics:
    """Stub for Prometheus/StatsD metrics."""

    @staticmethod
    def counter(name: str, value: int = 1, tags: dict[str, Any] | None = None):
        # In real impl, send to statsd
        pass

    @staticmethod
    def gauge(name: str, value: float, tags: dict[str, Any] | None = None):
        pass

    @staticmethod
    def histogram(name: str, value: float, tags: dict[str, Any] | None = None):
        pass
