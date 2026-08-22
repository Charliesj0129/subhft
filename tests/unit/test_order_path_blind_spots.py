"""Regressions for two order-path blind spots found on THESHOW 2026-08-20.

Both defects are silent by construction: they make the platform *look*
healthy while it has stopped tracking something real.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from hft_platform.feed_adapter.shioaji.account_gateway import AccountGateway


class _FakeClient:
    """Minimal stand-in for ShioajiClient's account-facing surface."""

    def __init__(self, api: Any) -> None:
        self.api = api
        self.mode = "real"
        self.logged_in = True
        self.recorded: list[tuple[str, bool]] = []
        self._positions_cache_ttl_s = 1.5

    def _cache_get(self, key: str) -> Any | None:
        return None

    def _cache_set(self, key: str, ttl_s: float, value: Any) -> None:
        return None

    def _rate_limit_api(self, op: str) -> bool:
        return True

    def _record_api_latency(self, op: str, start_ns: int, ok: bool = True) -> None:
        self.recorded.append((op, ok))


class _ApiWithNoAccounts:
    """A logged-in SDK handle that exposes neither trading account.

    This is what a logged-out session, an unactivated account, or an SDK
    surface change all look like from the gateway's side.
    """

    stock_account = None
    futopt_account = None

    def list_positions(self, account: Any) -> list[Any]:  # pragma: no cover
        raise AssertionError("list_positions must not be called without an account")


def test_get_positions_returns_none_when_no_trading_account_resolves() -> None:
    client = _FakeClient(_ApiWithNoAccounts())
    gateway = AccountGateway(client)

    result = gateway.get_positions()

    # None means "unknown"; an empty list would claim the account is flat and
    # the reconciler would build a broker map from it.
    assert result is None
    assert client.recorded == [("positions", False)]
    assert "no trading account resolved" in (gateway._last_positions_error or "")


class _ApiWithFutoptOnly:
    stock_account = None
    futopt_account = object()

    def __init__(self, positions: list[Any]) -> None:
        self._positions = positions

    def list_positions(self, account: Any) -> list[Any]:
        return self._positions


def test_get_positions_still_returns_empty_when_the_broker_answers_flat() -> None:
    """A genuine "you hold nothing" must stay distinguishable from "unknown"."""
    client = _FakeClient(_ApiWithFutoptOnly([]))
    gateway = AccountGateway(client)

    result = gateway.get_positions()

    assert result == []
    assert result is not None
    assert client.recorded == [("positions", True)]


# ---------------------------------------------------------------------------
# order_id_map trailing-edge persistence
# ---------------------------------------------------------------------------


async def _await_tokens(path: Path, expected: set[str], timeout_s: float = 2.0) -> set[str]:
    """Poll until the persisted set matches, or the deadline passes.

    The leading-edge write is offloaded to an executor and the trailing edge is
    a ``call_later``, so neither is observable synchronously. Polling keeps the
    test honest without pinning it to a fixed sleep.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    tokens: set[str] = set()
    while loop.time() < deadline:
        tokens = _read_persisted_tokens(path)
        if tokens == expected:
            return tokens
        await asyncio.sleep(0.01)
    return tokens


def _read_persisted_tokens(path: Path) -> set[str]:
    tokens: set[str] = set()
    if not path.exists():
        return tokens
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        for key in ("broker_id", "token", "key"):
            if key in record:
                tokens.add(str(record[key]))
    return tokens


@pytest.mark.asyncio
async def test_second_mapping_inside_the_throttle_window_still_reaches_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A two-sided quote registers both legs inside one throttle interval.

    Before the trailing-edge flush the second leg was dropped and, because no
    caller ever passes ``force=True``, it was never written again — the exact
    shape seen in production, where ``.state/order_id_map.jsonl`` held one of
    two orders eight hours later.
    """
    from hft_platform.order.adapter import OrderAdapter

    persist_path = tmp_path / "order_id_map.jsonl"
    monkeypatch.setenv("HFT_ORDER_ID_MAP_PERSIST_PATH", str(persist_path))
    monkeypatch.setenv("HFT_ORDER_ID_MAP_PERSIST_INTERVAL_S", "0.2")

    adapter = OrderAdapter.__new__(OrderAdapter)
    adapter._order_id_map_persist_path = str(persist_path)
    adapter._order_id_map_persist_interval_s = 0.2
    adapter._order_id_map_last_persist_s = 0.0
    adapter._order_id_map_trailing_handle = None
    # __new__ skips __init__, so the checkpoint's own state must be seeded.
    adapter._order_id_map_persist_futures = set()
    adapter.order_id_map = {}

    persisted: list[dict[str, str]] = []

    def _persist() -> None:
        persisted.append(dict(adapter.order_id_map))
        persist_path.write_text(
            "\n".join(json.dumps({"broker_id": k, "order_key": v}) for k, v in adapter.order_id_map.items())
        )

    adapter.persist_order_id_map = _persist  # type: ignore[method-assign]

    # Leg 1: first write of the window persists immediately (leading edge).
    adapter.order_id_map["121"] = "R47_MAKER_TMF:121"
    adapter._maybe_persist_order_id_map()
    assert await _await_tokens(persist_path, {"121"}) == {"121"}

    # Leg 2, ~0.3 s later in production terms: inside the throttle window.
    adapter.order_id_map["122"] = "R47_MAKER_TMF:122"
    adapter._maybe_persist_order_id_map()

    # Nothing else ever calls persist again — the trailing edge must.
    assert await _await_tokens(persist_path, {"121", "122"}) == {"121", "122"}
    adapter._cancel_trailing_persist()
