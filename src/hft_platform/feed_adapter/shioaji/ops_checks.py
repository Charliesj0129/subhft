"""Read-only broker ops checks: CA certificate expiry and trade-cache health.

Both are telemetry. Neither changes a trading decision and neither acts on what it finds (no re-login, no
``update_status``, no HALT): an operator reads the gauges and the log. Both go through the client's
``_safe_call_with_timeout``, so a hung SDK costs a bounded wait and an abandoned worker the guard already
tracks, never an unbounded one.

CA expiry
    The order session signs with a CA certificate that expires on a calendar date. An expired CA turns every
    order into a rejection, and nothing else in the platform reads the date. ``check_ca_expiry`` runs once right
    after the CA is activated at login (``SessionRuntime.login_with_retry``), so every session refresh re-reads it.
    It never raises: a read that fails, times out or finds no ``get_ca_expiretime`` logs and sets the state gauge,
    and the login carries on.

Trade-cache health
    ``api.trade_cache_health(account)`` (shioaji >= 1.7.6) says whether order or deal reports may have been
    missed, which leaves a ``Trade`` stuck on an old status with nothing visible. ``TradeCacheHealthCheck.run_once``
    is driven by ``ops/trade_cache_health_watch.py`` in a worker thread. It is never called from an SDK callback and
    never from the hot path.

The person id is the account holder's national id. It is passed to the SDK and nowhere else: not logged, not a
label, and every SDK error text goes through ``scrub_broker_error`` first.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.feed_adapter.shioaji._compat import resolve_trading_account
from hft_platform.feed_adapter.shioaji._infra import scrub_broker_error
from hft_platform.observability.metrics import CA_EXPIRY_STATES, TRADE_CACHE_HEALTH_STATES

logger = get_logger("feed_adapter.shioaji.ops_checks")

__all__ = [
    "CA_READ_TIMEOUT_S",
    "CA_WARN_DAYS",
    "CaExpiryReport",
    "TRADE_CACHE_HEALTH_TIMEOUT_S",
    "TradeCacheHealthCheck",
    "TradeCacheHealthReport",
    "check_ca_expiry",
    "classify_ca_days",
    "publish_ca_expiry",
    "read_ca_expiry",
    "read_trade_cache_health",
]

#: A CA with fewer days than this left logs a warning at every login.
CA_WARN_DAYS = 30.0
#: Upper bound on the login-path read; a hung SDK must not hold the login for longer.
CA_READ_TIMEOUT_S = 5.0
#: Upper bound on one trade-cache health probe.
TRADE_CACHE_HEALTH_TIMEOUT_S = 5.0

_SECONDS_PER_DAY = 86400.0
_MAX_LABEL_LEN = 32
# The SDK's TradeCacheHealthState.value, mapped to the label this platform exports.
_SDK_STATES = {"Healthy": "healthy", "Unknown": "unknown", "Degraded": "degraded"}


def _call_sdk(client: Any, op: str, fn: Callable[[], Any], timeout_s: float) -> tuple[bool, Any, Any, bool]:
    """Run ``fn`` through the client's timeout guard; fall back to a bare call for a client without one."""
    guard = getattr(client, "_safe_call_with_timeout", None)
    if callable(guard):
        return guard(op, fn, timeout_s)
    try:
        return True, fn(), None, False
    except Exception as exc:
        return False, None, exc, False


def _sdk_method(api: Any, name: str) -> Callable[..., Any] | None:
    """``api.<name>`` if the SDK has it as a callable, else None.

    Not a bare ``getattr(api, name, None)``: the 1.5+ Rust core's attribute fallback raises types that are not
    ``AttributeError`` (see ``resolve_trading_account``), and a probe for a missing method must read as absent.
    """
    if api is None:
        return None
    try:
        method = getattr(api, name, None)
    except Exception:
        return None
    return method if callable(method) else None


def _enum_text(value: Any) -> str:
    """The SDK enum's ``.value`` as text ('Healthy', 'FORDER'); ``str(value)`` for anything without one."""
    if value is None:
        return ""
    inner = getattr(value, "value", value)
    try:
        return str(inner)
    except Exception:
        return ""


def _label(text: str) -> str:
    """A Prometheus-safe label value: ASCII alphanumerics and underscore, bounded length, never empty."""
    cleaned = "".join(ch for ch in text if ch.isascii() and (ch.isalnum() or ch == "_"))[:_MAX_LABEL_LEN]
    return cleaned or "other"


def _metrics_of(client: Any, override: Any = None) -> Any:
    return override if override is not None else getattr(client, "metrics", None)


# ---------------------------------------------------------------------------
# CA certificate expiry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaExpiryReport:
    """Outcome of one CA expiry check. ``state`` is one of ``CA_EXPIRY_STATES``."""

    state: str
    days_to_expiry: float | None = None
    expires_at: str | None = None
    error: str | None = None


def classify_ca_days(days: float) -> str:
    """``expired`` at or below zero days, ``warn`` below ``CA_WARN_DAYS``, else ``ok``."""
    if days <= 0.0:
        return "expired"
    if days < CA_WARN_DAYS:
        return "warn"
    return "ok"


def _expiry_utc(value: Any) -> dt.datetime | None:
    """The SDK's expiry as an aware UTC datetime, or None if it is not a date.

    1.7.11 returns a ``datetime`` (the stub says so) and the CLI/HTTP form is an ISO string; both are taken. A naive
    value is read as UTC: the documented example ends 15:59:59, which is 23:59:59 in Taipei. Were it Taipei local
    instead the answer would move by 8 hours, which is immaterial against a 30 and a 14 day threshold.
    """
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, dt.datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def read_ca_expiry(client: Any, person_id: str | None, *, now_s: float | None = None) -> CaExpiryReport:
    """Ask the SDK when the CA expires. Pure of side effects besides the one read-only SDK call."""
    if not person_id:
        return CaExpiryReport(state="no_person_id")
    reader = _sdk_method(getattr(client, "api", None), "get_ca_expiretime")
    if reader is None:
        return CaExpiryReport(state="unavailable")

    ok, result, error, timed_out = _call_sdk(client, "get_ca_expiretime", lambda: reader(person_id), CA_READ_TIMEOUT_S)
    if not ok:
        reason = "timeout" if timed_out else scrub_broker_error(error)
        return CaExpiryReport(state="read_failed", error=reason)
    expires = _expiry_utc(result)
    if expires is None:
        return CaExpiryReport(state="read_failed", error=f"unexpected_result_type:{type(result).__name__}")

    now = timebase.now_s() if now_s is None else now_s
    days = (expires.timestamp() - now) / _SECONDS_PER_DAY
    return CaExpiryReport(
        state=classify_ca_days(days),
        days_to_expiry=days,
        expires_at=expires.date().isoformat(),
    )


def publish_ca_expiry(report: CaExpiryReport, metrics: Any) -> None:
    """Write the report to the gauges. A metrics failure is logged and goes no further."""
    if metrics is None:
        return
    try:
        if report.days_to_expiry is not None:
            metrics.shioaji_ca_days_to_expiry.set(report.days_to_expiry)
        for state in CA_EXPIRY_STATES:
            metrics.shioaji_ca_expiry_state.labels(state=state).set(1 if state == report.state else 0)
    except Exception as exc:
        logger.warning("ca_expiry_publish_failed", error=scrub_broker_error(exc))


def _log_ca_report(report: CaExpiryReport) -> None:
    days = None if report.days_to_expiry is None else round(report.days_to_expiry, 1)
    if report.state == "ok":
        logger.info("ca_expiry_ok", days_to_expiry=days, expires_at=report.expires_at)
    elif report.state == "warn":
        logger.warning("ca_expiry_soon", days_to_expiry=days, expires_at=report.expires_at, threshold_days=CA_WARN_DAYS)
    elif report.state == "expired":
        logger.error("ca_expired", days_to_expiry=days, expires_at=report.expires_at)
    elif report.state == "unavailable":
        logger.warning("ca_expiry_unavailable", reason="sdk_without_get_ca_expiretime")
    elif report.state == "no_person_id":
        logger.warning("ca_expiry_skipped", reason="no_person_id")
    else:
        logger.warning("ca_expiry_read_failed", error=report.error)


def check_ca_expiry(client: Any, person_id: str | None) -> CaExpiryReport:
    """Read, log and publish the CA expiry. Never raises, so it cannot fail a login."""
    try:
        report = read_ca_expiry(client, person_id)
    except Exception as exc:
        report = CaExpiryReport(state="read_failed", error=scrub_broker_error(exc))
    _log_ca_report(report)
    publish_ca_expiry(report, _metrics_of(client))
    return report


# ---------------------------------------------------------------------------
# Trade-cache health
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TradeCacheHealthReport:
    """Outcome of one trade-cache health probe.

    ``result`` is one of ``TRADE_CACHE_HEALTH_CHECK_RESULTS``. ``state`` is one of ``TRADE_CACHE_HEALTH_STATES``, or
    None when the probe was skipped (no session, no account), which says nothing about the cache.
    ``reasons`` is a sorted tuple of ``(event_type, reason)`` as the SDK lists them.
    """

    result: str
    state: str | None
    reasons: tuple[tuple[str, str], ...] = ()
    error: str | None = None


def _parse_health(health: Any) -> TradeCacheHealthReport:
    state = _SDK_STATES.get(_enum_text(getattr(health, "state", None)))
    if state is None:
        text = _label(_enum_text(getattr(health, "state", None)))
        return TradeCacheHealthReport(result="failed", state="check_failed", error=f"unexpected_state:{text}")
    reasons: set[tuple[str, str]] = set()
    for item in list(getattr(health, "reasons", None) or ()):
        event_type = _label(_enum_text(getattr(item, "event_type", None)))
        reason = _label(_enum_text(getattr(item, "reason", None)))
        reasons.add((event_type, reason))
    return TradeCacheHealthReport(result="ok", state=state, reasons=tuple(sorted(reasons)))


def read_trade_cache_health(client: Any) -> TradeCacheHealthReport:
    """Ask the SDK for the trade-cache health of the futures account (stock account when there is none)."""
    api = getattr(client, "api", None)
    if api is None or not getattr(client, "logged_in", False):
        return TradeCacheHealthReport(result="skipped", state=None)
    reader = _sdk_method(api, "trade_cache_health")
    if reader is None:
        return TradeCacheHealthReport(result="unavailable", state="unavailable")
    account = resolve_trading_account(api, "futopt_account")
    if account is None:
        account = resolve_trading_account(api, "stock_account")
    if account is None:
        return TradeCacheHealthReport(result="skipped", state=None)

    ok, health, error, timed_out = _call_sdk(
        client, "trade_cache_health", lambda: reader(account), TRADE_CACHE_HEALTH_TIMEOUT_S
    )
    if not ok:
        reason = "timeout" if timed_out else scrub_broker_error(error)
        return TradeCacheHealthReport(result="failed", state="check_failed", error=reason)
    return _parse_health(health)


class TradeCacheHealthCheck:
    """Probe the trade cache, publish the answer, and log when it changes.

    One instance per order client: it remembers which reason series it set to 1 so a reason that stops being
    listed goes back to 0 instead of staying lit, and it remembers the last answer so a steady state logs once.
    """

    __slots__ = ("_client", "_metrics", "_lit_reasons", "_last_signature")

    def __init__(self, client: Any, metrics: Any = None) -> None:
        self._client = client
        self._metrics = metrics
        self._lit_reasons: set[tuple[str, str]] = set()
        self._last_signature: tuple[str | None, tuple[tuple[str, str], ...]] | None = None

    def run_once(self) -> TradeCacheHealthReport:
        """One probe. Never raises; safe to call from a worker thread."""
        try:
            report = read_trade_cache_health(self._client)
        except Exception as exc:
            report = TradeCacheHealthReport(result="failed", state="check_failed", error=scrub_broker_error(exc))
        self._publish(report)
        self._log_change(report)
        return report

    def _publish(self, report: TradeCacheHealthReport) -> None:
        metrics = _metrics_of(self._client, self._metrics)
        if metrics is None:
            return
        try:
            metrics.shioaji_trade_cache_health_checks_total.labels(result=report.result).inc()
            if report.state is None:
                return
            for state in TRADE_CACHE_HEALTH_STATES:
                metrics.shioaji_trade_cache_health_state.labels(state=state).set(1 if state == report.state else 0)
            lit = set(report.reasons) if report.result == "ok" else set()
            for event_type, reason in sorted(self._lit_reasons - lit):
                metrics.shioaji_trade_cache_health_reasons.labels(event_type=event_type, reason=reason).set(0)
            for event_type, reason in sorted(lit):
                metrics.shioaji_trade_cache_health_reasons.labels(event_type=event_type, reason=reason).set(1)
            self._lit_reasons = lit
            if report.result == "ok":
                metrics.shioaji_trade_cache_health_last_success_ts.set(timebase.now_s())
        except Exception as exc:
            logger.warning("trade_cache_health_publish_failed", error=scrub_broker_error(exc))

    def _log_change(self, report: TradeCacheHealthReport) -> None:
        if report.state is None:
            logger.debug("trade_cache_health_skipped", reason="no_session_or_account")
            return
        signature = (report.state, report.reasons)
        if signature == self._last_signature:
            return
        self._last_signature = signature
        reasons = [f"{event_type}:{reason}" for event_type, reason in report.reasons]
        if report.state == "degraded":
            logger.warning("trade_cache_health_degraded", reasons=reasons)
        elif report.state == "check_failed":
            logger.warning("trade_cache_health_check_failed", error=report.error)
        elif report.state == "unavailable":
            logger.warning("trade_cache_health_unavailable", reason="sdk_without_trade_cache_health")
        elif report.state == "unknown":
            logger.info("trade_cache_health_unknown", reasons=reasons)
        else:
            logger.info("trade_cache_health_healthy", reasons=reasons)
