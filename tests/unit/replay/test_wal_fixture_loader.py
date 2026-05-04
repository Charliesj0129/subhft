"""Tests for WAL fixture loader (Slice C task 1).

Builds synthetic .tar.gz fixtures via `tarfile` mirroring the .wal/archive
layout: each member is a .jsonl shard with a header line carrying
`__wal_table__` followed by JSON-encoded body rows.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest


def _write_shard(
    tar: tarfile.TarFile,
    name: str,
    header: dict,
    rows: list[dict],
) -> None:
    """Add a single .jsonl shard (header + body rows) to the tar."""
    lines = [json.dumps(header)]
    lines.extend(json.dumps(r) for r in rows)
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    tar.addfile(info, io.BytesIO(payload))


def _build_fixture(path: Path, shards: list[tuple[str, dict, list[dict]]]) -> Path:
    """Create a .tar.gz fixture with the given (name, header, rows) shards."""
    with tarfile.open(path, "w:gz") as tar:
        for name, header, rows in shards:
            _write_shard(tar, name, header, rows)
    return path


def test_load_market_data_events_orders_by_exch_ts(tmp_path: Path) -> None:
    from hft_platform.replay.wal_fixture_loader import load_market_data_events

    fixture = tmp_path / "fixture.tar.gz"
    header = {"__wal_table__": "hft.market_data"}
    _build_fixture(
        fixture,
        [
            (
                "shard_b.jsonl",
                header,
                [{"symbol": "TMFD6", "exch_ts": 200, "bid": 1000000}],
            ),
            (
                "shard_a.jsonl",
                header,
                [{"symbol": "TMFD6", "exch_ts": 100, "bid": 999900}],
            ),
        ],
    )

    rows = list(load_market_data_events(fixture))

    assert [r["exch_ts"] for r in rows] == [100, 200]


def test_load_market_data_events_skips_non_market_data(tmp_path: Path) -> None:
    from hft_platform.replay.wal_fixture_loader import load_market_data_events

    fixture = tmp_path / "fills_only.tar.gz"
    _build_fixture(
        fixture,
        [
            (
                "fills.jsonl",
                {"__wal_table__": "hft.fills"},
                [{"symbol": "TMFD6", "exch_ts": 1, "qty": 1}],
            ),
        ],
    )

    rows = list(load_market_data_events(fixture))

    assert rows == []


def test_load_market_data_events_rejects_missing_fixture(tmp_path: Path) -> None:
    from hft_platform.replay.wal_fixture_loader import (
        FixtureLoadError,
        load_market_data_events,
    )

    missing = tmp_path / "does_not_exist.tar.gz"

    with pytest.raises(FixtureLoadError):
        list(load_market_data_events(missing))


def test_load_market_data_events_filters_by_symbol(tmp_path: Path) -> None:
    from hft_platform.replay.wal_fixture_loader import load_market_data_events

    fixture = tmp_path / "mixed.tar.gz"
    header = {"__wal_table__": "hft.market_data"}
    _build_fixture(
        fixture,
        [
            (
                "mixed.jsonl",
                header,
                [
                    {"symbol": "TMFD6", "exch_ts": 100, "bid": 999900},
                    {"symbol": "TXFD6", "exch_ts": 150, "bid": 1500000},
                    {"symbol": "TMFD6", "exch_ts": 200, "bid": 1000000},
                ],
            ),
        ],
    )

    rows = list(load_market_data_events(fixture, symbols={"TMFD6"}))

    assert [r["symbol"] for r in rows] == ["TMFD6", "TMFD6"]
    assert [r["exch_ts"] for r in rows] == [100, 200]
