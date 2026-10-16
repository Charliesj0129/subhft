"""A moved TXO strike window must move the live subscriptions with it.

Carrying the futures ``reference`` into the hourly cache makes the rebuilt option window follow the
underlying. The window can then change while the broker's contract-code set does not (a new trading day
re-bases ``reference``; no contract is listed or delisted). The refresh used to decide to resubscribe
only from the contract-code delta, and ``_load_config`` had already replaced ``client.symbols``: the new
ATM strikes were never subscribed and the old ones were never released, while the refresh reported ok.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.shioaji._infra import reset_login_slot_for_tests
from hft_platform.feed_adapter.shioaji.client import ShioajiClient

EXPIRY = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y/%m/%d")
STRIKES = (17500, 18000, 18500, 19000, 19500)


@pytest.fixture(autouse=True)
def _clean_login_slot():
    reset_login_slot_for_tests()
    yield
    reset_login_slot_for_tests()


def _window(strikes: tuple[int, ...]) -> list[dict]:
    return [
        {"code": f"TXO{strike}{right}7", "exchange": "TAIFEX", "product_type": "option"}
        for strike in strikes
        for right in ("C", "P")
    ]


def _codes(symbols: list[dict]) -> set[str]:
    return {s["code"] for s in symbols}


def _broker_contracts():
    futures = [SimpleNamespace(code="TXFB7", symbol="TXF202702", category="TXF", delivery_date=EXPIRY, reference=18500.0)]
    options = [
        SimpleNamespace(
            code=f"TXO{strike}{right}7",
            symbol=f"TXO{strike}{right}7",
            category="TXO",
            delivery_date=EXPIRY,
            strike_price=strike,
            option_right=name,
        )
        for strike in STRIKES
        for right, name in (("C", "Call"), ("P", "Put"))
    ]
    return futures, options


def _refresh_once(client: ShioajiClient, rebuilt: list[dict]) -> None:
    with patch("hft_platform.config.symbols.build_symbols", return_value=MagicMock(symbols=rebuilt, errors=[])):
        client._contracts_runtime.refresh_contracts_and_symbols()


@pytest.fixture
def live_client(tmp_path: Path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols: []\n", encoding="utf-8")
    with patch("hft_platform.feed_adapter.shioaji.client.sj"):
        client = ShioajiClient(config_path=str(cfg))
        futures, options = _broker_contracts()
        client.api = MagicMock()
        client.api.Contracts.Stocks.TSE = []
        client.api.Contracts.Stocks.OTC = []
        client.api.Contracts.Futures = futures
        client.api.Contracts.Options = options
        client._contract_cache_path = str(tmp_path / "contracts.json")
        client._contract_fetch_stagger_gap_s = 0.0
        client._contract_refresh_lock = threading.Lock()
        client._contract_refresh_resubscribe_policy = "diff"
        client.tick_callback = lambda *args, **kwargs: None
        client.logged_in = True
        # First refresh seeds the cache and the symbol file with the old window.
        _refresh_once(client, _window((18000, 18500, 19000)))
        client.subscribed_codes.clear()
        client.subscribed_codes.update(_codes(client.symbols))
        client.subscribed_count = len(client.subscribed_codes)
        try:
            yield client
        finally:
            client.close()


def test_moved_window_unsubscribes_dropped_strikes_and_subscribes_new_ones(live_client: ShioajiClient):
    old_codes = _codes(live_client.symbols)
    new_window = _window((18500, 19000, 19500))
    with (
        patch.object(live_client, "_subscribe_symbol", return_value=True) as subscribe,
        patch.object(live_client, "_unsubscribe_symbol") as unsubscribe,
    ):
        _refresh_once(live_client, new_window)

    assert _codes(live_client.symbols) == _codes(new_window)
    assert {call.args[0]["code"] for call in unsubscribe.call_args_list} == old_codes - _codes(new_window)
    assert {call.args[0]["code"] for call in subscribe.call_args_list} == _codes(new_window) - old_codes
    assert live_client.subscribed_codes == _codes(new_window)
    assert live_client.subscribed_count == len(_codes(new_window))


def test_unchanged_window_touches_no_subscription(live_client: ShioajiClient):
    same_window = _window((18000, 18500, 19000))
    with (
        patch.object(live_client, "_subscribe_symbol", return_value=True) as subscribe,
        patch.object(live_client, "_unsubscribe_symbol") as unsubscribe,
    ):
        _refresh_once(live_client, same_window)

    assert subscribe.call_count == 0
    assert unsubscribe.call_count == 0
    assert live_client.subscribed_codes == _codes(same_window)


def test_policy_none_leaves_subscriptions_alone_even_when_the_window_moves(live_client: ShioajiClient):
    """``none`` is the operator's off switch for refresh-driven subscription changes."""
    live_client._contract_refresh_resubscribe_policy = "none"
    old_codes = set(live_client.subscribed_codes)
    with (
        patch.object(live_client, "_subscribe_symbol", return_value=True) as subscribe,
        patch.object(live_client, "_unsubscribe_symbol") as unsubscribe,
    ):
        _refresh_once(live_client, _window((18500, 19000, 19500)))

    assert subscribe.call_count == 0
    assert unsubscribe.call_count == 0
    assert live_client.subscribed_codes == old_codes


def test_moved_window_over_the_subscription_limit_is_reported_not_raised(live_client: ShioajiClient):
    live_client.MAX_SUBSCRIPTIONS = len(live_client.subscribed_codes)
    old_codes = set(live_client.subscribed_codes)
    with (
        patch.object(live_client, "_subscribe_symbol", return_value=True),
        patch.object(live_client, "_unsubscribe_symbol"),
    ):
        _refresh_once(live_client, _window((18500, 19000, 19500, 20000)))

    # The refresh finished (status written, lock released) instead of dying on the limit.
    assert live_client._contract_refresh_lock.acquire(blocking=False)
    live_client._contract_refresh_lock.release()
    assert live_client.subscribed_count == len(live_client.subscribed_codes)
    assert old_codes - live_client.subscribed_codes  # the dropped strikes were released first
