"""Differential tests: ``hft_platform.utils.jsonfast`` (msgspec) against live orjson.

orjson stays importable on Python 3.12 only as a test-time reference; it is
skipped where it is not installed (cp314t). These tests pin, case by case:

* where the new codec is byte-identical to the one it replaced (everything the
  engine persists, and everything hashed: checkpoint SHA-256, WAL batch hash);
* the single byte difference (positive-exponent floats, ``1e+16`` vs ``1e16``);
* what msgspec accepts that orjson refused (a superset);
* that files written by one codec load through the real readers using the other.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import hashlib
import os
import random
import struct
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.utils import jsonfast

orjson = pytest.importorskip("orjson")

_BIG = 2**53 + 1

_PERSISTED_SHAPES = {
    "scaled_price_row": {"symbol": "TXFD6", "price_scaled": 331235000, "volume": 3, "ingest_ts": 1763884800000123456},
    "depth5_book": {
        "bids_price": [331230000, 331220000, 331210000, 331200000, 331190000],
        "asks_price": [331240000, 331250000, 331260000, 331270000, 331280000],
        "bids_vol": [5, 6, 7, 8, 9],
        "asks_vol": [1, 2, 3, 4, 5],
    },
    "ints_beyond_2_pow_53": {"a": _BIG, "b": -_BIG, "c": 2**63 - 1, "d": -(2**63), "e": 2**64 - 1},
    "floats_below_1e16": {"a": 0.1, "b": -0.0, "c": 1.0, "d": 123456789.12345679, "e": 9007199254740992.0, "f": 1.5e-7},
    "none_bool_empty": {"n": None, "t": True, "f": False, "s": "", "l": [], "d": {}},
    "unicode_text": {"name": "台灣 café 😀", "tab": "a\tb", "quote": 'say "hi"', "ctl": "x\x01y", "slash": "a/b"},
    "nested_mixed": {"z": [{"b": [1, 2.5, None]}, {"a": {"y": 1, "x": [True]}}], "a": chr(0x2028)},
    "wal_batch_header": {"__wal_table__": "hft.market_data", "__row_count__": 3},
    "order_id_map_entry": {"k": "TOKEN1", "v": "r47:100", "t_ns": 1763884800000000000, "s": "live"},
    "checkpoint_body": {
        "trading_date": "2026-11-23",
        "timestamp_ns": 1763884800123456789,
        "peak_equity_scaled": 9223372036854775807,
        "total_realized_pnl_scaled": -_BIG,
        "positions": {"acc:r47:台指期": {"symbol": "台指期", "net_qty": -3, "avg_price_scaled": -1}},
    },
    "non_finite_floats": {"nan": float("nan"), "inf": float("inf"), "ninf": float("-inf")},
    "tuple_as_array": {"t": (1, 2, (3, 4))},
}


@pytest.mark.parametrize("shape", sorted(_PERSISTED_SHAPES))
def test_dumps_bytes_equal_orjson_for_persisted_shape(shape):
    obj = _PERSISTED_SHAPES[shape]

    assert jsonfast.dumps(obj) == orjson.dumps(obj)


@pytest.mark.parametrize("shape", sorted(_PERSISTED_SHAPES))
def test_dumps_sorted_bytes_equal_orjson_sort_keys_for_persisted_shape(shape):
    obj = _PERSISTED_SHAPES[shape]

    assert jsonfast.dumps_sorted(obj) == orjson.dumps(obj, option=orjson.OPT_SORT_KEYS)


@pytest.mark.parametrize("shape", sorted(_PERSISTED_SHAPES))
def test_orjson_written_bytes_decode_identically_with_jsonfast(shape):
    written = orjson.dumps(_PERSISTED_SHAPES[shape])

    assert jsonfast.loads(written) == orjson.loads(written)


@pytest.mark.parametrize("shape", sorted(_PERSISTED_SHAPES))
def test_jsonfast_written_bytes_decode_identically_with_orjson(shape):
    written = jsonfast.dumps(_PERSISTED_SHAPES[shape])

    assert orjson.loads(written) == jsonfast.loads(written)


def test_decode_matches_orjson_for_integer_and_float_types():
    text = b"[1,-0,1.0,-0.0,1e5,1E5,1.5e-3,9007199254740993,18446744073709551615,-9223372036854775808,0.1,4.9e-324]"

    fast, ref = jsonfast.loads(text), orjson.loads(text)

    assert fast == ref
    assert [type(v) for v in fast] == [type(v) for v in ref]


def test_random_floats_below_1e16_encode_identically():
    rng = random.Random(20261123)
    values = [rng.uniform(-1e6, 1e6) for _ in range(20_000)]
    values += [round(rng.uniform(-1e6, 1e6), rng.randint(0, 8)) for _ in range(20_000)]
    values += [rng.random() * 10 ** rng.randint(-30, 15) for _ in range(20_000)]
    while len(values) < 80_000:
        value = struct.unpack("<d", struct.pack("<Q", rng.getrandbits(64)))[0]
        if value == value and abs(value) < 1e16:
            values.append(value)

    assert jsonfast.dumps(values) == orjson.dumps(values)


def test_every_unicode_code_point_encodes_identically():
    code_points = [cp for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF]
    chunks = ["".join(map(chr, code_points[i : i + 997])) for i in range(0, len(code_points), 997)]

    assert jsonfast.dumps(chunks) == orjson.dumps(chunks)


def test_random_unicode_key_dicts_sort_identically():
    rng = random.Random(7)
    spans = [(32, 126), (0x80, 0x7FF), (0x800, 0xD7FF), (0xE000, 0xFFFF), (0x10000, 0x10FFFF)]

    def key() -> str:
        return "".join(chr(rng.randint(*rng.choice(spans))) for _ in range(rng.randint(0, 4)))

    docs = [{key(): {key(): i, key(): [{key(): 1, key(): 2}]} for _ in range(rng.randint(0, 6))} for i in range(5_000)]

    assert jsonfast.dumps_sorted(docs) == orjson.dumps(docs, option=orjson.OPT_SORT_KEYS)


@pytest.mark.parametrize("value", [1e16, 1e17, 1e22, 1.5e300, 1.7976931348623157e308, -2.5e20])
def test_positive_exponent_floats_differ_from_orjson_only_in_exponent_sign(value):
    fast, ref = jsonfast.dumps(value), orjson.dumps(value)

    assert fast != ref
    assert ref.replace(b"e+", b"e") == fast
    assert jsonfast.loads(ref) == jsonfast.loads(fast) == orjson.loads(fast) == value


@pytest.mark.parametrize("value", [1e-5, 1.5e-7, 5e-324, 2.2250738585072014e-308, 1e-100])
def test_negative_exponent_floats_encode_identically(value):
    assert jsonfast.dumps(value) == orjson.dumps(value)


def test_non_finite_floats_encode_as_null_in_both_codecs():
    for value in (float("nan"), float("inf"), float("-inf")):
        assert jsonfast.dumps(value) == orjson.dumps(value) == b"null"


@pytest.mark.parametrize("text", [b"NaN", b"Infinity", b"-Infinity", b"1e400", b"", b'{"a":1,}', b'"\xff"', b"[1 2]"])
def test_malformed_input_is_rejected_by_both_codecs_with_value_error(text):
    with pytest.raises(ValueError):
        orjson.loads(text)
    with pytest.raises(ValueError):
        jsonfast.loads(text)


def test_ints_beyond_64_bits_are_exact_in_jsonfast_and_lossy_in_orjson():
    text = b"123456789012345678901234567890"

    assert isinstance(orjson.loads(text), float)
    assert jsonfast.loads(text) == 123456789012345678901234567890
    with pytest.raises(TypeError):
        orjson.dumps(2**64)
    assert jsonfast.dumps(2**64) == b"18446744073709551616"


def test_int_dict_keys_match_orjson_non_str_keys_option_and_are_refused_by_plain_orjson():
    with pytest.raises(TypeError):
        orjson.dumps({1: "a"})

    assert jsonfast.dumps({1: "a", 2: "b"}) == orjson.dumps({1: "a", 2: "b"}, option=orjson.OPT_NON_STR_KEYS)


@pytest.mark.parametrize("value", [b"ab", bytearray(b"ab"), {1, 2}, frozenset({1})])
def test_bytes_and_sets_encode_in_jsonfast_where_orjson_refuses(value):
    with pytest.raises(TypeError):
        orjson.dumps(value)

    assert jsonfast.loads(jsonfast.dumps(value)) is not None


def test_numpy_values_are_refused_by_both_codecs_without_serialize_numpy():
    np = pytest.importorskip("numpy")

    for value in (np.float64(1.5), np.float32(1.5), np.int64(5), np.bool_(True), np.arange(3)):
        with pytest.raises(TypeError):
            orjson.dumps(value)
        with pytest.raises(TypeError):
            jsonfast.dumps(value)


def test_str_and_int_subclasses_encode_identically():
    np = pytest.importorskip("numpy")

    class StrSub(str):
        __slots__ = ()

    class IntSub(int):
        __slots__ = ()

    obj = {"s": StrSub("x"), "i": IntSub(3), "np_str": np.str_("npx")}

    assert jsonfast.dumps(obj) == orjson.dumps(obj)


def test_utc_datetime_spelling_differs_from_orjson_and_naive_matches():
    naive = datetime.datetime(2020, 1, 2, 3, 4, 5)
    utc = datetime.datetime(2020, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)

    assert jsonfast.dumps(naive) == orjson.dumps(naive)
    assert orjson.dumps(utc) == b'"2020-01-02T03:04:05+00:00"'
    assert jsonfast.dumps(utc) == b'"2020-01-02T03:04:05Z"'


# --------------------------------------------------------------------------- #
# Cross-codec files through the real readers and writers                       #
# --------------------------------------------------------------------------- #


def _checkpoint_store() -> MagicMock:
    def _pos(symbol, net_qty, avg, realized, fees):
        pos = MagicMock()
        pos.symbol = symbol
        pos.net_qty = net_qty
        pos.avg_price_scaled = avg
        pos.realized_pnl_scaled = realized
        pos.fees_scaled = fees
        return pos

    positions = {
        "acc:r47:TMFK6": _pos("TMFK6", -3, 331235000, -1250000, 42000),
        "acc:r47:台指期": _pos("台指期", 1, 331235000, _BIG, 7),
    }
    store = MagicMock()
    store._peak_equity_scaled = 9223372036854775807
    store._total_realized_pnl_scaled = -_BIG
    store.snapshot_positions_with_recovery.return_value = (positions, {})
    return store


def test_checkpoint_written_by_jsonfast_verifies_under_the_orjson_algorithm(tmp_path):
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    path = tmp_path / "ckpt.json"
    PositionCheckpointWriter(_checkpoint_store(), path=str(path), interval_s=60).write_checkpoint()
    raw = path.read_bytes()

    data = orjson.loads(raw)
    stored_sha = data.pop("sha256")
    assert hashlib.sha256(orjson.dumps(data, option=orjson.OPT_SORT_KEYS)).hexdigest() == stored_sha
    data["sha256"] = stored_sha
    assert raw == orjson.dumps(data, option=orjson.OPT_SORT_KEYS)


def test_checkpoint_written_by_orjson_loads_through_the_jsonfast_reader(tmp_path):
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    body = {
        "trading_date": "2026-11-23",
        "timestamp_ns": 1763884800123456789,
        "peak_equity_scaled": 9223372036854775807,
        "total_realized_pnl_scaled": -_BIG,
        "positions": {"acc:r47:台指期": {"symbol": "台指期", "net_qty": -3, "avg_price_scaled": 331235000}},
    }
    sha = hashlib.sha256(orjson.dumps(body, option=orjson.OPT_SORT_KEYS)).hexdigest()
    path = tmp_path / "ckpt.json"
    path.write_bytes(orjson.dumps({**body, "sha256": sha}, option=orjson.OPT_SORT_KEYS))

    loaded = PositionCheckpointWriter.load_checkpoint(str(path))

    assert loaded == {**body, "sha256": sha}


def test_wal_lines_written_by_jsonfast_decode_with_orjson_and_back(tmp_path):
    from hft_platform.recorder import _loader_common
    from hft_platform.recorder.wal import WALWriter

    rows = [_PERSISTED_SHAPES["scaled_price_row"], _PERSISTED_SHAPES["depth5_book"], _PERSISTED_SHAPES["unicode_text"]]
    target = tmp_path / "t_1_0.jsonl"
    WALWriter(str(tmp_path))._write_sync_atomic(str(target), rows)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert [orjson.loads(line) for line in lines] == rows
    assert [_loader_common._loads(orjson.dumps(row).decode()) for row in rows] == rows
    assert lines == [orjson.dumps(row).decode() for row in rows]


def test_fill_dlq_lines_cross_codecs(tmp_path):
    from hft_platform.execution.fill_dlq import OrphanedFillDLQ

    fill = FillEvent(
        fill_id="F-1",
        account_id="acc",
        order_id="O-1",
        strategy_id="r47",
        symbol="台指期",
        side=Side.SELL,
        qty=2,
        price=331235000,
        fee=21000,
        tax=1000,
        ingest_ts_ns=_BIG,
        match_ts_ns=_BIG + 1,
    )
    written = tmp_path / "written.jsonl"
    dlq = OrphanedFillDLQ(persist_path=str(written))
    dlq.add(fill)
    dlq.persist()
    assert orjson.loads(written.read_bytes()) == dataclasses.asdict(fill)

    legacy = tmp_path / "legacy.jsonl"
    legacy.write_bytes(orjson.dumps(dataclasses.asdict(fill)) + b"\n")
    reloaded = OrphanedFillDLQ(persist_path=str(legacy))
    reloaded.load()
    assert reloaded.drain() == [fill]


def test_idempotency_store_files_cross_codecs(tmp_path):
    from hft_platform.gateway.dedup import IdempotencyStore

    written = tmp_path / "written.jsonl"
    store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=str(written))
    store.check_or_reserve("k-1")
    store.commit("k-1", approved=False, reason_code="HALT", cmd_id=2**53 + 3)
    store.persist()
    assert orjson.loads(written.read_bytes()) == {
        "key": "k-1",
        "approved": False,
        "reason_code": "HALT",
        "cmd_id": 2**53 + 3,
    }

    legacy = tmp_path / "legacy.jsonl"
    legacy.write_bytes(orjson.dumps({"key": "k-9", "approved": True, "reason_code": "OK", "cmd_id": 9}) + b"\n")
    loaded = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=str(legacy))
    loaded.load()
    record = loaded.check_or_reserve("k-9")
    assert record is not None
    assert (record.approved, record.reason_code, record.cmd_id) == (True, "OK", 9)


def _router(persist_path: str):
    from hft_platform.execution.router import ExecutionRouter

    with patch.dict(os.environ, {"HFT_FILL_DEDUP_PERSIST_PATH": persist_path}):
        return ExecutionRouter(
            bus=MagicMock(),
            raw_queue=asyncio.Queue(),
            order_id_map={},
            position_store=MagicMock(),
            terminal_handler=MagicMock(),
        )


def test_fill_dedup_file_crosses_codecs(tmp_path):
    keys = ["F-1:O-1:台", "F-2", "x" * 300, 'q"uote']
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_bytes(b"".join(orjson.dumps(k) + b"\n" for k in keys))
    assert _router(str(legacy))._legacy_fill_ids == set(keys)

    written = tmp_path / "written.jsonl"
    router = _router(str(written))
    for key in keys:
        router._seen_fill_ids[key] = None
    router.persist_fill_dedup()
    assert written.read_bytes() == b"".join(orjson.dumps(k) + b"\n" for k in keys)


@pytest.mark.parametrize("shape", ["scaled_price_row", "floats_below_1e16", "unicode_text", "ints_beyond_2_pow_53"])
def test_wal_dedup_hash_equals_sha256_of_orjson_sorted_bytes(shape):
    from hft_platform.recorder._loader_ch import insert_with_dedup

    rows = [_PERSISTED_SHAPES[shape], {"b": 2, "a": [1, None, True, 1.5e-7]}]
    captured: list[str] = []
    svc = MagicMock()
    svc._dedup_enabled = True
    svc.ch_client = MagicMock()
    svc._is_duplicate.return_value = False
    svc.insert_batch.return_value = True
    svc._record_dedup.side_effect = lambda table, content_hash, row_count: captured.append(content_hash)

    insert_with_dedup(svc, "hft.market_data", rows, "f.jsonl")

    expected = hashlib.sha256(b"".join(orjson.dumps(r, option=orjson.OPT_SORT_KEYS) for r in rows)).hexdigest()
    assert captured == [expected]
