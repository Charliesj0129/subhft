"""Integration test: SHM snapshot write → read → ShmDataSource roundtrip."""

from __future__ import annotations

import os
import uuid

import pytest

try:
    from hft_platform.ipc.shm_snapshot import (
        ShmSnapshotReader,
        ShmSnapshotWriter,
        _symbol_hash,
    )

    HAS_SHM = True
except (ImportError, OSError):
    HAS_SHM = False

pytestmark = [
    pytest.mark.skipif(not HAS_SHM, reason="ShmSnapshotTable (Rust) not available"),
    pytest.mark.integration,
]

_PLATFORM_TO_CH_SCALE = 100  # x10000 → x1000000


@pytest.fixture()
def shm_name():
    name = f"test_monitor_shm_{uuid.uuid4().hex[:8]}"
    yield name
    try:
        import posixipc

        posixipc.unlink_shared_memory(f"/{name}")
    except Exception:
        try:
            os.unlink(f"/dev/shm/{name}")
        except Exception:
            pass


def _make_lob_fields(best_bid: int = 2105000, best_ask: int = 2106000) -> list[int]:
    """9 LOB fields in platform x10000 scale."""
    return [best_bid, best_ask, 0, 0, 0, 0, 50, 30, 0]


def _make_features(n: int = 16) -> list[int]:
    return [i * 10 for i in range(n)]


# ------------------------------------------------------------------
# 1. Writer → Reader roundtrip
# ------------------------------------------------------------------


def test_writer_reader_roundtrip(shm_name: str) -> None:
    writer = ShmSnapshotWriter(shm_name, max_symbols=4)
    ts = 1_700_000_000_000_000_000
    sym_hash = _symbol_hash("2330")
    lob = _make_lob_fields()
    feats = _make_features()

    writer.publish(slot_idx=0, ts_ns=ts, symbol_hash=sym_hash, lob_fields=lob, features=feats)

    reader = ShmSnapshotReader(shm_name, max_symbols=4)
    slot = reader.read_slot(0)

    assert slot is not None
    assert slot.ts_ns == ts
    assert slot.symbol_hash == sym_hash
    assert slot.lob_fields == tuple(lob)
    assert slot.features == tuple(feats)
    assert slot.version >= 1


# ------------------------------------------------------------------
# 2. ShmDataSource.poll converts to CH scale (x1000000)
# ------------------------------------------------------------------


def test_data_source_poll_converts_scale(shm_name: str) -> None:
    from hft_platform.monitor._data_source import ShmDataSource

    best_bid = 2105000  # 210.5 in x10000
    best_ask = 2106000
    ts = 1_700_000_000_000_000_000
    sym = "2330"

    writer = ShmSnapshotWriter(shm_name, max_symbols=4)
    writer.publish(
        slot_idx=0,
        ts_ns=ts,
        symbol_hash=_symbol_hash(sym),
        lob_fields=_make_lob_fields(best_bid, best_ask),
        features=_make_features(),
    )

    ds = ShmDataSource(shm_name=shm_name, max_symbols=4, symbols=(sym,))
    assert ds.connected

    rows = ds.poll({sym: 0})
    assert sym in rows
    assert len(rows[sym]) == 1

    row = rows[sym][0]
    assert row.bids_price == [best_bid * _PLATFORM_TO_CH_SCALE]
    assert row.asks_price == [best_ask * _PLATFORM_TO_CH_SCALE]
    assert row.bids_vol == [50]
    assert row.asks_vol == [30]
    assert row.ingest_ts == ts


# ------------------------------------------------------------------
# 3. Second poll with no update → empty (version tracking)
# ------------------------------------------------------------------


def test_data_source_skips_unchanged_version(shm_name: str) -> None:
    from hft_platform.monitor._data_source import ShmDataSource

    sym = "2881"
    ts = 1_700_000_000_000_000_000

    writer = ShmSnapshotWriter(shm_name, max_symbols=4)
    writer.publish(
        slot_idx=0,
        ts_ns=ts,
        symbol_hash=_symbol_hash(sym),
        lob_fields=_make_lob_fields(),
        features=_make_features(),
    )

    ds = ShmDataSource(shm_name=shm_name, max_symbols=4, symbols=(sym,))
    first = ds.poll({sym: 0})
    assert len(first[sym]) == 1

    second = ds.poll({sym: 0})
    assert len(second[sym]) == 0


# ------------------------------------------------------------------
# 4. Lazy discovery of a new symbol
# ------------------------------------------------------------------


def test_data_source_discovers_new_symbol(shm_name: str) -> None:
    from hft_platform.monitor._data_source import ShmDataSource

    sym_a = "2330"
    sym_b = "2454"
    ts = 1_700_000_000_000_000_000

    writer = ShmSnapshotWriter(shm_name, max_symbols=4)
    # Only sym_a exists at init time
    writer.publish(
        slot_idx=0,
        ts_ns=ts,
        symbol_hash=_symbol_hash(sym_a),
        lob_fields=_make_lob_fields(),
        features=_make_features(),
    )

    ds = ShmDataSource(shm_name=shm_name, max_symbols=4, symbols=(sym_a, sym_b))
    assert sym_a in ds._symbol_to_slot
    assert sym_b not in ds._symbol_to_slot

    # Publish sym_b after init
    writer.publish(
        slot_idx=1,
        ts_ns=ts + 1,
        symbol_hash=_symbol_hash(sym_b),
        lob_fields=_make_lob_fields(3000000, 3001000),
        features=_make_features(),
    )

    rows = ds.poll({sym_a: 0, sym_b: 0})
    assert len(rows.get(sym_b, [])) == 1
    assert sym_b in ds._symbol_to_slot
