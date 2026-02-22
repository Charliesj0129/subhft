"""Integration tests using Shioaji simulation mode.

Requires SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY environment variables.
Skipped automatically when credentials are absent or shioaji is not installed.
"""

import os

import pytest

sj = pytest.importorskip("shioaji")

pytestmark = [pytest.mark.integration]

_API_KEY = os.getenv("SHIOAJI_API_KEY")
_SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY")
_SKIP_REASON = "SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY required"


@pytest.fixture(scope="module")
def sim_api():
    if not _API_KEY or not _SECRET_KEY:
        pytest.skip(_SKIP_REASON)
    api = sj.Shioaji(simulation=True)
    api.login(api_key=_API_KEY, secret_key=_SECRET_KEY, contracts_timeout=60000)
    yield api
    try:
        api.logout()
    except Exception:
        pass


def test_simulation_login(sim_api):
    assert sim_api is not None


def test_contracts_available(sim_api):
    assert hasattr(sim_api.Contracts, "Stocks")
    tse = sim_api.Contracts.Stocks.TSE
    assert "2330" in {c.code for c in tse}


def test_quote_callback_registration(sim_api):
    received = []

    def on_tick(exchange, tick):
        received.append(tick)

    sim_api.quote.set_on_tick_stk_v1_callback(on_tick)
    # Callback registered without error


def test_event_callback_registration(sim_api):
    events = []

    def on_event(resp_code, event_code, info, event):
        events.append((resp_code, event_code))

    sim_api.quote.set_event_callback(on_event)
    # Event callback registered without error


def test_subscribe_symbol(sim_api):
    contract = sim_api.Contracts.Stocks.TSE["2330"]
    sim_api.quote.subscribe(
        contract,
        quote_type=sj.constant.QuoteType.Tick,
        version=sj.constant.QuoteVersion.v1,
    )
    # Subscription completes without error
    sim_api.quote.unsubscribe(
        contract,
        quote_type=sj.constant.QuoteType.Tick,
        version=sj.constant.QuoteVersion.v1,
    )
