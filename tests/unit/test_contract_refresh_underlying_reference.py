"""The hourly contract refresh must keep the futures ``reference`` price in the cache.

PR #571 centres the TXO strike window on the nearest TXF future's ``reference``.
On THESHOW the cache that feeds ``build_symbols`` is rewritten hourly by
``ContractsRuntime.refresh_contracts_and_symbols``, whose row normaliser dropped
``reference`` (only the operator-side ``contract_fetcher`` kept it). The cache
therefore held 0 ``reference`` keys in 56,547 rows and the fix had no runtime
data source: every build fell back to the median strike, exactly as before.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.config._symbols_expansion import pick_underlying_price
from hft_platform.config.symbols import load_contract_cache
from hft_platform.feed_adapter.shioaji._infra import reset_login_slot_for_tests
from hft_platform.feed_adapter.shioaji.contracts_runtime import ContractsRuntime

# Relative to today: a literal date turns every later run red once it passes.
EXPIRY = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y/%m/%d")
TXF_REFERENCE = 23456.0
OPTION_PREMIUM = 187.5


@pytest.fixture(autouse=True)
def _clean_login_slot():
    reset_login_slot_for_tests()
    yield
    reset_login_slot_for_tests()


def _refresh_client(tmp_path: Path):
    cache_path = tmp_path / "contracts.json"
    cache_path.write_text(json.dumps({"cache_version": 1, "contracts": [{"code": "2330"}]}), encoding="utf-8")
    client = MagicMock()
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
    client.api.Contracts.Stocks.TSE = []
    client.api.Contracts.Stocks.OTC = []
    client.api.Contracts.Futures = [
        SimpleNamespace(
            code="TXFJ6",
            symbol="TXF202610",
            name="TXF",
            category="TXF",
            delivery_date=EXPIRY,
            reference=TXF_REFERENCE,
        ),
    ]
    client.api.Contracts.Options = [
        SimpleNamespace(
            code="TXO23500J6",
            symbol="TXO23500J6",
            name="TXO",
            category="TXO",
            delivery_date=EXPIRY,
            strike_price=23500,
            option_right="Call",
            reference=OPTION_PREMIUM,
        ),
    ]
    return client


def _refreshed_rows(tmp_path: Path) -> dict[str, dict]:
    client = _refresh_client(tmp_path)
    ContractsRuntime(client).refresh_contracts_and_symbols()
    payload = json.loads(Path(client._contract_cache_path).read_text(encoding="utf-8"))
    return {row["code"]: row for row in payload["contracts"]}


def test_refresh_writes_the_future_reference_price_into_the_cache(tmp_path: Path):
    rows = _refreshed_rows(tmp_path)

    assert rows["TXFJ6"]["reference"] == TXF_REFERENCE


def test_refresh_does_not_cache_an_option_premium_as_a_reference(tmp_path: Path):
    """An option's ``reference`` is its premium; centring a strike window on it is the #571 bug."""
    rows = _refreshed_rows(tmp_path)

    assert "reference" not in rows["TXO23500J6"]


def test_underlying_is_picked_from_the_cache_the_refresh_wrote(tmp_path: Path):
    """End to end: refresh -> cache file -> ``load_contract_cache`` -> underlying for the TXO chain."""
    client = _refresh_client(tmp_path)
    ContractsRuntime(client).refresh_contracts_and_symbols()
    index = load_contract_cache(client._contract_cache_path, metrics_path=str(tmp_path / "no_metrics.json"))
    assert index is not None
    chain = index.options_by_root["TXO"]

    price, source = pick_underlying_price("TXO", chain, index)

    assert (price, source) == (TXF_REFERENCE, "TXF:TXFJ6")
