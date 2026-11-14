"""A seqno the broker reissues must not drop a real fill (fill identity, 2026-11-13).

``FillEvent.fill_id`` is the deal's ``seqno``, and ``seqno`` is ORDER-scoped. The
paper server reissues ids at the day-session open that the previous night session
already used: night deal ``06FE0D`` for order R47_MAKER_TMF:33978 (11-13 04:02
Taipei) and, at 08:50, a day deal ``06FE0D`` for a different order. The router
and R47 both keyed their dedup on the bare ``fill_id``, so five real fills were
skipped as ``duplicate_fill_skipped``.

The key is now the deal's identity: ``fill_dedup_key`` (symbol, order id, side,
price, qty, match ts, fill id). A byte-identical redelivery still collides.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import MagicMock, patch

import orjson
import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.core import timebase
from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq
from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.router import ExecutionRouter

REISSUED_SEQNO = "06FE0D"
SYMBOL = "TXFD6"
ACCOUNT = "FUTACC1"
NIGHT_TS_NS = 1_000_000_000_000
DAY_TS_NS = 2_000_000_000_000


@pytest.fixture(autouse=True)
def _stub_metrics(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    stub = MagicMock()
    monkeypatch.setattr(
        "hft_platform.observability.metrics.MetricsRegistry.get",
        staticmethod(lambda: stub),
    )
    return stub


@pytest.fixture(autouse=True)
def _fresh_dlq(tmp_path, monkeypatch: pytest.MonkeyPatch):
    import hft_platform.execution.fill_dlq as dlq_mod

    old = dlq_mod._dlq
    dlq_mod._dlq = None
    monkeypatch.setenv("HFT_FILL_DLQ_PERSIST_PATH", str(tmp_path / "dlq.jsonl"))
    yield
    dlq_mod._dlq = old


@pytest.fixture()
def persist_path(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = str(tmp_path / "fill_dedup_window.jsonl")
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", path)
    # 0 = checkpoint on every registered fill, so the file is observable.
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_INTERVAL_S", "0")
    return path


def _deal(
    *,
    seqno: str = REISSUED_SEQNO,
    ordno: str = "ORD_A",
    ts_ns: int = NIGHT_TS_NS,
    qty: int = 1,
    price: float = 123.0,
    action: str = "Buy",
) -> RawExecEvent:
    return RawExecEvent(
        topic="deal",
        data={
            "ordno": ordno,
            "code": SYMBOL,
            "action": action,
            "price": price,
            "quantity": qty,
            "seqno": seqno,
            "account_id": ACCOUNT,
            "custom_field": "strat1",
            "ts": ts_ns,
        },
        ingest_ts_ns=ts_ns,
    )


def _make_router(store: PositionStore | None = None) -> ExecutionRouter:
    return ExecutionRouter(
        bus=MagicMock(),
        raw_queue=asyncio.Queue(),
        order_id_map={},
        position_store=store if store is not None else PositionStore(),
        terminal_handler=MagicMock(),
    )


async def _run_router(router: ExecutionRouter, *raws: RawExecEvent) -> None:
    """Feed ``raws`` through the real ``run()`` loop and wait until all are handled."""
    for raw in raws:
        router.raw_queue.put_nowait(raw)
    task = asyncio.create_task(router.run())
    try:
        await asyncio.wait_for(router.raw_queue.join(), timeout=5.0)
    finally:
        router.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _net_qty(store: PositionStore) -> int:
    return sum(p.net_qty for p in store.positions.values() if p.symbol == SYMBOL)


def _orphan(*, fill_id: str, order_id: str, match_ts_ns: int) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        account_id=ACCOUNT,
        order_id=order_id,
        strategy_id="UNKNOWN",
        symbol=SYMBOL,
        side=Side.BUY,
        qty=1,
        price=1_230_000,
        fee=0,
        tax=0,
        ingest_ts_ns=match_ts_ns,
        match_ts_ns=match_ts_ns,
    )


# ---------------------------------------------------------------------------
# (a) the 06FE0D replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reissued_seqno_on_a_different_order_is_applied(persist_path, _stub_metrics) -> None:
    store = PositionStore()
    router = _make_router(store)

    night = _deal(seqno=REISSUED_SEQNO, ordno="ORD_A", ts_ns=NIGHT_TS_NS)
    day = _deal(seqno=REISSUED_SEQNO, ordno="ORD_B", ts_ns=DAY_TS_NS)
    await _run_router(router, night, day)

    assert _net_qty(store) == 2, "the day deal that reused seqno 06FE0D was dropped"
    _stub_metrics.duplicate_fill_total.inc.assert_not_called()


@pytest.mark.asyncio
async def test_reissued_seqno_on_the_same_order_at_a_later_ts_is_applied(persist_path, _stub_metrics) -> None:
    store = PositionStore()
    router = _make_router(store)

    await _run_router(
        router,
        _deal(ordno="ORD_A", ts_ns=NIGHT_TS_NS),
        _deal(ordno="ORD_A", ts_ns=DAY_TS_NS),
    )

    assert _net_qty(store) == 2
    _stub_metrics.duplicate_fill_total.inc.assert_not_called()


# ---------------------------------------------------------------------------
# (b) a genuine redelivery is still skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_redelivery_is_skipped_once(persist_path, _stub_metrics) -> None:
    store = PositionStore()
    router = _make_router(store)

    await _run_router(router, _deal(), _deal())

    assert _net_qty(store) == 1
    assert _stub_metrics.duplicate_fill_total.inc.call_count == 1


def test_fill_dedup_key_is_stable_for_a_redelivery_and_differs_on_a_reissue() -> None:
    from hft_platform.execution.router import fill_dedup_key

    base = _orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=NIGHT_TS_NS)
    redelivery = _orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=NIGHT_TS_NS)
    other_order = _orphan(fill_id=REISSUED_SEQNO, order_id="ORD_B", match_ts_ns=NIGHT_TS_NS)
    other_ts = _orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=DAY_TS_NS)

    assert fill_dedup_key(base) == fill_dedup_key(redelivery)
    assert fill_dedup_key(base) != fill_dedup_key(other_order)
    assert fill_dedup_key(base) != fill_dedup_key(other_ts)
    assert fill_dedup_key(base).startswith("v2|")
    assert fill_dedup_key(base).endswith(f"|{REISSUED_SEQNO}")


# ---------------------------------------------------------------------------
# (c) a restart over an old-format window file
# ---------------------------------------------------------------------------


def _write_legacy_window(path: str, *keys: str) -> None:
    with open(path, "wb") as f:
        for key in keys:
            f.write(orjson.dumps(key) + b"\n")


@pytest.mark.asyncio
async def test_legacy_window_entry_skips_the_same_seqno_within_the_ttl(persist_path, _stub_metrics) -> None:
    _write_legacy_window(persist_path, REISSUED_SEQNO)
    store = PositionStore()
    router = _make_router(store)

    assert REISSUED_SEQNO in router._legacy_fill_ids
    assert REISSUED_SEQNO not in router._seen_fill_ids

    with patch("hft_platform.execution.router.logger") as log:
        await _run_router(router, _deal(seqno=REISSUED_SEQNO, ordno="ORD_A"))

    assert _net_qty(store) == 0, "a broker replay of a pre-deploy fill was applied twice"
    assert _stub_metrics.duplicate_fill_total.inc.call_count == 1
    events = [c.args[0] for c in log.warning.call_args_list if c.args]
    assert "duplicate_fill_skipped_legacy" in events


# The legacy window is also bounded by a wall-clock boundary taken at load: a
# deal stamped after the restart cannot be a broker replay of a fill the OLD
# build applied, so it is never legacy-matched. Without it, a deploy restart
# shortly before a session open shadows the day's reissued seqnos (the bug).

_ONE_MINUTE_NS = 60 * 1_000_000_000
_ONE_HOUR_NS = 3600 * 1_000_000_000


@pytest.mark.asyncio
async def test_legacy_seqno_on_a_deal_stamped_after_the_restart_is_applied(persist_path, _stub_metrics) -> None:
    _write_legacy_window(persist_path, REISSUED_SEQNO)
    store = PositionStore()
    router = _make_router(store)
    assert REISSUED_SEQNO in router._legacy_fill_ids, "inside the TTL the legacy set is live"

    day_open_deal = _deal(seqno=REISSUED_SEQNO, ordno="ORD_B", ts_ns=timebase.now_ns() + _ONE_MINUTE_NS)
    with patch("hft_platform.execution.router.logger") as log:
        await _run_router(router, day_open_deal)

    assert _net_qty(store) == 1, "a day-open deal reusing a legacy seqno was dropped as a legacy duplicate"
    _stub_metrics.duplicate_fill_total.inc.assert_not_called()
    events = [c.args[0] for c in log.warning.call_args_list if c.args]
    assert "duplicate_fill_skipped_legacy" not in events


@pytest.mark.asyncio
async def test_legacy_seqno_on_a_deal_stamped_before_the_restart_is_skipped_within_the_ttl(
    persist_path, _stub_metrics
) -> None:
    _write_legacy_window(persist_path, REISSUED_SEQNO)
    store = PositionStore()
    router = _make_router(store)

    replayed = _deal(seqno=REISSUED_SEQNO, ordno="ORD_A", ts_ns=timebase.now_ns() - _ONE_HOUR_NS)
    with patch("hft_platform.execution.router.logger") as log:
        await _run_router(router, replayed)

    assert _net_qty(store) == 0, "a broker replay of a pre-deploy fill was applied twice"
    assert _stub_metrics.duplicate_fill_total.inc.call_count == 1
    events = [c.args[0] for c in log.warning.call_args_list if c.args]
    assert "duplicate_fill_skipped_legacy" in events


@pytest.mark.parametrize("match_ts_ns", [0, -1])
def test_legacy_match_needs_a_positive_deal_timestamp(persist_path, _stub_metrics, match_ts_ns) -> None:
    _write_legacy_window(persist_path, REISSUED_SEQNO)
    router = _make_router()
    before_boundary = _orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=timebase.now_ns() - _ONE_HOUR_NS)
    assert router._is_legacy_fill_duplicate(before_boundary) is True, "control: a live legacy entry must match"
    _stub_metrics.duplicate_fill_total.inc.reset_mock()

    unstamped = _orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=match_ts_ns)

    assert router._is_legacy_fill_duplicate(unstamped) is False
    _stub_metrics.duplicate_fill_total.inc.assert_not_called()


def test_legacy_boundary_is_a_wall_clock_stamp_taken_at_load(persist_path) -> None:
    _write_legacy_window(persist_path, REISSUED_SEQNO)
    t0 = timebase.now_ns()
    router = _make_router()
    t1 = timebase.now_ns()

    assert t0 <= router._legacy_fill_boundary_ns <= t1


@pytest.mark.asyncio
async def test_legacy_window_expires_after_the_ttl(persist_path, _stub_metrics, monkeypatch) -> None:
    monkeypatch.setenv("HFT_FILL_DEDUP_LEGACY_TTL_S", "1800")
    _write_legacy_window(persist_path, REISSUED_SEQNO)
    clock = {"now": 5_000.0}
    monkeypatch.setattr(
        "hft_platform.execution.router.time",
        types.SimpleNamespace(monotonic=lambda: clock["now"]),
    )
    store = PositionStore()
    router = _make_router(store)
    assert REISSUED_SEQNO in router._legacy_fill_ids

    clock["now"] += 1800.0 + 1.0
    with patch("hft_platform.execution.router.logger") as log:
        await _run_router(router, _deal(seqno=REISSUED_SEQNO, ordno="ORD_A"))

    assert _net_qty(store) == 1, "the reissued seqno was still treated as a legacy duplicate after the TTL"
    assert not router._legacy_fill_ids
    _stub_metrics.duplicate_fill_total.inc.assert_not_called()
    events = [c.args[0] for c in log.info.call_args_list if c.args]
    assert events.count("fill_dedup_legacy_window_expired") == 1


@pytest.mark.asyncio
async def test_legacy_window_expiry_is_logged_once(persist_path, _stub_metrics, monkeypatch) -> None:
    _write_legacy_window(persist_path, REISSUED_SEQNO, "synth_TXFD6|ORD_A|BUY|1230000|1|5")
    clock = {"now": 100.0}
    monkeypatch.setattr(
        "hft_platform.execution.router.time",
        types.SimpleNamespace(monotonic=lambda: clock["now"]),
    )
    router = _make_router()
    clock["now"] += 1800.0 + 1.0

    with patch("hft_platform.execution.router.logger") as log:
        await _run_router(
            router,
            _deal(seqno="S1", ordno="ORD_1", ts_ns=NIGHT_TS_NS),
            _deal(seqno="S2", ordno="ORD_2", ts_ns=DAY_TS_NS),
        )

    events = [c.args[0] for c in log.info.call_args_list if c.args]
    assert events.count("fill_dedup_legacy_window_expired") == 1
    assert not router._legacy_fill_ids


def test_legacy_ttl_is_configurable_from_the_environment(persist_path, monkeypatch) -> None:
    _write_legacy_window(persist_path, REISSUED_SEQNO)
    clock = {"now": 100.0}
    monkeypatch.setattr(
        "hft_platform.execution.router.time",
        types.SimpleNamespace(monotonic=lambda: clock["now"]),
    )
    monkeypatch.setenv("HFT_FILL_DEDUP_LEGACY_TTL_S", "60")
    router = _make_router()

    assert router._legacy_fill_deadline_seconds == pytest.approx(160.0)


def test_v2_entries_in_the_window_file_are_not_legacy(persist_path) -> None:
    from hft_platform.execution.router import fill_dedup_key

    fill = _orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=NIGHT_TS_NS)
    key = fill_dedup_key(fill)
    _write_legacy_window(persist_path, key)
    router = _make_router()

    assert key in router._seen_fill_ids
    assert not router._legacy_fill_ids


# ---------------------------------------------------------------------------
# (d) the persisted window holds only v2 keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persisted_window_contains_only_v2_keys(persist_path) -> None:
    # A legacy entry present at load must not be written back.
    _write_legacy_window(persist_path, "OLD_SEQ_1")
    store = PositionStore()
    router = _make_router(store)

    await _run_router(
        router,
        _deal(seqno=REISSUED_SEQNO, ordno="ORD_A", ts_ns=NIGHT_TS_NS),
        _deal(seqno=REISSUED_SEQNO, ordno="ORD_B", ts_ns=DAY_TS_NS),
    )
    await router.flush_fill_dedup()
    router.persist_fill_dedup()

    with open(persist_path, "rb") as f:
        persisted = [orjson.loads(line) for line in f if line.strip()]
    assert len(persisted) == 2
    assert all(key.startswith("v2|") for key in persisted), persisted
    assert "OLD_SEQ_1" not in persisted


# ---------------------------------------------------------------------------
# (e) R47's own dedup
# ---------------------------------------------------------------------------


def _r47():
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    strat = R47MakerStrategy(strategy_id="r47_maker", max_pos=5)
    strat.symbols = {"TMFD6"}
    ctx = MagicMock()
    ctx.positions = {}
    ctx.strategy_id = "r47_maker"
    strat.ctx = ctx
    strat._generated_intents = []
    return strat


def _r47_fill(*, fill_id: str, order_id: str, match_ts_ns: int) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        account_id="acct",
        order_id=order_id,
        strategy_id="r47_maker",
        symbol="TMFD6",
        side=Side.BUY,
        qty=1,
        price=1_000_0000,
        fee=0,
        tax=0,
        ingest_ts_ns=match_ts_ns,
        match_ts_ns=match_ts_ns,
    )


def test_r47_accepts_a_reused_fill_id_on_a_different_order_and_ts() -> None:
    strat = _r47()

    strat.on_fill(_r47_fill(fill_id=REISSUED_SEQNO, order_id="R47_MAKER_TMF:33978", match_ts_ns=NIGHT_TS_NS))
    strat.on_fill(_r47_fill(fill_id=REISSUED_SEQNO, order_id="R47_MAKER_TMF:40001", match_ts_ns=DAY_TS_NS))

    assert strat._local_pos["TMFD6"] == 2, "R47 dropped the day fill that reused the night seqno"


def test_r47_still_skips_an_identical_redelivery() -> None:
    strat = _r47()
    fill = _r47_fill(fill_id=REISSUED_SEQNO, order_id="R47_MAKER_TMF:33978", match_ts_ns=NIGHT_TS_NS)
    redelivery = _r47_fill(fill_id=REISSUED_SEQNO, order_id="R47_MAKER_TMF:33978", match_ts_ns=NIGHT_TS_NS)

    strat.on_fill(fill)
    strat.on_fill(redelivery)

    assert strat._local_pos["TMFD6"] == 1


def test_r47_dedup_key_is_the_routers_key() -> None:
    from hft_platform.execution.router import fill_dedup_key

    strat = _r47()
    fill = _r47_fill(fill_id=REISSUED_SEQNO, order_id="R47_MAKER_TMF:33978", match_ts_ns=NIGHT_TS_NS)

    strat.on_fill(fill)

    assert strat._seen_fill_ids == {fill_dedup_key(fill)}


# ---------------------------------------------------------------------------
# (f) DLQ replay uses the same key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dlq_resolved_fill_whose_v2_key_was_applied_is_skipped(persist_path, _stub_metrics) -> None:
    from hft_platform.execution.router import fill_dedup_key

    store = PositionStore()
    router = _make_router(store)
    router._order_id_map["ORD_A"] = "strat1:1"
    applied = _orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=NIGHT_TS_NS)
    router._register_fill_dedup_key(fill_dedup_key(applied))

    get_orphaned_fill_dlq().add(_orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=NIGHT_TS_NS))
    with patch("hft_platform.execution.router.logger") as log:
        await router._retry_orphaned_fills()

    assert _net_qty(store) == 0
    assert _stub_metrics.duplicate_fill_total.inc.call_count == 1
    events = [c.args[0] for c in log.warning.call_args_list if c.args]
    assert "duplicate_fill_skipped_dlq" in events


@pytest.mark.asyncio
async def test_dlq_resolved_fill_with_a_reissued_seqno_on_another_order_is_applied(persist_path, _stub_metrics) -> None:
    from hft_platform.execution.router import fill_dedup_key

    store = PositionStore()
    router = _make_router(store)
    router._order_id_map["ORD_B"] = "strat1:2"
    night = _orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=NIGHT_TS_NS)
    router._register_fill_dedup_key(fill_dedup_key(night))

    get_orphaned_fill_dlq().add(_orphan(fill_id=REISSUED_SEQNO, order_id="ORD_B", match_ts_ns=DAY_TS_NS))
    await router._retry_orphaned_fills()

    assert _net_qty(store) == 1
    _stub_metrics.duplicate_fill_total.inc.assert_not_called()


@pytest.mark.asyncio
async def test_dlq_resolved_fill_matching_a_legacy_entry_is_skipped_within_the_ttl(persist_path, _stub_metrics) -> None:
    _write_legacy_window(persist_path, REISSUED_SEQNO)
    store = PositionStore()
    router = _make_router(store)
    router._order_id_map["ORD_A"] = "strat1:1"

    get_orphaned_fill_dlq().add(_orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=NIGHT_TS_NS))
    with patch("hft_platform.execution.router.logger") as log:
        await router._retry_orphaned_fills()

    assert _net_qty(store) == 0
    assert _stub_metrics.duplicate_fill_total.inc.call_count == 1
    events = [c.args[0] for c in log.warning.call_args_list if c.args]
    assert "duplicate_fill_skipped_legacy" in events


@pytest.mark.asyncio
async def test_recover_fill_gaps_skips_a_fill_whose_v2_key_was_applied(persist_path) -> None:
    from hft_platform.execution.router import fill_dedup_key

    store = PositionStore()
    router = _make_router(store)
    router._order_id_map["ORD_A"] = "strat1:1"
    applied = _orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=NIGHT_TS_NS)
    router._register_fill_dedup_key(fill_dedup_key(applied))

    dlq = get_orphaned_fill_dlq()
    dlq.add(_orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=NIGHT_TS_NS))
    dlq.add(_orphan(fill_id=REISSUED_SEQNO, order_id="ORD_A", match_ts_ns=DAY_TS_NS))
    result = await router.recover_fill_gaps(checkpoint_path="/nonexistent")

    assert result["skipped_dedup"] == 1
    assert result["resolved"] == 1
    assert _net_qty(store) == 1


# ---------------------------------------------------------------------------
# shutdown drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_drain_applies_a_reissued_seqno_and_skips_a_redelivery(persist_path) -> None:
    store = PositionStore()
    router = _make_router(store)
    router._order_id_map["ORD_A"] = "strat1:1"
    router._order_id_map["ORD_B"] = "strat1:2"

    router.raw_queue.put_nowait(_deal(ordno="ORD_A", ts_ns=NIGHT_TS_NS))
    router.raw_queue.put_nowait(_deal(ordno="ORD_A", ts_ns=NIGHT_TS_NS))  # redelivery
    router.raw_queue.put_nowait(_deal(ordno="ORD_B", ts_ns=DAY_TS_NS))  # reissued seqno
    drained = await router.stop(drain_timeout_s=2.0)

    assert drained == 2
    assert _net_qty(store) == 2
