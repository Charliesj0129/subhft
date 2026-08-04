"""The hourly contract refresh must be done once per process, not once per facade.

2026-08-03 measurement on THESHOW: the four pooled facades each ran a full
read → walk → write of the *same* ``config/contracts.json`` (13 MB, 54 727
contracts) on every hourly cycle, producing identical diffs and spanning ~11 s
of largely GIL-held work per hour. That accounted for 17 of the 21 event-loop
stalls above 50 ms measured over a clean two-hour night session.

What must stay per-facade is the relevance half: each facade subscribes to its
own ~74-symbol shard, so ``relevant_count`` — and therefore the resubscribe
decision — differs even though the contract delta does not.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.shioaji import contracts_runtime
from hft_platform.feed_adapter.shioaji.client import ShioajiClient


@pytest.fixture(autouse=True)
def _clear_shared_rebuild():
    """The rebuild is a module global; without this it leaks between tests."""
    contracts_runtime._reset_shared_rebuild()
    yield
    contracts_runtime._reset_shared_rebuild()


class _CountingContracts(list):
    """A contract container that records how many times it was walked."""

    def __init__(self, items):
        super().__init__(items)
        self.walks = 0

    def __iter__(self):
        self.walks += 1
        return super().__iter__()


def _make_shard(tmp_path, group: int):
    pool_dir = tmp_path / "hft_quote_pool_test"
    pool_dir.mkdir(exist_ok=True)
    shard = pool_dir / f"symbols_group_{group}.yaml"
    shard.write_text(
        "symbols:\n  - code: '2330'\n    exchange: TSE\n    product_type: stock\n",
        encoding="utf-8",
    )
    return shard


def _make_canonical(tmp_path, name: str):
    cfg = tmp_path / name
    cfg.write_text(
        "symbols:\n  - code: '2330'\n    exchange: TSE\n    product_type: stock\n",
        encoding="utf-8",
    )
    return cfg


def _seed_cache(tmp_path, codes: list[str]) -> Path:
    cache = tmp_path / "contracts.json"
    cache.write_text(
        json.dumps(
            {
                "cache_version": 1,
                # Stocks, matching the fake SDK store below: a cache full of
                # derivatives against a stock-only store trips the
                # "broker returned no derivative contracts" integrity guard.
                "contracts": [{"code": c, "exchange": "TSE", "type": "stock"} for c in codes],
            }
        ),
        encoding="utf-8",
    )
    return cache


def _attach_sdk(client, tse: _CountingContracts) -> None:
    """Point the client at a fake SDK contract store and a shared cache file."""
    client.api = MagicMock()
    client.api.Contracts.Stocks.TSE = tse
    client.api.Contracts.Stocks.OTC = []
    client.api.Contracts.Futures.keys.return_value = []
    client.api.Contracts.Options.keys.return_value = []


def _refresh(client):
    build_result = MagicMock(symbols=[{"code": "2330"}], errors=[])
    with (
        patch("hft_platform.config.symbols.build_symbols", return_value=build_result),
        patch("hft_platform.config.symbols.write_symbols_yaml"),
        patch("hft_platform.config.symbols.write_contract_cache") as mock_write_cache,
    ):
        client._contracts_runtime.refresh_contracts_and_symbols()
        return mock_write_cache


def _contract(code: str):
    c = MagicMock()
    c.code = code
    c.symbol = code
    c.name = code
    c.category = "TXF"
    return c


def test_second_pooled_facade_reuses_the_first_facades_rebuild(tmp_path):
    """Two pooled facades, one walk of the SDK contract store and one cache write."""
    cache = _seed_cache(tmp_path, ["TXFH6", "TXFI6"])
    tse_a = _CountingContracts([_contract("TXFH6"), _contract("TXFI6")])
    tse_b = _CountingContracts([_contract("TXFH6"), _contract("TXFI6")])

    with patch("hft_platform.feed_adapter.shioaji.client.sj"):
        a = ShioajiClient(config_path=str(_make_shard(tmp_path, 0)))
        b = ShioajiClient(config_path=str(_make_shard(tmp_path, 1)))
        try:
            for client, tse in ((a, tse_a), (b, tse_b)):
                client._contract_cache_path = str(cache)
                _attach_sdk(client, tse)

            writes_a = _refresh(a)
            writes_b = _refresh(b)

            assert tse_a.walks == 1, "the first facade must do the real rebuild"
            assert tse_b.walks == 0, f"the second facade re-walked the SDK store ({tse_b.walks} walks)"
            assert writes_a.call_count == 1
            assert writes_b.call_count == 0, "the second facade rewrote the shared 13 MB cache"
        finally:
            a.close()
            b.close()


def test_reusing_facade_computes_relevance_against_its_own_subscriptions(tmp_path):
    """The contract delta is shared; the subscription shard it is judged against is not."""
    # Cache holds a code the SDK no longer does -> a one-code removal delta.
    cache = _seed_cache(tmp_path, ["TXFH6", "TXFI6"])
    tse_a = _CountingContracts([_contract("TXFH6")])
    tse_b = _CountingContracts([_contract("TXFH6")])

    with patch("hft_platform.feed_adapter.shioaji.client.sj"):
        a = ShioajiClient(config_path=str(_make_shard(tmp_path, 0)))
        b = ShioajiClient(config_path=str(_make_shard(tmp_path, 1)))
        try:
            for client, tse in ((a, tse_a), (b, tse_b)):
                client._contract_cache_path = str(cache)
                _attach_sdk(client, tse)
            # Only facade B subscribes to the code that disappeared.
            a.subscribed_codes = {"TXFH6"}
            b.subscribed_codes = {"TXFI6"}

            _refresh(a)
            _refresh(b)

            assert a._contract_refresh_last_diff["removed_count"] == 1
            assert b._contract_refresh_last_diff["removed_count"] == 1, "shared delta must carry over"
            assert a._contract_refresh_last_diff["relevant_count"] == 0
            assert b._contract_refresh_last_diff["relevant_count"] == 1, (
                "the reusing facade inherited the owner's relevance instead of computing its own"
            )
        finally:
            a.close()
            b.close()


def test_non_pool_clients_never_share_a_rebuild(tmp_path):
    """Outside pool mode each client owns its config_path and must rebuild for itself."""
    cache = _seed_cache(tmp_path, ["TXFH6"])
    tse_a = _CountingContracts([_contract("TXFH6")])
    tse_b = _CountingContracts([_contract("TXFH6")])

    with patch("hft_platform.feed_adapter.shioaji.client.sj"):
        a = ShioajiClient(config_path=str(_make_canonical(tmp_path, "a.yaml")))
        b = ShioajiClient(config_path=str(_make_canonical(tmp_path, "b.yaml")))
        try:
            for client, tse in ((a, tse_a), (b, tse_b)):
                client._contract_cache_path = str(cache)
                _attach_sdk(client, tse)

            _refresh(a)
            _refresh(b)

            assert tse_a.walks == 1
            assert tse_b.walks == 1, "a non-pool client must not inherit another client's rebuild"
        finally:
            a.close()
            b.close()


def test_rebuild_older_than_the_share_window_is_not_reused(tmp_path):
    """A facade whose refresh thread has drifted does its own work, not a stale delta."""
    cache = _seed_cache(tmp_path, ["TXFH6"])
    tse_a = _CountingContracts([_contract("TXFH6")])
    tse_b = _CountingContracts([_contract("TXFH6")])

    with patch("hft_platform.feed_adapter.shioaji.client.sj"):
        a = ShioajiClient(config_path=str(_make_shard(tmp_path, 0)))
        b = ShioajiClient(config_path=str(_make_shard(tmp_path, 1)))
        try:
            for client, tse in ((a, tse_a), (b, tse_b)):
                client._contract_cache_path = str(cache)
                _attach_sdk(client, tse)
            b._contract_refresh_share_window_s = 0.0

            _refresh(a)
            _refresh(b)

            assert tse_a.walks == 1
            assert tse_b.walks == 1, "a rebuild older than the share window must not be reused"
        finally:
            a.close()
            b.close()
