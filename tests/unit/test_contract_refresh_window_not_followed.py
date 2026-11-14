"""The facade's own symbol rebuild must not follow the futures ``reference`` that the cache now carries.

The hourly cache keeps the nearest TXF future's ``reference`` so the connection pool's options roll and the
operator CLI can centre the TXO strike window on the underlying. The facade's own rebuild
(``refresh_contracts_and_symbols`` -> ``build_symbols`` -> ``_load_config``) is different: nothing in that path
moves live subscriptions to a different strike window. ``_resubscribe_all`` unsubscribes only symbols that are
still in ``client.symbols``, and the default ``diff`` policy resubscribes only when a *relevant contract* was
listed or delisted. A rebuild that re-centred the window on a moving reference would leave ``client.symbols``
naming strikes the broker is not serving (and the old strikes served but unnamed), most visibly when an unrelated
listing makes the contract-code diff non-empty while ``relevant_count`` stays zero.

So the rebuild is given the futures rows without ``reference`` (what it always saw), while the cache file keeps it.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.config import symbols as config_symbols
from hft_platform.feed_adapter.shioaji._infra import reset_login_slot_for_tests
from hft_platform.feed_adapter.shioaji.client import ShioajiClient

EXPIRY = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y/%m/%d")
STRIKES = (17500, 18000, 18500, 19000, 19500, 20000)
# Six strikes: the median strike is 19000, so a window without an underlying is 18500/19000/19500.
MEDIAN_WINDOW = {f"TXO{strike}{right}7" for strike in (18500, 19000, 19500) for right in ("C", "P")}
LIST_LINE = "OPT@TXO@near@ATM+/-1 exchange=OPT tags=options|near_month|atm\n"
FIRST_REFERENCE = 18500.0  # centred on this the window would be 18000/18500/19000
MOVED_REFERENCE = 19500.0  # centred on this it would be 19000/19500/20000


@pytest.fixture(autouse=True)
def _clean_login_slot():
    reset_login_slot_for_tests()
    yield
    reset_login_slot_for_tests()


def _option(strike: int, right: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        code=f"TXO{strike}{right}7",
        symbol=f"TXO{strike}{right}7",
        category="TXO",
        delivery_date=EXPIRY,
        strike_price=strike,
        option_right=name,
    )


def _future(reference: float) -> SimpleNamespace:
    return SimpleNamespace(code="TXFB7", symbol="TXF202702", category="TXF", delivery_date=EXPIRY, reference=reference)


def _codes(symbols: list[dict]) -> set[str]:
    return {s["code"] for s in symbols}


def _refresh(client: ShioajiClient) -> None:
    client._contracts_runtime.refresh_contracts_and_symbols()


@pytest.fixture
def live_client(tmp_path: Path):
    (tmp_path / "symbols.list").write_text(LIST_LINE, encoding="utf-8")
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols: []\n", encoding="utf-8")
    with patch("hft_platform.feed_adapter.shioaji.client.sj"):
        client = ShioajiClient(config_path=str(cfg))
        client.api = MagicMock()
        client.api.Contracts.Stocks.TSE = []
        client.api.Contracts.Stocks.OTC = []
        client.api.Contracts.Futures = [_future(FIRST_REFERENCE)]
        client.api.Contracts.Options = [
            _option(strike, right, name) for strike in STRIKES for right, name in (("C", "Call"), ("P", "Put"))
        ]
        client._contract_cache_path = str(tmp_path / "contracts.json")
        client._contract_fetch_stagger_gap_s = 0.0
        client._contract_refresh_lock = threading.Lock()
        client._contract_refresh_resubscribe_policy = "diff"
        client.tick_callback = lambda *args, **kwargs: None
        client.logged_in = True
        # The first refresh seeds the cache and the symbol file; what it loads is what is subscribed.
        _refresh(client)
        client.subscribed_codes.clear()
        client.subscribed_codes.update(_codes(client.symbols))
        client.subscribed_count = len(client.subscribed_codes)
        try:
            yield client
        finally:
            client.close()


def test_rebuild_starts_from_the_window_without_an_underlying(live_client: ShioajiClient):
    """The control that fixes what 'the list it always produced' means for this data."""
    assert _codes(live_client.symbols) == MEDIAN_WINDOW


def test_rebuild_is_not_given_the_future_reference_but_the_cache_keeps_it(live_client: ShioajiClient):
    live_client.api.Contracts.Futures = [_future(MOVED_REFERENCE)]
    seen: list[list[dict]] = []
    real_build = config_symbols.build_symbols

    def spy(list_path, contract_index=None, *args, **kwargs):
        seen.append(list(contract_index.contracts))
        return real_build(list_path, contract_index, *args, **kwargs)

    with patch("hft_platform.config.symbols.build_symbols", side_effect=spy):
        _refresh(live_client)

    assert len(seen) == 1
    futures_given = [c for c in seen[0] if c.get("type") == "future"]
    assert futures_given and all("reference" not in c for c in futures_given)
    cached = json.loads(Path(live_client._contract_cache_path).read_text(encoding="utf-8"))["contracts"]
    cached_future = next(c for c in cached if c["code"] == "TXFB7")
    assert cached_future["reference"] == MOVED_REFERENCE  # the pool's options roll reads it from here
    # nothing else about any row differs between what the rebuild saw and what the cache holds
    cached_by_code = {c["code"]: c for c in cached}
    for given in seen[0]:
        assert given == {k: v for k, v in cached_by_code[given["code"]].items() if k != "reference"}


def test_reference_move_with_an_unrelated_listing_keeps_symbols_and_subscriptions_in_step(
    live_client: ShioajiClient,
):
    """The bypass: an unrelated listing makes the contract-code diff non-empty, relevant_count stays zero."""
    live_client.api.Contracts.Futures = [_future(MOVED_REFERENCE)]
    live_client.api.Contracts.Stocks.TSE = [
        SimpleNamespace(code="9999", symbol="9999", category="", delivery_date="", name="unrelated")
    ]
    with (
        patch.object(live_client, "_subscribe_symbol", return_value=True) as subscribe,
        patch.object(live_client, "_unsubscribe_symbol") as unsubscribe,
    ):
        _refresh(live_client)

    diff = live_client._contract_refresh_last_diff
    assert diff["added_count"] > 0 and diff["relevant_count"] == 0  # the case under test is really reached
    assert _codes(live_client.symbols) == live_client.subscribed_codes == MEDIAN_WINDOW
    assert subscribe.call_count == 0
    assert unsubscribe.call_count == 0


def test_symbol_file_keeps_the_window_the_subscriptions_follow(live_client: ShioajiClient):
    live_client.api.Contracts.Futures = [_future(MOVED_REFERENCE)]

    _refresh(live_client)

    on_disk = yaml.safe_load(Path(live_client.config_path).read_text(encoding="utf-8"))["symbols"]
    assert _codes(on_disk) == MEDIAN_WINDOW
    assert _codes(live_client.symbols) == MEDIAN_WINDOW


def test_listing_a_contract_inside_the_window_is_adopted_as_before(live_client: ShioajiClient):
    """The refresh keeps adopting a rebuilt list whose contract set changed."""
    live_client.api.Contracts.Options = [
        *live_client.api.Contracts.Options,
        _option(18750, "C", "Call"),
        _option(18750, "P", "Put"),
    ]

    _refresh(live_client)

    assert "TXO18750C7" in _codes(live_client.symbols)
    assert _codes(live_client.symbols) != MEDIAN_WINDOW


def test_delisting_a_subscribed_contract_is_adopted_and_resubscribed_as_before(live_client: ShioajiClient):
    live_client.api.Contracts.Options = [o for o in live_client.api.Contracts.Options if o.strike_price != 19000]
    with patch.object(live_client, "_resubscribe_all") as resubscribe:
        _refresh(live_client)

    assert not any("19000" in code for code in _codes(live_client.symbols))
    assert live_client._contract_refresh_last_diff["relevant_count"] > 0
    assert resubscribe.call_count == 1
