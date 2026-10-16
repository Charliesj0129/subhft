"""A refresh that only re-centres the TXO window must not leave ``client.symbols`` ahead of the subscriptions.

Carrying the futures ``reference`` into the hourly cache makes the rebuilt option window follow the
underlying. The window can then change while the broker's contract-code set does not (a new trading day
re-bases ``reference``; no contract is listed or delisted). Nothing in the refresh moves the live
subscriptions in that case, and ``_resubscribe_all`` only unsubscribes symbols that are still in
``client.symbols``. Adopting the new list would name strikes that are not subscribed and leave the old
strikes subscribed but unnamed (and, after a reconnect, forgotten by ``subscribed_codes.clear()`` yet still
held by the broker). So the list in force is kept and the symbol file carries the new window to the next start.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.feed_adapter.shioaji import contracts_runtime
from hft_platform.feed_adapter.shioaji._infra import reset_login_slot_for_tests
from hft_platform.feed_adapter.shioaji.client import ShioajiClient

EXPIRY = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y/%m/%d")
STRIKES = (17500, 18000, 18500, 19000, 19500, 20000)
OLD_WINDOW_STRIKES = (18000, 18500, 19000)
NEW_WINDOW_STRIKES = (18500, 19000, 19500, 20000)
HELD_EVENT = "contract_refresh_window_move_held"


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


def _option(strike: int, right: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        code=f"TXO{strike}{right}7",
        symbol=f"TXO{strike}{right}7",
        category="TXO",
        delivery_date=EXPIRY,
        strike_price=strike,
        option_right=name,
    )


def _broker_contracts():
    futures = [
        SimpleNamespace(code="TXFB7", symbol="TXF202702", category="TXF", delivery_date=EXPIRY, reference=18500.0)
    ]
    options = [_option(strike, right, name) for strike in STRIKES for right, name in (("C", "Call"), ("P", "Put"))]
    return futures, options


def _refresh_once(client: ShioajiClient, rebuilt: list[dict]) -> None:
    with patch("hft_platform.config.symbols.build_symbols", return_value=MagicMock(symbols=rebuilt, errors=[])):
        client._contracts_runtime.refresh_contracts_and_symbols()


def _held_logged(fake_logger: MagicMock) -> list:
    return [c for c in fake_logger.info.call_args_list if c.args and c.args[0] == HELD_EVENT]


@pytest.fixture
def fake_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    # Patch the module logger itself: structlog's capture_logs goes blind after the integration suite.
    fake = MagicMock()
    monkeypatch.setattr(contracts_runtime, "logger", fake)
    return fake


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
        _refresh_once(client, _window(OLD_WINDOW_STRIKES))
        client.subscribed_codes.clear()
        client.subscribed_codes.update(_codes(client.symbols))
        client.subscribed_count = len(client.subscribed_codes)
        try:
            yield client
        finally:
            client.close()


def test_window_moved_without_a_contract_change_keeps_the_symbols_in_force(
    live_client: ShioajiClient, fake_logger: MagicMock
):
    old_codes = _codes(live_client.symbols)
    with (
        patch.object(live_client, "_subscribe_symbol", return_value=True) as subscribe,
        patch.object(live_client, "_unsubscribe_symbol") as unsubscribe,
    ):
        _refresh_once(live_client, _window(NEW_WINDOW_STRIKES))

    assert _codes(live_client.symbols) == old_codes  # still what is subscribed
    assert live_client.subscribed_codes == old_codes
    assert subscribe.call_count == 0
    assert unsubscribe.call_count == 0
    held = _held_logged(fake_logger)
    assert len(held) == 1
    assert held[0].kwargs["added_count"] == 4  # the 19500 and 20000 calls and puts
    assert held[0].kwargs["removed_count"] == 2  # the 18000 call and put
    assert held[0].kwargs["added_sample"] == sorted(_codes(_window(NEW_WINDOW_STRIKES)) - old_codes)
    assert held[0].kwargs["removed_sample"] == sorted(old_codes - _codes(_window(NEW_WINDOW_STRIKES)))


def test_held_window_still_reaches_the_symbol_file_for_the_next_start(live_client: ShioajiClient):
    _refresh_once(live_client, _window(NEW_WINDOW_STRIKES))

    on_disk = yaml.safe_load(Path(live_client.config_path).read_text(encoding="utf-8"))["symbols"]
    assert _codes(on_disk) == _codes(_window(NEW_WINDOW_STRIKES))
    assert _codes(live_client.symbols) == _codes(_window(OLD_WINDOW_STRIKES))


def test_unchanged_window_is_not_reported_as_held(live_client: ShioajiClient, fake_logger: MagicMock):
    _refresh_once(live_client, _window(OLD_WINDOW_STRIKES))

    assert _held_logged(fake_logger) == []
    assert _codes(live_client.symbols) == _codes(_window(OLD_WINDOW_STRIKES))


@pytest.mark.parametrize("change", ["listed", "delisted"])
def test_moved_window_is_adopted_when_the_contract_set_changed(
    live_client: ShioajiClient, fake_logger: MagicMock, change: str
):
    """A listing or a delisting is the case the refresh always adopted; only the unlisted-move case is held."""
    live_client._contract_refresh_resubscribe_policy = "none"  # isolate adoption from any resubscribe
    options = list(live_client.api.Contracts.Options)
    if change == "listed":
        options.append(_option(20500, "C", "Call"))
    else:
        options = [o for o in options if o.strike_price != 17500]
    live_client.api.Contracts.Options = options

    _refresh_once(live_client, _window(NEW_WINDOW_STRIKES))

    assert _codes(live_client.symbols) == _codes(_window(NEW_WINDOW_STRIKES))
    assert _held_logged(fake_logger) == []


def test_policy_all_resubscribes_the_window_in_force_not_the_moved_one(live_client: ShioajiClient):
    """``_resubscribe_all`` unsubscribes only symbols still in ``client.symbols``: it must see the old window."""
    live_client._contract_refresh_resubscribe_policy = "all"
    old_codes = _codes(live_client.symbols)
    seen: list[set[str]] = []

    with patch.object(live_client, "_resubscribe_all", side_effect=lambda: seen.append(_codes(live_client.symbols))):
        _refresh_once(live_client, _window(NEW_WINDOW_STRIKES))

    assert seen == [old_codes]


def test_policy_none_also_keeps_the_symbols_in_force(live_client: ShioajiClient):
    live_client._contract_refresh_resubscribe_policy = "none"
    old_codes = _codes(live_client.symbols)
    with (
        patch.object(live_client, "_subscribe_symbol", return_value=True) as subscribe,
        patch.object(live_client, "_unsubscribe_symbol") as unsubscribe,
    ):
        _refresh_once(live_client, _window(NEW_WINDOW_STRIKES))

    assert _codes(live_client.symbols) == old_codes
    assert subscribe.call_count == 0
    assert unsubscribe.call_count == 0


def test_nothing_is_held_when_there_was_no_list_in_force(live_client: ShioajiClient, fake_logger: MagicMock):
    """An empty list in force has nothing to stay consistent with; the first rebuilt list must be adopted."""
    live_client.symbols = _window(NEW_WINDOW_STRIKES)
    live_client._contract_refresh_last_diff = {"added_count": 0, "removed_count": 0}

    live_client._contracts_runtime._hold_window_unless_contracts_changed([])

    assert _codes(live_client.symbols) == _codes(_window(NEW_WINDOW_STRIKES))
    assert _held_logged(fake_logger) == []
