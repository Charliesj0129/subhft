"""The hourly contract refresh must report a failed fetch as a failure.

Production ran for a long time with every hourly refresh on all four pooled
facades failing, while logging "Contract data refreshed from broker" and
incrementing ``contract_refresh_total{result="ok"}`` each time. Three bugs
stacked up:

* ``_ensure_contracts`` called ``api.fetch_contracts`` directly, bypassing the
  ``InflightGuard``, so the four facades entered the SDK's Rust ``_core``
  together and it aborted them with "exclusive access lost";
* it swallowed the exception and returned ``contracts_ready``, which is
  ``hasattr(api, "Contracts")`` — always True once logged in;
* the caller ignored the return value and accepted the ``Fetched`` status the
  *login-time* fetch had left behind.

These tests pin each half of the repair: the fetch reports its own failure, and
the refresh routine stops claiming success on top of it.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import structlog.testing
import yaml

from hft_platform.feed_adapter.shioaji._infra import reset_login_slot_for_tests
from hft_platform.feed_adapter.shioaji.contracts_runtime import ContractsRuntime


@pytest.fixture(autouse=True)
def _clean_login_slot():
    reset_login_slot_for_tests()
    yield
    reset_login_slot_for_tests()


# --------------------------------------------------------------------------- #
# _ensure_contracts                                                            #
# --------------------------------------------------------------------------- #


def _client(tmp_path: Path):
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

    cfg = tmp_path / "symbols.yaml"
    cfg.write_text(yaml.dump({"symbols": [{"code": "2330", "exchange": "TSE"}]}))
    c = ShioajiClient(config_path=str(cfg))
    c.metrics = MagicMock()
    return c


def test_ensure_contracts_returns_false_when_sdk_call_fails(tmp_path: Path):
    """A raising fetch is a failure even though api.Contracts still exists."""
    c = _client(tmp_path)
    api = MagicMock()
    api.Contracts = MagicMock()  # login-time contracts are still attached
    api.fetch_contracts = MagicMock(side_effect=RuntimeError("exclusive access lost"))
    c.api = api

    assert c.contracts_ready is True  # the old return value — always True
    assert c._ensure_contracts() is False


def test_ensure_contracts_records_error_latency_when_sdk_call_fails(tmp_path: Path):
    c = _client(tmp_path)
    api = MagicMock()
    api.Contracts = MagicMock()
    api.fetch_contracts = MagicMock(side_effect=RuntimeError("exclusive access lost"))
    c.api = api
    c._record_api_latency = MagicMock()

    c._ensure_contracts()

    assert c._record_api_latency.call_args[0][0] == "fetch_contracts"
    assert c._record_api_latency.call_args[1]["ok"] is False


def test_ensure_contracts_is_refused_while_a_previous_worker_is_abandoned(tmp_path: Path):
    """The fetch must go through the InflightGuard like every other SDK call.

    Calling ``api.fetch_contracts`` directly is what let four facades enter the
    non-reentrant SDK object at once.
    """
    c = _client(tmp_path)
    api = MagicMock()
    api.Contracts = MagicMock()
    api.fetch_contracts = MagicMock()
    c.api = api
    c._sdk_busy_grace_s = 0.0
    # A previous call timed out and its worker is still inside the SDK.
    c._sdk_inflight.record_abandoned("login", threading.Event())

    assert c._ensure_contracts() is False
    api.fetch_contracts.assert_not_called()


def test_ensure_contracts_returns_true_on_a_clean_fetch(tmp_path: Path):
    c = _client(tmp_path)
    api = MagicMock()
    api.Contracts = MagicMock()
    api.fetch_contracts = MagicMock()
    c.api = api

    assert c._ensure_contracts() is True
    api.fetch_contracts.assert_called_once_with(contract_download=True)


# --------------------------------------------------------------------------- #
# refresh_contracts_and_symbols                                                #
# --------------------------------------------------------------------------- #


def _refresh_client(tmp_path: Path, *, cached_contracts: list[dict] | None = None):
    """A minimal client wired for refresh_contracts_and_symbols()."""
    cache_path = tmp_path / "contracts.json"
    cache_path.write_text(
        json.dumps({"cache_version": 1, "contracts": cached_contracts or [{"code": "2330"}]}),
        encoding="utf-8",
    )

    client = MagicMock()
    client.api = MagicMock()
    client._contract_cache_path = str(cache_path)
    client._contract_refresh_status_path = str(tmp_path / "status.json")
    client._contract_refresh_lock = threading.Lock()
    client._contract_refresh_version = 0
    client._contract_refresh_last_diff = {}
    client._contract_refresh_resubscribe_policy = "none"
    client._contract_refresh_s = 3600.0
    client._contract_refresh_running = True
    client._contract_refresh_thread = None
    client._contract_fetch_stagger_gap_s = 0.0
    client._contract_fetch_stagger_timeout_s = 5.0
    client.config_path = str(tmp_path / "symbols.yaml")
    client.metrics = MagicMock()
    client.logged_in = False
    client.symbols = []
    client.subscribed_codes = set()
    return client


def test_contract_refresh_reports_failure_when_fetch_fails(tmp_path: Path):
    """A failed fetch must not be logged or counted as a successful refresh."""
    client = _refresh_client(tmp_path)
    client._ensure_contracts.return_value = False
    runtime = ContractsRuntime(client)

    with structlog.testing.capture_logs() as logs:
        runtime.refresh_contracts_and_symbols()

    events = [entry.get("event") for entry in logs]
    assert "contract_refresh_fetch_failed" in events
    assert "Contract data refreshed from broker" not in events

    results = [call.kwargs.get("result") for call in client.metrics.contract_refresh_total.labels.call_args_list]
    assert "fetch_failed" in results
    assert "ok" not in results

    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["result"] == "fetch_failed"


def test_contract_refresh_releases_its_lock_when_the_fetch_fails(tmp_path: Path):
    """A failure path that leaks the lock would wedge every later refresh."""
    client = _refresh_client(tmp_path)
    client._ensure_contracts.return_value = False
    runtime = ContractsRuntime(client)

    runtime.refresh_contracts_and_symbols()

    assert client._contract_refresh_lock.locked() is False


def test_contract_refresh_flags_a_status_that_was_already_fetched(tmp_path: Path):
    """``Fetched`` that never left ``Fetched`` proves nothing was re-read."""
    client = _refresh_client(tmp_path)
    client._ensure_contracts.return_value = True
    client.api.Contracts.status = "FetchStatus.Fetched"
    runtime = ContractsRuntime(client)

    with structlog.testing.capture_logs() as logs:
        runtime.refresh_contracts_and_symbols()

    ready = [entry for entry in logs if entry.get("event") == "Contract fetch ready"]
    assert ready, "the readiness log must still be emitted"
    assert ready[0]["status_assumed"] is True
    assert ready[0]["status_before"] == "FetchStatus.Fetched"


def test_contract_refresh_does_not_flag_a_status_that_changed(tmp_path: Path):
    """A status that moved off its previous value is a genuinely observed fetch."""
    client = _refresh_client(tmp_path)
    client._ensure_contracts.return_value = True
    statuses = iter(["FetchStatus.Unfetch", "FetchStatus.Fetched", "FetchStatus.Fetched"])

    class _Contracts:
        @property
        def status(self):
            return next(statuses, "FetchStatus.Fetched")

    client.api.Contracts = _Contracts()
    runtime = ContractsRuntime(client)

    with structlog.testing.capture_logs() as logs:
        runtime.refresh_contracts_and_symbols()

    ready = [entry for entry in logs if entry.get("event") == "Contract fetch ready"]
    assert ready
    assert ready[0]["status_assumed"] is False


def test_contract_refresh_takes_the_shared_slot_around_the_fetch(tmp_path: Path):
    """Serialising the pooled facades is what stops "exclusive access lost".

    The slot must be held across the fetch and released afterwards, so the four
    facades' hourly refreshes queue instead of colliding inside the SDK.
    """
    client = _refresh_client(tmp_path)
    calls: list[str] = []
    client._ensure_contracts.side_effect = lambda: calls.append("fetch") or True

    with (
        patch(
            "hft_platform.feed_adapter.shioaji.contracts_runtime.acquire_login_slot",
            side_effect=lambda **kw: calls.append("acquire") or True,
        ),
        patch(
            "hft_platform.feed_adapter.shioaji.contracts_runtime.release_login_slot",
            side_effect=lambda: calls.append("release"),
        ),
    ):
        ContractsRuntime(client).refresh_contracts_and_symbols()

    assert calls == ["acquire", "fetch", "release"]


def test_contract_refresh_does_not_release_a_slot_it_never_held(tmp_path: Path):
    """acquire_login_slot returning False means "proceed unserialised"."""
    client = _refresh_client(tmp_path)
    released: list[str] = []

    with (
        patch(
            "hft_platform.feed_adapter.shioaji.contracts_runtime.acquire_login_slot",
            return_value=False,
        ),
        patch(
            "hft_platform.feed_adapter.shioaji.contracts_runtime.release_login_slot",
            side_effect=lambda: released.append("release"),
        ),
    ):
        ContractsRuntime(client).refresh_contracts_and_symbols()

    client._ensure_contracts.assert_called_once_with()
    assert released == []


def test_contract_cache_age_gauge_moves_only_on_a_real_cache_write(tmp_path: Path):
    """The freshness gauge is the one signal a stuck cache cannot fake."""
    client = _refresh_client(tmp_path)
    client._ensure_contracts.return_value = False
    ContractsRuntime(client).refresh_contracts_and_symbols()
    assert client.metrics.contract_cache_last_success_ts.set.call_count == 0

    client = _refresh_client(tmp_path, cached_contracts=[{"code": "2330", "exchange": "TSE", "type": "stock"}])
    client._ensure_contracts.return_value = True
    client.api.Contracts.Stocks.TSE = [SimpleNamespace(code="2330", symbol="2330", name="TSMC")]
    client.api.Contracts.Stocks.OTC = []
    client.api.Contracts.Futures.keys.return_value = []
    client.api.Contracts.Options.keys.return_value = []

    with patch("hft_platform.config.symbols.write_contract_cache"):
        with patch("hft_platform.config.symbols.build_symbols") as build:
            build.return_value = MagicMock(symbols=[], errors=[])
            ContractsRuntime(client).refresh_contracts_and_symbols()

    assert client.metrics.contract_cache_last_success_ts.set.call_count == 1
