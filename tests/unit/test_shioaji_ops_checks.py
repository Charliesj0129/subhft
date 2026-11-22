"""CA expiry and trade-cache health: read-only broker ops checks (``feed_adapter/shioaji/ops_checks.py``).

Every test runs against fakes of the SDK surface the checks may touch; none logs in or connects. The two properties
that matter more than the readings: a failing check never raises into the login or the watch that calls it, and
neither check takes an action (no re-login, no ``update_status``, no HALT).
"""

from __future__ import annotations

import datetime as dt
import inspect
import math
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.shioaji import ops_checks
from hft_platform.feed_adapter.shioaji.ops_checks import (
    CaExpiryReport,
    TradeCacheHealthCheck,
    check_ca_expiry,
    classify_ca_days,
    read_ca_expiry,
    read_trade_cache_health,
)
from hft_platform.observability.metrics import CA_EXPIRY_STATES, TRADE_CACHE_HEALTH_STATES, MetricsRegistry

NOW = 1_800_000_000.0
DAY = 86400.0
PERSON_ID = "A123456789"  # fake; matches the national-id shape scrub_broker_error redacts


def _naive_utc(days_from_now: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(NOW + days_from_now * DAY, tz=dt.timezone.utc).replace(tzinfo=None)


@pytest.fixture
def metrics():
    MetricsRegistry._instance = None
    registry = MetricsRegistry.get()
    yield registry
    MetricsRegistry._instance = None  # the next get() builds a registry consistent with the global REGISTRY


@pytest.fixture
def fixed_now(monkeypatch):
    monkeypatch.setattr(ops_checks.timebase, "now_s", lambda: NOW)


def _value(metric: Any, suffix: str = "", **labels: str) -> float:
    """The sample of ``metric`` with exactly these labels (counters: pass suffix="_total")."""
    for family in metric.collect():
        for sample in family.samples:
            if sample.labels == labels and sample.name.endswith(suffix) and not sample.name.endswith("_created"):
                return sample.value
    raise AssertionError(f"no sample with labels {labels} on {metric}")


def _one_hot(metric: Any, states: tuple[str, ...]) -> dict[str, float]:
    return {state: _value(metric, state=state) for state in states}


class _Enum:
    """Stands in for an SDK enum: it has ``.value``."""

    def __init__(self, value: str) -> None:
        self.value = value


@dataclass
class FakeReason:
    event_type: Any
    reason: Any


@dataclass
class FakeHealth:
    state: Any
    reasons: list[Any] = field(default_factory=list)


def _health(state: str, *pairs: tuple[str, str]) -> FakeHealth:
    return FakeHealth(_Enum(state), [FakeReason(_Enum(e), _Enum(r)) for e, r in pairs])


class FakeApi:
    """The SDK surface the checks may touch, recording every call."""

    def __init__(
        self,
        *,
        ca: Any = None,
        ca_error: Exception | None = None,
        health: Any = None,
        health_error: Exception | None = None,
        futopt_account: Any = "FUT-ACCT",
        stock_account: Any = "STK-ACCT",
    ) -> None:
        self.ca = ca
        self.ca_error = ca_error
        self.health = health
        self.health_error = health_error
        self.futopt_account = futopt_account
        self.stock_account = stock_account
        self.calls: list[tuple[str, Any]] = []

    def get_ca_expiretime(self, person_id: str) -> Any:
        self.calls.append(("get_ca_expiretime", person_id))
        if self.ca_error is not None:
            raise self.ca_error
        return self.ca

    def trade_cache_health(self, account: Any) -> Any:
        self.calls.append(("trade_cache_health", account))
        if self.health_error is not None:
            raise self.health_error
        return self.health

    def login(self, **kwargs: Any) -> None:
        self.calls.append(("login", None))

    def activate_ca(self, **kwargs: Any) -> bool:
        self.calls.append(("activate_ca", None))
        return True


class FakeClient:
    """What ops_checks reads off ShioajiClient: api, logged_in, metrics and the timeout guard."""

    def __init__(self, api: Any, *, metrics: Any = None, logged_in: bool = True, guard_result: Any = None) -> None:
        self.api = api
        self.metrics = metrics
        self.logged_in = logged_in
        self.guard_result = guard_result
        self.guard_calls: list[tuple[str, float]] = []

    def _safe_call_with_timeout(self, op: str, fn: Any, timeout_s: float) -> tuple[bool, Any, Any, bool]:
        self.guard_calls.append((op, timeout_s))
        if self.guard_result is not None:
            return self.guard_result
        try:
            return True, fn(), None, False
        except Exception as exc:
            return False, None, exc, False


# ---------------------------------------------------------------------------
# CA expiry: reading and classifying
# ---------------------------------------------------------------------------


def test_ca_expiry_read_reports_days_left_from_a_naive_utc_datetime():
    api = FakeApi(ca=_naive_utc(45.5))

    report = read_ca_expiry(FakeClient(api), PERSON_ID, now_s=NOW)

    assert report.state == "ok"
    assert report.days_to_expiry == pytest.approx(45.5)
    assert report.expires_at == _naive_utc(45.5).date().isoformat()
    assert api.calls == [("get_ca_expiretime", PERSON_ID)]


@pytest.mark.parametrize(
    ("days", "state"),
    [(400.0, "ok"), (30.0, "ok"), (29.9, "warn"), (0.1, "warn"), (0.0, "expired"), (-3.0, "expired")],
)
def test_ca_days_classify_at_the_warn_and_expired_boundaries(days, state):
    assert classify_ca_days(days) == state


def test_ca_expiry_accepts_a_timezone_aware_value_as_the_same_instant():
    taipei = dt.timezone(dt.timedelta(hours=8))
    expiry = dt.datetime.fromtimestamp(NOW + 10 * DAY, tz=taipei)

    report = read_ca_expiry(FakeClient(FakeApi(ca=expiry)), PERSON_ID, now_s=NOW)

    assert report.days_to_expiry == pytest.approx(10.0)
    assert report.state == "warn"


def test_ca_expiry_accepts_the_iso_string_the_cli_and_http_forms_return():
    text = _naive_utc(20).isoformat()

    report = read_ca_expiry(FakeClient(FakeApi(ca=text)), PERSON_ID, now_s=NOW)

    assert report.state == "warn"
    assert report.days_to_expiry == pytest.approx(20.0)


def test_ca_expiry_result_that_is_not_a_date_is_a_failed_read():
    report = read_ca_expiry(FakeClient(FakeApi(ca=None)), PERSON_ID, now_s=NOW)

    assert report.state == "read_failed"
    assert report.days_to_expiry is None
    assert report.error == "unexpected_result_type:NoneType"


def test_ca_expiry_without_a_person_id_is_not_asked():
    api = FakeApi(ca=_naive_utc(40))

    for missing in (None, ""):
        assert read_ca_expiry(FakeClient(api), missing, now_s=NOW).state == "no_person_id"

    assert api.calls == []


def test_ca_expiry_on_an_sdk_without_the_method_is_unavailable():
    class OldApi:
        pass

    report = read_ca_expiry(FakeClient(OldApi()), PERSON_ID, now_s=NOW)

    assert report.state == "unavailable"


def test_ca_expiry_timeout_is_a_failed_read_and_goes_through_the_timeout_guard():
    client = FakeClient(FakeApi(ca=_naive_utc(40)), guard_result=(False, None, TimeoutError("slow"), True))

    report = read_ca_expiry(client, PERSON_ID, now_s=NOW)

    assert report == CaExpiryReport(state="read_failed", error="timeout")
    assert client.guard_calls == [("get_ca_expiretime", ops_checks.CA_READ_TIMEOUT_S)]


def test_ca_expiry_sdk_error_is_scrubbed_of_identity_and_host():
    error = RuntimeError(f"CaError: cannot read cert for person_id={PERSON_ID} from 10.1.2.3")

    report = read_ca_expiry(FakeClient(FakeApi(ca_error=error)), PERSON_ID, now_s=NOW)

    assert report.state == "read_failed"
    assert PERSON_ID not in (report.error or "")
    assert "10.1.2.3" not in (report.error or "")


# ---------------------------------------------------------------------------
# CA expiry: logging and gauges
# ---------------------------------------------------------------------------


def test_ca_expiry_under_30_days_warns_and_sets_the_warn_state(metrics, fixed_now, module_log_sink):
    events = module_log_sink(ops_checks)
    client = FakeClient(FakeApi(ca=_naive_utc(12.4)), metrics=metrics)

    report = check_ca_expiry(client, PERSON_ID)

    assert report.state == "warn"
    soon = [e for e in events if e["event"] == "ca_expiry_soon"]
    assert len(soon) == 1
    assert soon[0]["log_level"] == "warning"
    assert soon[0]["days_to_expiry"] == 12.4
    assert soon[0]["threshold_days"] == 30.0
    assert _value(metrics.shioaji_ca_days_to_expiry) == pytest.approx(12.4)
    assert _one_hot(metrics.shioaji_ca_expiry_state, CA_EXPIRY_STATES) == {
        "ok": 0.0,
        "warn": 1.0,
        "expired": 0.0,
        "read_failed": 0.0,
        "unavailable": 0.0,
        "no_person_id": 0.0,
    }


def test_ca_expiry_over_30_days_is_ok_and_does_not_warn(metrics, fixed_now, module_log_sink):
    events = module_log_sink(ops_checks)

    report = check_ca_expiry(FakeClient(FakeApi(ca=_naive_utc(200)), metrics=metrics), PERSON_ID)

    assert report.state == "ok"
    assert [e for e in events if e["log_level"] in ("warning", "error")] == []
    assert _one_hot(metrics.shioaji_ca_expiry_state, CA_EXPIRY_STATES)["ok"] == 1.0
    assert _value(metrics.shioaji_ca_days_to_expiry) == pytest.approx(200.0)


def test_an_expired_ca_logs_an_error_and_exports_negative_days(metrics, fixed_now, module_log_sink):
    events = module_log_sink(ops_checks)

    report = check_ca_expiry(FakeClient(FakeApi(ca=_naive_utc(-2)), metrics=metrics), PERSON_ID)

    assert report.state == "expired"
    assert [e["log_level"] for e in events if e["event"] == "ca_expired"] == ["error"]
    assert _value(metrics.shioaji_ca_days_to_expiry) == pytest.approx(-2.0)
    assert _one_hot(metrics.shioaji_ca_expiry_state, CA_EXPIRY_STATES)["expired"] == 1.0


def test_a_failing_ca_read_logs_sets_the_state_and_keeps_the_last_good_days(metrics, fixed_now, module_log_sink):
    events = module_log_sink(ops_checks)
    check_ca_expiry(FakeClient(FakeApi(ca=_naive_utc(50)), metrics=metrics), PERSON_ID)
    failing = FakeClient(FakeApi(ca_error=RuntimeError(f"boom person_id={PERSON_ID}")), metrics=metrics)

    report = check_ca_expiry(failing, PERSON_ID)

    assert report.state == "read_failed"
    assert _one_hot(metrics.shioaji_ca_expiry_state, CA_EXPIRY_STATES)["read_failed"] == 1.0
    assert _one_hot(metrics.shioaji_ca_expiry_state, CA_EXPIRY_STATES)["ok"] == 0.0
    assert _value(metrics.shioaji_ca_days_to_expiry) == pytest.approx(50.0)
    failed = [e for e in events if e["event"] == "ca_expiry_read_failed"]
    assert [e["log_level"] for e in failed] == ["warning"]
    assert PERSON_ID not in repr(events)


def test_a_ca_never_read_leaves_days_nan_so_no_threshold_alert_can_fire(metrics):
    report = check_ca_expiry(FakeClient(FakeApi(), metrics=metrics), None)

    assert report.state == "no_person_id"
    assert math.isnan(_value(metrics.shioaji_ca_days_to_expiry))
    assert _one_hot(metrics.shioaji_ca_expiry_state, CA_EXPIRY_STATES)["no_person_id"] == 1.0


def test_check_ca_expiry_never_raises_when_the_read_itself_blows_up(metrics, monkeypatch):
    def boom(*args: Any, **kwargs: Any) -> CaExpiryReport:
        raise ValueError(f"unexpected {PERSON_ID}")

    monkeypatch.setattr(ops_checks, "read_ca_expiry", boom)

    report = check_ca_expiry(FakeClient(FakeApi(), metrics=metrics), PERSON_ID)

    assert report.state == "read_failed"
    assert PERSON_ID not in (report.error or "")


def test_check_ca_expiry_never_raises_when_the_gauge_write_fails(fixed_now):
    broken = MagicMock()
    broken.shioaji_ca_expiry_state.labels.side_effect = RuntimeError("registry gone")

    report = check_ca_expiry(FakeClient(FakeApi(ca=_naive_utc(60)), metrics=broken), PERSON_ID)

    assert report.state == "ok"


# ---------------------------------------------------------------------------
# CA expiry: wired into the login
# ---------------------------------------------------------------------------


def _login_runtime(api: FakeApi, metrics: Any, *, activate_ca: bool):
    from hft_platform.feed_adapter.shioaji.session_runtime import SessionRuntime

    client = MagicMock()
    client.api = api
    client.metrics = metrics
    client.logged_in = False
    client.ca_active = False
    client.activate_ca = activate_ca
    client.ca_path = "/certs/ca.p12"
    client.fetch_contract = False
    client.contracts_ready = True
    client.subscribe_trade = True
    client._login_retry_max = 0
    client._last_login_error = None
    client._last_session_refresh_ts = 0.0

    def inline(op: str, fn: Any, timeout_s: float) -> tuple[bool, Any, Any, bool]:
        try:
            return True, fn(), None, False
        except Exception as exc:
            return False, None, exc, False

    client._safe_call_with_timeout = inline
    return SessionRuntime(client), client


@pytest.fixture
def login_env(monkeypatch):
    monkeypatch.setenv("SHIOAJI_API_KEY", "k")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "s")
    monkeypatch.setenv("SHIOAJI_PERSON_ID", PERSON_ID)
    monkeypatch.setenv("SHIOAJI_CA_PASSWORD", "capass")


def test_login_with_an_active_ca_reads_the_expiry_with_the_person_id(metrics, fixed_now, login_env):
    api = FakeApi(ca=_naive_utc(18))
    runtime, client = _login_runtime(api, metrics, activate_ca=True)

    assert runtime.login_with_retry() is True

    assert ("get_ca_expiretime", PERSON_ID) in api.calls
    assert api.calls.index(("activate_ca", None)) < api.calls.index(("get_ca_expiretime", PERSON_ID))
    assert _value(metrics.shioaji_ca_days_to_expiry) == pytest.approx(18.0)
    assert _one_hot(metrics.shioaji_ca_expiry_state, CA_EXPIRY_STATES)["warn"] == 1.0


def test_login_succeeds_when_the_ca_expiry_cannot_be_read(metrics, fixed_now, login_env):
    api = FakeApi(ca_error=RuntimeError("cert store unavailable"))
    runtime, client = _login_runtime(api, metrics, activate_ca=True)

    assert runtime.login_with_retry() is True

    assert client.logged_in is True
    assert client.ca_active is True
    assert _one_hot(metrics.shioaji_ca_expiry_state, CA_EXPIRY_STATES)["read_failed"] == 1.0


def test_login_without_ca_activation_never_asks_for_the_expiry(metrics, fixed_now, login_env):
    api = FakeApi(ca=_naive_utc(18))
    runtime, client = _login_runtime(api, metrics, activate_ca=False)

    assert runtime.login_with_retry() is True

    assert [name for name, _ in api.calls if name == "get_ca_expiretime"] == []
    assert math.isnan(_value(metrics.shioaji_ca_days_to_expiry))


# ---------------------------------------------------------------------------
# Trade-cache health
# ---------------------------------------------------------------------------


def test_healthy_trade_cache_is_exported_one_hot_with_a_success_timestamp(metrics, fixed_now):
    api = FakeApi(health=_health("Healthy"))
    check = TradeCacheHealthCheck(FakeClient(api, metrics=metrics))

    report = check.run_once()

    assert (report.result, report.state, report.reasons) == ("ok", "healthy", ())
    assert _one_hot(metrics.shioaji_trade_cache_health_state, TRADE_CACHE_HEALTH_STATES) == {
        "healthy": 1.0,
        "unknown": 0.0,
        "degraded": 0.0,
        "check_failed": 0.0,
        "unavailable": 0.0,
    }
    assert _value(metrics.shioaji_trade_cache_health_checks_total, "_total", result="ok") == 1.0
    assert _value(metrics.shioaji_trade_cache_health_last_success_ts) == NOW


def test_degraded_trade_cache_exports_each_reason_and_logs_a_warning(metrics, module_log_sink):
    events = module_log_sink(ops_checks)
    api = FakeApi(health=_health("Degraded", ("FORDER", "SequenceGap"), ("FDEAL", "PendingReport")))
    check = TradeCacheHealthCheck(FakeClient(api, metrics=metrics))

    report = check.run_once()

    assert report.state == "degraded"
    assert report.reasons == (("FDEAL", "PendingReport"), ("FORDER", "SequenceGap"))
    reasons = metrics.shioaji_trade_cache_health_reasons
    assert _value(reasons, event_type="FORDER", reason="SequenceGap") == 1.0
    assert _value(reasons, event_type="FDEAL", reason="PendingReport") == 1.0
    degraded = [e for e in events if e["event"] == "trade_cache_health_degraded"]
    assert [e["log_level"] for e in degraded] == ["warning"]
    assert degraded[0]["reasons"] == ["FDEAL:PendingReport", "FORDER:SequenceGap"]


def test_a_reason_the_sdk_stops_listing_goes_back_to_zero(metrics):
    api = FakeApi(health=_health("Degraded", ("FORDER", "SequenceGap")))
    check = TradeCacheHealthCheck(FakeClient(api, metrics=metrics))
    check.run_once()
    api.health = _health("Healthy")

    check.run_once()

    assert _value(metrics.shioaji_trade_cache_health_reasons, event_type="FORDER", reason="SequenceGap") == 0.0
    assert _one_hot(metrics.shioaji_trade_cache_health_state, TRADE_CACHE_HEALTH_STATES)["healthy"] == 1.0


def test_an_unchanged_answer_is_logged_once_not_every_probe(metrics, module_log_sink):
    events = module_log_sink(ops_checks)
    api = FakeApi(health=_health("Degraded", ("FORDER", "SequenceGap")))
    check = TradeCacheHealthCheck(FakeClient(api, metrics=metrics))

    for _ in range(3):
        check.run_once()

    assert len([e for e in events if e["event"] == "trade_cache_health_degraded"]) == 1
    assert _value(metrics.shioaji_trade_cache_health_checks_total, "_total", result="ok") == 3.0


def test_unknown_state_is_exported_as_unknown_and_not_as_a_failure(metrics, module_log_sink):
    events = module_log_sink(ops_checks)
    api = FakeApi(health=_health("Unknown", ("FDEAL", "NoBaseline")))
    check = TradeCacheHealthCheck(FakeClient(api, metrics=metrics))

    report = check.run_once()

    assert (report.result, report.state) == ("ok", "unknown")
    assert _one_hot(metrics.shioaji_trade_cache_health_state, TRADE_CACHE_HEALTH_STATES)["unknown"] == 1.0
    assert [e["log_level"] for e in events if e["event"] == "trade_cache_health_unknown"] == ["info"]


def test_probe_error_sets_check_failed_clears_reasons_and_never_raises(metrics):
    api = FakeApi(health=_health("Degraded", ("FORDER", "SequenceGap")))
    check = TradeCacheHealthCheck(FakeClient(api, metrics=metrics))
    check.run_once()
    api.health_error = RuntimeError(f"session lost person_id={PERSON_ID}")

    report = check.run_once()

    assert (report.result, report.state) == ("failed", "check_failed")
    assert PERSON_ID not in (report.error or "")
    assert _one_hot(metrics.shioaji_trade_cache_health_state, TRADE_CACHE_HEALTH_STATES)["check_failed"] == 1.0
    assert _value(metrics.shioaji_trade_cache_health_reasons, event_type="FORDER", reason="SequenceGap") == 0.0
    assert _value(metrics.shioaji_trade_cache_health_checks_total, "_total", result="failed") == 1.0


def test_probe_timeout_is_check_failed(metrics):
    client = FakeClient(
        FakeApi(health=_health("Healthy")), metrics=metrics, guard_result=(False, None, TimeoutError("slow"), True)
    )

    report = TradeCacheHealthCheck(client).run_once()

    assert (report.state, report.error) == ("check_failed", "timeout")
    assert client.guard_calls == [("trade_cache_health", ops_checks.TRADE_CACHE_HEALTH_TIMEOUT_S)]


def test_sdk_without_trade_cache_health_is_reported_unavailable_not_failed(metrics, module_log_sink):
    events = module_log_sink(ops_checks)

    class OldApi:
        futopt_account = "FUT-ACCT"

    report = TradeCacheHealthCheck(FakeClient(OldApi(), metrics=metrics)).run_once()

    assert (report.result, report.state) == ("unavailable", "unavailable")
    assert _one_hot(metrics.shioaji_trade_cache_health_state, TRADE_CACHE_HEALTH_STATES)["unavailable"] == 1.0
    assert _value(metrics.shioaji_trade_cache_health_checks_total, "_total", result="unavailable") == 1.0
    assert [e["log_level"] for e in events if e["event"] == "trade_cache_health_unavailable"] == ["warning"]


def test_probe_without_a_session_is_skipped_and_leaves_the_last_state_alone(metrics):
    api = FakeApi(health=_health("Healthy"))
    client = FakeClient(api, metrics=metrics)
    check = TradeCacheHealthCheck(client)
    check.run_once()
    client.logged_in = False

    report = check.run_once()

    assert (report.result, report.state) == ("skipped", None)
    assert _one_hot(metrics.shioaji_trade_cache_health_state, TRADE_CACHE_HEALTH_STATES)["healthy"] == 1.0
    assert _value(metrics.shioaji_trade_cache_health_checks_total, "_total", result="skipped") == 1.0
    assert [name for name, _ in api.calls] == ["trade_cache_health"]


def test_probe_without_any_account_is_skipped(metrics):
    api = FakeApi(health=_health("Healthy"), futopt_account=None, stock_account=None)

    report = read_trade_cache_health(FakeClient(api, metrics=metrics))

    assert (report.result, report.state) == ("skipped", None)
    assert api.calls == []


def test_probe_asks_about_the_futures_account_and_falls_back_to_stock(metrics):
    api = FakeApi(health=_health("Healthy"))
    read_trade_cache_health(FakeClient(api))
    stock_only = FakeApi(health=_health("Healthy"), futopt_account=None)
    read_trade_cache_health(FakeClient(stock_only))

    assert api.calls == [("trade_cache_health", "FUT-ACCT")]
    assert stock_only.calls == [("trade_cache_health", "STK-ACCT")]


def test_probe_takes_no_action_on_the_session_or_the_orders(metrics):
    api = FakeApi(health=_health("Degraded", ("FORDER", "SequenceGap")))

    TradeCacheHealthCheck(FakeClient(api, metrics=metrics)).run_once()

    assert [name for name, _ in api.calls] == ["trade_cache_health"]


def test_an_unrecognised_sdk_state_is_check_failed_not_a_silent_healthy(metrics):
    api = FakeApi(health=_health("Frobnicated"))

    report = TradeCacheHealthCheck(FakeClient(api, metrics=metrics)).run_once()

    assert (report.result, report.state) == ("failed", "check_failed")
    assert report.error == "unexpected_state:Frobnicated"


def test_sdk_supplied_label_text_is_reduced_to_a_bounded_safe_label(metrics):
    api = FakeApi(health=_health("Degraded", ("bad label!", "x" * 80), ("", "NoBaseline")))

    report = TradeCacheHealthCheck(FakeClient(api, metrics=metrics)).run_once()

    assert ("badlabel", "x" * 32) in report.reasons
    assert ("other", "NoBaseline") in report.reasons


def test_a_gauge_write_failure_never_escapes_the_probe():
    broken = MagicMock()
    broken.shioaji_trade_cache_health_checks_total.labels.side_effect = RuntimeError("registry gone")

    report = TradeCacheHealthCheck(FakeClient(FakeApi(health=_health("Healthy")), metrics=broken)).run_once()

    assert report.state == "healthy"


def test_facade_exposes_the_probe_as_a_state_label(tmp_path, metrics):
    from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")
    with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
        mock_sj.Shioaji.return_value = MagicMock()
        facade = ShioajiClientFacade(str(cfg), {})
    api = FakeApi(health=_health("Degraded", ("FORDER", "SequenceGap")))
    facade._client.api = api
    facade._client.logged_in = True
    facade._client.metrics = metrics

    assert facade.check_trade_cache_health() == "degraded"
    assert [name for name, _ in api.calls] == ["trade_cache_health"]

    facade._client.logged_in = False
    assert facade.check_trade_cache_health() is None


# ---------------------------------------------------------------------------
# The real 1.7.11 signatures this module is written against
# ---------------------------------------------------------------------------


def test_installed_shioaji_has_both_methods_with_the_signatures_the_checks_call():
    sj = pytest.importorskip("shioaji")

    assert list(inspect.signature(sj.Shioaji.get_ca_expiretime).parameters) == ["self", "person_id"]
    assert list(inspect.signature(sj.Shioaji.trade_cache_health).parameters) == ["self", "account"]
    # The state labels this platform exports are keyed on these exact SDK values.
    assert {name: getattr(sj.TradeCacheHealthState, name).value for name in ("Healthy", "Unknown", "Degraded")} == {
        "Healthy": "Healthy",
        "Unknown": "Unknown",
        "Degraded": "Degraded",
    }
