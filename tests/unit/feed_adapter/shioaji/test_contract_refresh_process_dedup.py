"""The hourly contract refresh must be done once per process, not once per facade.

2026-08-03 measurement on THESHOW: the four pooled facades each ran a full
read → walk → write of the *same* ``config/contracts.json`` (13 MB, 54 727
contracts) on every hourly cycle, producing identical diffs and spanning ~11 s
per hour — ~1.2 s of GIL-held walk each, plus the 2 s login-slot gaps they
queue behind. That accounted for 17 of the 21 event-loop stalls above 50 ms
measured over a clean two-hour night session.

What must stay per-facade is the relevance half: each facade subscribes to its
own ~74-symbol shard, so ``relevant_count`` — and therefore the resubscribe
decision — differs even though the contract delta does not.

The concurrent tests at the bottom exist because the first attempt at this
(PR #392) passed every sequential test here and was still a complete no-op in
production: the facades arrive 1.5 s apart, closer together than one rebuild
takes to finish, so a handoff that publishes on completion is never there to be
found. Running the facades one after another cannot see that.
"""

from __future__ import annotations

import contextlib
import json
import threading
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
        #: Set the instant a walk begins, so a concurrent test can wait for
        #: "this facade started rebuilding" instead of sleeping a guessed
        #: interval and hoping.
        self.walk_started = threading.Event()

    def __iter__(self):
        self.walks += 1
        self.walk_started.set()
        return super().__iter__()


class _BlockingContracts(_CountingContracts):
    """A store whose walk parks until the test lets it go.

    Lets a test pin one facade in the middle of a rebuild — holding the rebuild
    slot but not yet having published — which is the exact window the pooled
    facades actually arrive in.
    """

    def __init__(self, items, *, fail: bool = False):
        super().__init__(items)
        self.entered = threading.Event()
        self.release = threading.Event()
        self._fail = fail

    def __iter__(self):
        self.entered.set()
        if not self.release.wait(timeout=10.0):  # pragma: no cover - deadlock guard
            raise AssertionError("the blocked walk was never released")
        if self._fail:
            raise RuntimeError("SDK contract walk failed")
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


def _seed_cache(tmp_path, codes: list[str], *, kind: str = "stock") -> Path:
    cache = tmp_path / "contracts.json"
    exchange = "TSE" if kind == "stock" else "TAIFEX"
    cache.write_text(
        json.dumps(
            {
                "cache_version": 1,
                # Stocks by default, matching the fake SDK store below: a cache
                # full of derivatives against a stock-only store trips the
                # "broker returned no derivative contracts" integrity guard.
                # ``kind="future"`` deliberately trips it.
                "contracts": [{"code": c, "exchange": exchange, "type": kind} for c in codes],
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


@contextlib.contextmanager
def _patched_symbol_io():
    """Patch the symbol/cache writers once, around the whole exercise.

    ``unittest.mock.patch`` rebinds module attributes process-wide, so the
    concurrent tests below must enter it once on the main thread rather than
    once per worker.
    """
    build_result = MagicMock(symbols=[{"code": "2330"}], errors=[])
    with (
        patch("hft_platform.config.symbols.build_symbols", return_value=build_result),
        patch("hft_platform.config.symbols.write_symbols_yaml"),
        patch("hft_platform.config.symbols.write_contract_cache") as mock_write_cache,
    ):
        yield mock_write_cache


def _refresh(client):
    with _patched_symbol_io() as mock_write_cache:
        client._contracts_runtime.refresh_contracts_and_symbols()
        return mock_write_cache


def _refresh_in_thread(client, errors: list) -> threading.Thread:
    """Run one facade's refresh on its own thread, capturing any escape."""

    def _run() -> None:
        try:
            client._contracts_runtime.refresh_contracts_and_symbols()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the caller's assert
            errors.append(exc)

    thread = threading.Thread(target=_run, name="test-refresh", daemon=True)
    thread.start()
    return thread


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


def test_sibling_arriving_before_the_owner_publishes_waits_and_reuses(tmp_path):
    """The handoff has to hold for a sibling that arrives *during* the rebuild.

    This is the case the field disproved on 2026-08-05: the four facades arrived
    1.494 s apart while one rebuild took 2.028 s to publish, so every sibling
    reached the check before there was anything to find and all four did the full
    read/walk/write. The tests above never caught it because they run the facades
    strictly one after another — the one ordering the pool never produces.
    """
    cache = _seed_cache(tmp_path, ["TXFH6", "TXFI6"])
    tse_a = _BlockingContracts([_contract("TXFH6"), _contract("TXFI6")])
    tse_b = _CountingContracts([_contract("TXFH6"), _contract("TXFI6")])
    errors: list = []

    with patch("hft_platform.feed_adapter.shioaji.client.sj"):
        a = ShioajiClient(config_path=str(_make_shard(tmp_path, 0)))
        b = ShioajiClient(config_path=str(_make_shard(tmp_path, 1)))
        try:
            for client, tse in ((a, tse_a), (b, tse_b)):
                client._contract_cache_path = str(cache)
                _attach_sdk(client, tse)

            with _patched_symbol_io():
                thread_a = _refresh_in_thread(a, errors)
                assert tse_a.entered.wait(timeout=5.0), "the owner never reached the SDK walk"

                # A is now mid-rebuild and has published nothing. B must park.
                thread_b = _refresh_in_thread(b, errors)
                assert not tse_b.walk_started.wait(timeout=1.0), (
                    "the sibling started its own walk while the owner was still rebuilding"
                )

                tse_a.release.set()
                thread_a.join(timeout=10.0)
                thread_b.join(timeout=10.0)
                assert not thread_a.is_alive() and not thread_b.is_alive()

            assert errors == []
            assert tse_a.walks == 1, "the owner must do the real rebuild"
            assert tse_b.walks == 0, f"the sibling re-walked the SDK store ({tse_b.walks} walks)"
            assert b._contract_refresh_last_diff["removed_count"] == 0
            assert b._contract_refresh_last_diff["contracts_after"] == 2, (
                "the sibling did not inherit the owner's delta"
            )
        finally:
            tse_a.release.set()
            a.close()
            b.close()


def test_sibling_rebuilds_itself_when_the_owners_cycle_aborts(tmp_path):
    """An owner that publishes nothing must hand back an empty slot, not a hung pool.

    Driven through the integrity guard — a cache holding derivatives against an
    SDK store that reports none — because that is the one abort path that
    returns early, before the publish, while still holding the slot.
    """
    cache = _seed_cache(tmp_path, ["TXFH6"], kind="future")
    tse_a = _BlockingContracts([_contract("2330")])
    tse_b = _CountingContracts([_contract("2330")])
    errors: list = []

    with patch("hft_platform.feed_adapter.shioaji.client.sj"):
        a = ShioajiClient(config_path=str(_make_shard(tmp_path, 0)))
        b = ShioajiClient(config_path=str(_make_shard(tmp_path, 1)))
        try:
            for client, tse in ((a, tse_a), (b, tse_b)):
                client._contract_cache_path = str(cache)
                client._contract_fetch_stagger_gap_s = 0.0  # the login slot is not under test here
                _attach_sdk(client, tse)

            with _patched_symbol_io():
                thread_a = _refresh_in_thread(a, errors)
                assert tse_a.entered.wait(timeout=5.0)
                thread_b = _refresh_in_thread(b, errors)

                tse_a.release.set()  # the owner now walks, then trips the guard
                thread_a.join(timeout=10.0)
                thread_b.join(timeout=10.0)
                assert not thread_a.is_alive(), "the aborted owner never released the slot"
                assert not thread_b.is_alive(), "the sibling is still waiting on a dead owner"

            assert errors == []
            assert tse_a.walks == 1
            assert tse_b.walks == 1, "the sibling must rebuild for itself when nothing was published"
            assert contracts_runtime._take_shared_rebuild(300.0) is None, (
                "an aborted cycle must not leave a rebuild behind"
            )
        finally:
            tse_a.release.set()
            a.close()
            b.close()


def test_sibling_gives_up_and_rebuilds_after_the_slot_wait_timeout(tmp_path):
    """A wedged facade must not stall the other refresh threads indefinitely."""
    cache = _seed_cache(tmp_path, ["TXFH6"])
    tse_a = _BlockingContracts([_contract("TXFH6")])
    tse_b = _CountingContracts([_contract("TXFH6")])
    errors: list = []

    with patch("hft_platform.feed_adapter.shioaji.client.sj"):
        a = ShioajiClient(config_path=str(_make_shard(tmp_path, 0)))
        b = ShioajiClient(config_path=str(_make_shard(tmp_path, 1)))
        try:
            for client, tse in ((a, tse_a), (b, tse_b)):
                client._contract_cache_path = str(cache)
                _attach_sdk(client, tse)
            b._contract_refresh_share_wait_s = 0.05
            # The walk has a second, pre-existing gate: ``acquire_login_slot``,
            # which the parked owner is also holding. Time B out of that one too,
            # or this test measures the login stagger instead of the rebuild slot.
            b._contract_fetch_stagger_timeout_s = 0.2
            b._contract_fetch_stagger_gap_s = 0.0

            with _patched_symbol_io():
                thread_a = _refresh_in_thread(a, errors)
                assert tse_a.entered.wait(timeout=5.0)

                thread_b = _refresh_in_thread(b, errors)
                # B waits 50 ms for the slot, gives up, and rebuilds alone —
                # while A is still parked, so nothing was ever published.
                assert tse_b.walk_started.wait(timeout=5.0), (
                    "the sibling waited on the wedged owner instead of timing out"
                )
                thread_b.join(timeout=10.0)
                assert not thread_b.is_alive()

                tse_a.release.set()
                thread_a.join(timeout=10.0)

            assert errors == []
            assert tse_b.walks == 1
        finally:
            tse_a.release.set()
            a.close()
            b.close()
