"""Tests for deterministic strategy replay harness (Slice C task 4).

The harness drives a strategy over a market-data fixture using a patched
clock and seeded RNG; two replays of the same config must yield byte-identical
canonical hashes. These tests build a tiny in-test ``.tar.gz`` fixture
(mirroring ``tests/unit/replay/test_wal_fixture_loader.py``) and a tiny
test-only strategy that emits one ``OrderIntent`` per event.
"""

from __future__ import annotations

import io
import json
import random
import tarfile
from pathlib import Path
from typing import Any

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side

# ─── Fixture builder helpers (mirrors test_wal_fixture_loader pattern) ────


def _write_shard(
    tar: tarfile.TarFile,
    name: str,
    header: dict,
    rows: list[dict],
) -> None:
    lines = [json.dumps(header)]
    lines.extend(json.dumps(r) for r in rows)
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    tar.addfile(info, io.BytesIO(payload))


def _build_fixture(path: Path, shards: list[tuple[str, dict, list[dict]]]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, header, rows in shards:
            _write_shard(tar, name, header, rows)
    return path


def _market_data_rows(symbol: str = "TMFD6", n: int = 5) -> list[dict]:
    """Build N alternating BidAsk + Tick rows with monotonic exch_ts."""
    rows = []
    for i in range(n):
        exch_ts = 1_000_000_000 + i * 1_000_000  # 1ms steps
        if i % 2 == 0:
            # BidAsk row
            rows.append(
                {
                    "symbol": symbol,
                    "exch_ts": exch_ts,
                    "ingest_ts": exch_ts + 100,
                    "type": "BidAsk",
                    "bids_price": [9999_0000 - i * 10000, 9998_0000],
                    "bids_vol": [10, 20],
                    "asks_price": [10000_0000 + i * 10000, 10001_0000],
                    "asks_vol": [10, 20],
                    "seq_no": i,
                    "price_scaled": 0,
                    "volume": 0,
                }
            )
        else:
            # Tick row
            rows.append(
                {
                    "symbol": symbol,
                    "exch_ts": exch_ts,
                    "ingest_ts": exch_ts + 100,
                    "type": "Tick",
                    "price_scaled": 9999_0000 + i * 1000,
                    "volume": 1,
                    "bids_price": [],
                    "bids_vol": [],
                    "asks_price": [],
                    "asks_vol": [],
                    "seq_no": i,
                    "trade_direction": 1 if i % 4 == 1 else -1,
                }
            )
    return rows


@pytest.fixture
def tmp_fixture(tmp_path: Path) -> Path:
    """Build a deterministic 5-event WAL fixture."""
    fixture = tmp_path / "fixture.tar.gz"
    header = {"__wal_table__": "hft.market_data"}
    _build_fixture(fixture, [("shard.jsonl", header, _market_data_rows())])
    return fixture


# ─── Test-only strategy classes ───────────────────────────────────────────


class _DeterministicStrategy:
    """Test strategy: emits one OrderIntent per event, fields derived from event.

    Calls ``hft_platform.core.timebase.now_ns()`` so the harness's patch is
    exercised — proving the determinism contract holds.
    """

    def __init__(self, *, rng: random.Random | None = None) -> None:
        self.rng = rng
        self._counter = 0

    def handle_event(self, ctx: Any, event: Any) -> list[OrderIntent]:
        from hft_platform.core import timebase

        self._counter += 1
        # Pull a price from the event (BidAsk uses bids[0][0]; Tick uses price).
        if hasattr(event, "bids") and len(event.bids) > 0:
            price = int(event.bids[0][0])
        else:
            price = int(getattr(event, "price", 0))
        intent = OrderIntent(
            intent_id=self._counter,
            strategy_id="replay_test",
            symbol=getattr(event, "symbol", ""),
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=price,
            qty=1,
            tif=TIF.LIMIT,
            timestamp_ns=timebase.now_ns(),  # exercises the harness patch
            decision_price=price,
        )
        return [intent]


class _RandomizedStrategy(_DeterministicStrategy):
    """Test strategy: same as deterministic, but ``qty`` is RNG-driven so a
    different seed produces a different canonical hash.
    """

    def handle_event(self, ctx: Any, event: Any) -> list[OrderIntent]:
        intents = super().handle_event(ctx, event)
        if intents and self.rng is not None:
            # Mutate qty into a seed-dependent value (1..5).
            it = intents[0]
            it.qty = self.rng.randint(1, 5)
        return intents


def _make_det_strategy(*, rng: random.Random | None = None) -> _DeterministicStrategy:
    return _DeterministicStrategy(rng=rng)


def _make_rng_strategy(*, rng: random.Random | None = None) -> _RandomizedStrategy:
    return _RandomizedStrategy(rng=rng)


# ─── Tests ────────────────────────────────────────────────────────────────


def test_replay_is_deterministic(tmp_fixture: Path) -> None:
    from hft_platform.replay.strategy_replay import ReplayConfig, replay_strategy

    cfg = ReplayConfig(
        fixture_path=str(tmp_fixture),
        strategy_factory=_make_det_strategy,
        rng_seed=42,
    )

    log_a = replay_strategy(cfg)
    log_b = replay_strategy(cfg)

    assert log_a.n_intents() > 0, "Strategy should have emitted at least one intent"
    assert log_a.hash() == log_b.hash()


def test_replay_captures_intents_in_order(tmp_fixture: Path) -> None:
    from hft_platform.replay.strategy_replay import ReplayConfig, replay_strategy

    cfg = ReplayConfig(
        fixture_path=str(tmp_fixture),
        strategy_factory=_make_det_strategy,
        rng_seed=0,
    )

    log = replay_strategy(cfg)

    timestamps = [int(it.timestamp_ns) for it in log.intents]
    assert timestamps == sorted(timestamps), f"timestamp_ns must be monotonic; got {timestamps}"


def test_replay_respects_max_events(tmp_fixture: Path) -> None:
    from hft_platform.replay.strategy_replay import ReplayConfig, replay_strategy

    cfg = ReplayConfig(
        fixture_path=str(tmp_fixture),
        strategy_factory=_make_det_strategy,
        rng_seed=0,
        max_events=3,
    )

    log = replay_strategy(cfg)

    assert log.n_events_processed == 3
    # Each event yields exactly one intent in this test strategy.
    assert log.n_intents() == 3


def test_replay_rng_seed_changes_output_when_strategy_uses_rng(
    tmp_fixture: Path,
) -> None:
    from hft_platform.replay.strategy_replay import ReplayConfig, replay_strategy

    cfg_seed_1 = ReplayConfig(
        fixture_path=str(tmp_fixture),
        strategy_factory=_make_rng_strategy,
        rng_seed=1,
    )
    cfg_seed_2 = ReplayConfig(
        fixture_path=str(tmp_fixture),
        strategy_factory=_make_rng_strategy,
        rng_seed=2,
    )

    h1 = replay_strategy(cfg_seed_1).hash()
    h2 = replay_strategy(cfg_seed_2).hash()

    assert h1 != h2, "Distinct rng_seeds must produce distinct canonical hashes"
