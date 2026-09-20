"""QuoteConnectionPool must follow ``symbols.list`` when a contract settles.

THESHOW, 2026-09-16 to 09-19: the September contracts settled and
``config/symbols.yaml`` still listed 102 of them. Every hour each delisted code
failed to subscribe and paged ``FeedSubscriptionPermanentlyFailed`` -- 2,988
Telegram notifications in six days, 96% of everything the bot sent.

The rebuild was never the missing piece: the hourly contract refresh already
calls ``build_symbols(symbols.list, fresh contracts)``, which resolves
``TMF@front`` to the new month. In pool mode it then threw the result away
(``skip_symbol_yaml_write_in_pool_mode``), because the shards were fixed at
boot and re-sharding had no owner. These tests pin the owner.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from hft_platform.config import _symbols_types as symbols_types
from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

# 2026-09-17 20:00 Taipei — the night after TMFI6 settled (13:30 on 09-16).
AFTER_SEPTEMBER_SETTLEMENT_NS = int(dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.UTC).timestamp() * 1e9)
# 2026-09-16 20:00 Taipei — settlement day, after the 13:30 final session.
SETTLEMENT_EVENING_NS = int(dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.UTC).timestamp() * 1e9)

SEPTEMBER = "2026/09/16"
OCTOBER = "2026/10/21"
NOVEMBER = "2026/11/18"


def _future(code: str, root: str, delivery: str) -> dict[str, Any]:
    return {
        "code": code,
        "symbol": code,
        "root": root,
        "type": "future",
        "delivery_date": delivery,
        "name": f"{root} future",
        "unit": 1,
    }


def _contract_cache(tmp_path: Path, contracts: list[dict[str, Any]]) -> str:
    path = tmp_path / "contracts.json"
    path.write_text(
        json.dumps({"cache_version": 1, "updated_at": "2026-09-19T12:00:00Z", "contracts": contracts}),
        encoding="utf-8",
    )
    return str(path)


class FakeSubscriptions:
    """Records what the roll asks the broker to do."""

    def __init__(self, client: "FakeClient") -> None:
        self._client = client

    def _unsubscribe_symbol(self, sym: dict[str, Any]) -> None:
        self._client.unsubscribed.append(str(sym.get("code")))

    def _subscribe_symbol(self, sym: dict[str, Any], cb: Any) -> bool:
        code = str(sym.get("code"))
        self._client.subscribed.append(code)
        return code not in self._client.refuse_codes


class FakeClient:
    def __init__(self, shard_path: str, symbols: list[dict[str, Any]]) -> None:
        self.config_path = shard_path
        self.symbols = symbols
        self.subscribed_codes: set[str] = {str(s["code"]) for s in symbols}
        self.subscribed_count = len(self.subscribed_codes)
        self._failed_sub_symbols: list[dict[str, Any]] = []
        self.unsubscribed: list[str] = []
        self.subscribed: list[str] = []
        self.refuse_codes: set[str] = set()
        self.routes_refreshed = 0

    def _subscriptions(self) -> FakeSubscriptions:
        return FakeSubscriptions(self)

    def _load_config(self) -> None:
        with open(self.config_path) as fh:
            self.symbols = (yaml.safe_load(fh) or {}).get("symbols", [])

    def _refresh_quote_routes(self) -> None:
        self.routes_refreshed += 1


def _make_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    universe: list[str],
    contracts: list[dict[str, Any]],
    list_body: str,
    today: dt.date,
    num_conns: int = 2,
) -> QuoteConnectionPool:
    canonical = tmp_path / "symbols.yaml"
    canonical.write_text(
        yaml.safe_dump({"symbols": [{"code": code, "exchange": "FUT", "product_type": "future"} for code in universe]}),
        encoding="utf-8",
    )
    (tmp_path / "symbols.list").write_text(list_body, encoding="utf-8")
    monkeypatch.setenv("HFT_CONTRACT_CACHE_PATH", _contract_cache(tmp_path, contracts))
    monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", str(tmp_path / "runtime.yaml"))
    monkeypatch.setattr(symbols_types, "exchange_today", lambda: today)
    return QuoteConnectionPool(str(canonical), {}, num_conns=num_conns)


def _universe_codes(pool: QuoteConnectionPool) -> set[str]:
    """The universe the pool holds, which before login no facade mirrors yet."""
    return {str(sym["code"]) for sym in pool._all_symbols}


def _attach_facades(pool: QuoteConnectionPool) -> list[FakeClient]:
    clients: list[FakeClient] = []
    for group_id, shard_path in enumerate(pool._shard_paths):
        with open(shard_path) as fh:
            symbols = (yaml.safe_load(fh) or {}).get("symbols", [])
        client = FakeClient(shard_path, symbols)
        clients.append(client)
        pool._clients.append(SimpleNamespace(logged_in=True, _client=client))
    pool._user_callback = lambda *args, **kwargs: None
    return clients


TMF_LIST = "TMF@front exchange=FUT tags=futures|front_month|tmf\nTMF@next exchange=FUT tags=futures|next_month|tmf\n"
SEPTEMBER_CONTRACTS = [
    _future("TMFI6", "TMF", SEPTEMBER),
    _future("TMFJ6", "TMF", OCTOBER),
    _future("TMFK6", "TMF", NOVEMBER),
]


class TestBootRoll:
    def test_settled_code_never_reaches_a_subscribe_call(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The roll runs before the facades exist, so the shards they read are clean."""
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )

        assert pool.roll_universe(live=False, now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is True

        assert _universe_codes(pool) == {"TMFJ6", "TMFK6"}
        for shard_path in pool._shard_paths:
            shard_codes = {s["code"] for s in (yaml.safe_load(Path(shard_path).read_text()) or {})["symbols"]}
            assert "TMFI6" not in shard_codes
        assert pool.universe_rolled is True

    def test_canonical_symbols_file_is_never_written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )
        canonical = Path(pool._symbols_input_path)
        before = canonical.read_bytes()

        assert pool.roll_universe(live=False, now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is True

        assert canonical.read_bytes() == before

    def test_snapshot_records_the_adopted_universe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )

        pool.roll_universe(live=False, now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS)

        snapshot = tmp_path / "runtime.yaml"
        assert snapshot.is_file()
        text = snapshot.read_text()
        assert "Auto-rolled by QuoteConnectionPool.roll_universe" in text
        assert {s["code"] for s in (yaml.safe_load(text) or {})["symbols"]} == {"TMFJ6", "TMFK6"}

    def test_universe_that_did_not_change_is_left_alone(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFJ6", "TMFK6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )

        assert pool.roll_universe(live=False, now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is False
        assert pool.universe_rolled is False

    def test_second_roll_is_a_no_op(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )

        assert pool.roll_universe(live=False, now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is True
        assert pool.roll_universe(live=False, now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is False


class TestRefusals:
    def test_universe_still_holding_a_settled_contract_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Settlement day after 13:30: the builder's date filter still keeps I6,
        the delivery cutoff does not. The stricter one wins and nothing moves."""
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFH6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 16),
        )

        assert pool.roll_universe(live=False, now_ns=SETTLEMENT_EVENING_NS) is False
        assert _universe_codes(pool) == {"TMFH6"}

    def test_build_failure_keeps_the_running_universe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body="ZZZ@front exchange=FUT tags=futures\n",
            today=dt.date(2026, 9, 17),
        )

        assert pool.roll_universe(live=False, now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is False
        assert _universe_codes(pool) == {"TMFJ6"}

    def test_unreadable_contract_cache_keeps_the_running_universe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )
        monkeypatch.setenv("HFT_CONTRACT_CACHE_PATH", str(tmp_path / "gone.json"))

        assert pool.roll_universe(live=False, now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is False
        assert _universe_codes(pool) == {"TMFJ6"}

    def test_universe_too_large_for_the_pool_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )
        monkeypatch.setattr(
            "hft_platform.feed_adapter.shioaji.quote_connection_pool._MAX_SUBSCRIPTIONS_PER_CONN",
            0,
        )

        assert pool.roll_universe(live=False, now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is False
        assert _universe_codes(pool) == {"TMFI6"}


class TestLiveRoll:
    def test_only_the_settled_code_is_unsubscribed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """R47's contract stays subscribed across the roll: no gap, no re-subscribe."""
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )
        clients = _attach_facades(pool)
        monkeypatch.setattr(QuoteConnectionPool, "reconnect_allowed", lambda self: False)

        assert pool.roll_universe(now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is True

        unsubscribed = [code for client in clients for code in client.unsubscribed]
        subscribed = [code for client in clients for code in client.subscribed]
        assert unsubscribed == ["TMFI6"]
        assert subscribed == ["TMFK6"]
        assert "TMFJ6" not in subscribed

    def test_subscription_counts_follow_the_roll(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )
        clients = _attach_facades(pool)
        monkeypatch.setattr(QuoteConnectionPool, "reconnect_allowed", lambda self: False)

        pool.roll_universe(now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS)

        live_codes = {code for client in clients for code in client.subscribed_codes}
        assert live_codes == {"TMFJ6", "TMFK6"}
        for client in clients:
            assert client.subscribed_count == len(client.subscribed_codes)

    def test_a_code_the_broker_refuses_is_queued_for_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )
        clients = _attach_facades(pool)
        for client in clients:
            client.refuse_codes = {"TMFK6"}
        monkeypatch.setattr(QuoteConnectionPool, "reconnect_allowed", lambda self: False)

        assert pool.roll_universe(now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is True

        queued = [str(sym.get("code")) for client in clients for sym in client._failed_sub_symbols]
        assert queued == ["TMFK6"]
        live_codes = {code for client in clients for code in client.subscribed_codes}
        assert "TMFK6" not in live_codes

    def test_roll_is_deferred_while_a_session_is_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Four facades churning subscriptions mid-session is not worth the roll."""
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )
        clients = _attach_facades(pool)
        monkeypatch.setattr(QuoteConnectionPool, "reconnect_allowed", lambda self: True)

        assert pool.roll_universe(now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is False
        assert _universe_codes(pool) == {"TMFI6", "TMFJ6"}
        assert all(client.unsubscribed == [] and client.subscribed == [] for client in clients)

    def test_a_logged_out_facade_gets_its_shard_but_no_broker_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )
        clients = _attach_facades(pool)
        pool._clients[0].logged_in = False
        monkeypatch.setattr(QuoteConnectionPool, "reconnect_allowed", lambda self: False)

        assert pool.roll_universe(now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is True

        assert clients[0].unsubscribed == [] and clients[0].subscribed == []
        shard_codes = {s["code"] for s in (yaml.safe_load(Path(pool._shard_paths[0]).read_text()) or {})["symbols"]}
        assert "TMFI6" not in shard_codes

    def test_one_failing_facade_does_not_abort_the_roll(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )
        clients = _attach_facades(pool)

        def _boom() -> None:
            raise RuntimeError("facade wedged")

        clients[0]._load_config = _boom  # type: ignore[method-assign]
        monkeypatch.setattr(QuoteConnectionPool, "reconnect_allowed", lambda self: False)

        assert pool.roll_universe(now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is True
        assert _universe_codes(pool) == {"TMFJ6", "TMFK6"}


class TestListener:
    def test_listener_gets_the_universe_that_was_adopted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )
        seen: list[list[str]] = []
        pool.set_universe_listener(lambda symbols: seen.append([str(s["code"]) for s in symbols]))

        pool.roll_universe(live=False, now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS)

        assert seen == [["TMFJ6", "TMFK6"]] or seen == [["TMFK6", "TMFJ6"]]

    def test_listener_runs_before_the_broker_is_touched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Metadata must exist before the first tick of a rolled-in contract."""
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )
        clients = _attach_facades(pool)
        order: list[str] = []
        pool.set_universe_listener(lambda symbols: order.append("metadata"))
        for client in clients:
            original = client._subscriptions

            def tracked(_original: Any = original) -> Any:
                order.append("broker")
                return _original()

            client._subscriptions = tracked  # type: ignore[method-assign]
        monkeypatch.setattr(QuoteConnectionPool, "reconnect_allowed", lambda self: False)

        pool.roll_universe(now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS)

        assert order[0] == "metadata"
        assert "broker" in order

    def test_a_throwing_listener_does_not_fail_the_roll(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFI6", "TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )

        def _boom(_symbols: Any) -> None:
            raise RuntimeError("metadata exploded")

        pool.set_universe_listener(_boom)

        assert pool.roll_universe(live=False, now_ns=AFTER_SEPTEMBER_SETTLEMENT_NS) is True


class TestRollClock:
    @pytest.mark.asyncio
    async def test_clock_survives_a_failing_check(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A raising roll must not end the clock — that is how the October roll
        would silently stop happening."""
        import asyncio

        pool = _make_pool(
            tmp_path,
            monkeypatch,
            universe=["TMFJ6"],
            contracts=SEPTEMBER_CONTRACTS,
            list_body=TMF_LIST,
            today=dt.date(2026, 9, 17),
        )
        calls: list[int] = []

        def _boom(_self: Any, **_kwargs: Any) -> bool:
            calls.append(1)
            raise RuntimeError("roll blew up")

        monkeypatch.setattr(QuoteConnectionPool, "roll_universe", _boom)

        task = asyncio.create_task(pool.run_universe_roll_clock(interval_s=0.01))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if len(calls) >= 2:
                break
        task.cancel()

        assert len(calls) >= 2, "clock stopped after the first failure"


def test_bootstrap_wires_the_roll_clock_and_metadata_listener() -> None:
    """The roll only ever runs if bootstrap starts it and feeds metadata."""
    source = Path("src/hft_platform/services/bootstrap.py").read_text(encoding="utf-8")
    assert "pool.roll_universe(live=False)" in source
    assert "set_universe_listener(symbol_metadata.apply_runtime_overlay)" in source
    assert "deferred_tasks.append(md_client.run_universe_roll_clock())" in source
