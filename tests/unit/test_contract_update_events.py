"""The broker announces contract changes; this platform used to only poll.

shioaji 1.5.x pushes ``SYS/CONTRACT`` events (solace topic
``APISUB/V1/SYS/CONTRACT``) via ``set_contract_event_callback``, carrying
``action`` (FORCE|CHECK), ``security_type`` and ``check_file_ts``. The platform
never registered for them and instead called ``fetch_contracts`` hourly — a call
that requires sole ownership of the SDK's inner client and so failed on 100% of
cycles (56/56 observed on THESHOW 2026-07-30) against facades holding 74
permanently-registered subscriptions.

These tests pin the push path: that we register, that the handler is bounded and
non-throwing on a broker thread, and that it does *not* re-enter the SDK.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import structlog.testing

from hft_platform.feed_adapter.shioaji.contracts_runtime import (
    _CONTRACT_EVENT_ACTIONS,
    _CONTRACT_EVENT_SECURITY_TYPES,
    ContractsRuntime,
    _classify_contract_event_field,
)


class _Api:
    def __init__(self) -> None:
        self.registered: list[Any] = []

    def set_contract_event_callback(self, cb: Any) -> None:
        self.registered.append(cb)


def _runtime(api: Any = None, metrics: Any = None) -> ContractsRuntime:
    client = SimpleNamespace(
        api=api,
        metrics=metrics,
        _contract_update_last_event_s=0.0,
    )
    return ContractsRuntime(client)  # type: ignore[arg-type]


def _event(action: Any = "ContractAction.FORCE", security_type: Any = "ContractUpdateSecurityType.FUT", ts: Any = 1.5):
    return SimpleNamespace(action=action, security_type=security_type, check_file_ts=ts)


# --------------------------------------------------------------------------- #
# Registration                                                                 #
# --------------------------------------------------------------------------- #


def test_contract_event_callback_is_registered_on_the_api():
    api = _Api()
    runtime = _runtime(api)

    assert runtime.register_contract_event_callback() is True
    assert len(api.registered) == 1


def test_contract_event_registration_reports_failure_without_raising():
    """A broker that rejects the setter must not break contract startup."""

    class _Rejecting:
        def set_contract_event_callback(self, cb: Any) -> None:
            raise RuntimeError("Already borrowed")

    runtime = _runtime(_Rejecting())
    assert runtime.register_contract_event_callback() is False


def test_contract_event_registration_is_skipped_when_the_sdk_lacks_the_api():
    """Older SDK builds simply do not expose it — that is not an error."""
    runtime = _runtime(SimpleNamespace())  # no set_contract_event_callback
    assert runtime.register_contract_event_callback() is False


def test_contract_event_registration_deferred_without_an_api():
    assert _runtime(None).register_contract_event_callback() is False


# --------------------------------------------------------------------------- #
# Handler behaviour                                                            #
# --------------------------------------------------------------------------- #


def test_contract_update_event_records_timestamp_and_metric():
    metrics = SimpleNamespace(
        contract_update_events_total=MagicMock(),
        contract_update_last_event_ts=MagicMock(),
    )
    runtime = _runtime(_Api(), metrics)

    runtime._on_contract_update_event(_event())

    metrics.contract_update_events_total.labels.assert_called_once_with(action="force", security_type="fut")
    assert metrics.contract_update_last_event_ts.set.call_count == 1
    assert metrics.contract_update_last_event_ts.set.call_args[0][0] > 0
    assert runtime._client._contract_update_last_event_s > 0


def test_contract_update_event_logs_the_announced_fields():
    runtime = _runtime(_Api())

    with structlog.testing.capture_logs() as logs:
        runtime._on_contract_update_event(_event(action="ContractAction.CHECK", ts=1234.5))

    events = [e for e in logs if e.get("event") == "contract_update_event"]
    assert events
    assert events[0]["action"] == "check"
    assert events[0]["security_type"] == "fut"
    assert events[0]["check_file_ts"] == 1234.5


def test_contract_update_event_handler_never_raises_into_the_sdk():
    """It runs on a shioaji callback thread; an exception there kills the thread."""

    class _Exploding:
        @property
        def action(self):
            raise RuntimeError("boom")

    runtime = _runtime(_Api())
    runtime._on_contract_update_event(_Exploding())  # must not raise


def test_contract_update_event_does_not_re_enter_the_sdk():
    """Fetching from the SDK's own callback thread is the 'Already borrowed' bug."""
    api = MagicMock()
    runtime = _runtime(api)

    runtime._on_contract_update_event(_event())

    api.fetch_contracts.assert_not_called()


def test_contract_update_event_survives_a_missing_metrics_registry():
    runtime = _runtime(_Api(), metrics=None)
    runtime._on_contract_update_event(_event())
    assert runtime._client._contract_update_last_event_s > 0


# --------------------------------------------------------------------------- #
# Label bounding (cardinality discipline)                                      #
# --------------------------------------------------------------------------- #


def test_unknown_contract_event_values_collapse_to_other():
    """A new SDK enum variant must not mint an unbounded Prometheus series."""
    assert _classify_contract_event_field("ContractAction.SOMETHING_NEW", _CONTRACT_EVENT_ACTIONS) == "other"
    assert _classify_contract_event_field("weird", _CONTRACT_EVENT_SECURITY_TYPES) == "other"


def test_missing_contract_event_field_is_unknown_not_other():
    assert _classify_contract_event_field(None, _CONTRACT_EVENT_ACTIONS) == "unknown"


def test_contract_event_field_accepts_every_declared_sdk_variant():
    """Guard: the bounded sets must cover the SDK's declared enums."""
    for name in ("FORCE", "CHECK"):
        assert _classify_contract_event_field(f"ContractAction.{name}", _CONTRACT_EVENT_ACTIONS) == name.lower()
    for name in ("ALL", "IND", "STK", "FUT", "OPT"):
        got = _classify_contract_event_field(f"ContractUpdateSecurityType.{name}", _CONTRACT_EVENT_SECURITY_TYPES)
        assert got == name.lower()


def test_contract_event_field_reads_enum_name_attribute():
    """Real SDK enums expose ``.name``; prefer it over ``str()``."""
    assert _classify_contract_event_field(SimpleNamespace(name="FUT"), _CONTRACT_EVENT_SECURITY_TYPES) == "fut"
