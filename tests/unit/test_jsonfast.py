"""Tests for ``hft_platform.utils.jsonfast`` and the persisted-format compatibility.

The engine's JSON codec moved from orjson (no cp314t wheel) to ``msgspec.json``.
Everything the engine persists (checkpoint, WAL, fill DLQ, dedup files) was
written by orjson, so the new codec must read those bytes unchanged and write
bytes the old readers accept.

This module must NOT import orjson, so it also runs on the free-threaded
interpreter. The byte literals below were produced by orjson 3.11 (the
checkpoint with OPT_SORT_KEYS, as ``execution/checkpoint.py`` used it). The
differential tests against live orjson are in
``test_jsonfast_orjson_compat.py``.
"""

from __future__ import annotations

import ast
import asyncio
import collections.abc as abc
import dataclasses
import enum
import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import msgspec
import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.utils import jsonfast

_BIG = 2**53 + 1

# --------------------------------------------------------------------------- #
# Bytes written by orjson before the switch                                    #
# --------------------------------------------------------------------------- #

_LEGACY_CHECKPOINT = (
    b'{"identity":{"account":"0123456789abcdef","broker":"shioaji","venue":"paper"},'
    b'"peak_equity_scaled":9223372036854775807,'
    b'"positions":{"acc:r47:TMFK6":{"avg_price_scaled":331235000,"fees_scaled":42000,"net_qty":-3,'
    b'"realized_pnl_scaled":-1250000,"symbol":"TMFK6"},'
    b'"acc:r47:\xe5\x8f\xb0\xe6\x8c\x87\xe6\x9c\x9f":{"avg_price_scaled":-1,"fees_scaled":0,"net_qty":1,'
    b'"realized_pnl_scaled":9007199254740993,"symbol":"\xe5\x8f\xb0\xe6\x8c\x87\xe6\x9c\x9f","unknown_basis":true}},'
    b'"sha256":"52fc9b556e4e5c61298a1f4f0e26811ad39e934e6d6a7a7fffebc7ef370a6cff",'
    b'"timestamp_ns":1763884800123456789,"total_realized_pnl_scaled":-9007199254740993,'
    b'"trading_date":"2026-11-23"}'
)
_LEGACY_CHECKPOINT_SHA = "52fc9b556e4e5c61298a1f4f0e26811ad39e934e6d6a7a7fffebc7ef370a6cff"

_LEGACY_WAL_ROW = (
    b'{"symbol":"TXFD6","exch_ts":1763884800000000000,"ingest_ts":1763884800000123456,'
    b'"price_scaled":331235000,"volume":3,"bids_price":[331230000,331220000],"asks_price":[331240000],'
    b'"ratio":0.1,"tiny":1.5e-7,"none":null,"flag":true,'
    b'"name":"\xe5\x8f\xb0\xe7\x81\xa3 caf\xc3\xa9 \xf0\x9f\x98\x80","big":9007199254740993}'
)
_WAL_ROW = {
    "symbol": "TXFD6",
    "exch_ts": 1763884800000000000,
    "ingest_ts": 1763884800000123456,
    "price_scaled": 331235000,
    "volume": 3,
    "bids_price": [331230000, 331220000],
    "asks_price": [331240000],
    "ratio": 0.1,
    "tiny": 1.5e-7,
    "none": None,
    "flag": True,
    "name": "台灣 café 😀",
    "big": _BIG,
}
_LEGACY_WAL_HEADER = b'{"__wal_table__":"hft.market_data","__row_count__":3}'

_LEGACY_DLQ_LINE = (
    b'{"fill_id":"F-1","account_id":"acc","order_id":"O-1","strategy_id":"r47","symbol":"TMFK6","side":1,'
    b'"qty":2,"price":331235000,"fee":21000,"tax":1000,"ingest_ts_ns":1763884800000000001,'
    b'"match_ts_ns":1763884800000000002,"decision_price":331236000,"arrival_price":331234000,'
    b'"client_order_id":"r47:1","trace_id":"t-1"}'
)
_DLQ_FILL = FillEvent(
    fill_id="F-1",
    account_id="acc",
    order_id="O-1",
    strategy_id="r47",
    symbol="TMFK6",
    side=Side.SELL,
    qty=2,
    price=331235000,
    fee=21000,
    tax=1000,
    ingest_ts_ns=1763884800000000001,
    match_ts_ns=1763884800000000002,
    decision_price=331236000,
    arrival_price=331234000,
    client_order_id="r47:1",
    trace_id="t-1",
)

_LEGACY_DEDUP_COMMITTED = b'{"key":"k-1","approved":true,"reason_code":"OK","cmd_id":42}'
_LEGACY_DEDUP_RESERVED = b'{"key":"k-2","approved":null,"reason_code":"","cmd_id":0}'
_LEGACY_FILL_DEDUP_KEY = b'"F-1:O-1:\xe5\x8f\xb0"'
_LEGACY_ORDER_ID_MAP_LINE = b'{"k":"TOKEN1","v":"r47:100","t_ns":1763884800000000000,"s":"live"}'

_HASH_ROWS = [
    {"symbol": "TXFD6", "price_scaled": 331235000, "volume": 3, "ratio": 0.1, "name": "台灣", "big": _BIG},
    {"b": 2, "a": [1, None, True, 1.5e-7]},
]
_LEGACY_HASH_ROWS_SHA = "b354d3fb57945d64b8cd84636aeddbbbc1fa1605254ff8f07455cb887ea70f8b"


class _SdkMapping(abc.Mapping):
    """Mapping that is not a dict (the shape of the shioaji Rust payloads)."""

    def __init__(self, data: dict) -> None:
        self._d = data

    def __getitem__(self, key):
        return self._d[key]

    def __iter__(self):
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)


class _StrSub(str):
    __slots__ = ()

    def __str__(self) -> str:
        return "overridden"


class _IntSub(int):
    __slots__ = ()

    def __int__(self) -> int:
        return 999

    def __index__(self) -> int:
        return 999


class _Color(enum.Enum):
    RED = "red"


@dataclasses.dataclass(slots=True)
class _Row:
    qty: int
    side: Side
    note: str


# --------------------------------------------------------------------------- #
# Encoding                                                                     #
# --------------------------------------------------------------------------- #


def test_dumps_is_compact_and_keeps_unicode_unescaped():
    assert jsonfast.dumps({"name": "台灣 café 😀", "a": [1, 2]}) == '{"name":"台灣 café 😀","a":[1,2]}'.encode()


def test_dumps_sorted_orders_keys_at_every_depth():
    obj = {"b": 1, "a": {"z": 1, "y": [{"d": 1, "c": 2}]}}

    assert jsonfast.dumps_sorted(obj) == b'{"a":{"y":[{"c":2,"d":1}],"z":1},"b":1}'


def test_dumps_sorted_is_independent_of_insertion_order():
    assert jsonfast.dumps_sorted({"z": 3, "a": 1, "m": 2}) == jsonfast.dumps_sorted({"a": 1, "m": 2, "z": 3})


def test_dumps_sorted_orders_unicode_keys_by_code_point():
    obj = {"é": 1, "z": 2, "Z": 3, "10": 4, "9": 5, "台": 6, "😀": 7}

    assert jsonfast.dumps_sorted(obj) == '{"10":4,"9":5,"Z":3,"z":2,"é":1,"台":6,"😀":7}'.encode()


def test_dumps_keeps_insertion_order_when_not_sorted():
    assert jsonfast.dumps({"b": 1, "a": 2}) == b'{"b":1,"a":2}'


@pytest.mark.parametrize(
    "value",
    [0, 1, -1, 331235000, 10**12, 2**53, _BIG, -(2**53) - 1, 2**63 - 1, -(2**63), 2**64 - 1],
)
def test_dumps_writes_scaled_and_large_ints_exactly(value):
    encoded = jsonfast.dumps(value)

    assert encoded == str(value).encode()
    assert jsonfast.loads(encoded) == value


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_dumps_writes_non_finite_floats_as_null(value):
    assert jsonfast.dumps({"x": value}) == b'{"x":null}'


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.1, b"0.1"), (1.5e-7, b"1.5e-7"), (123456789.12345679, b"123456789.12345679"), (1.0, b"1.0"), (-0.0, b"-0.0")],
)
def test_dumps_writes_shortest_round_trip_floats_below_1e16(value, expected):
    assert jsonfast.dumps(value) == expected
    assert jsonfast.loads(expected) == value


def test_dumps_writes_enum_intenum_dataclass_and_tuple_by_value():
    assert jsonfast.dumps(Side.SELL) == b"1"
    assert jsonfast.dumps(_Color.RED) == b'"red"'
    assert jsonfast.dumps(_Row(qty=2, side=Side.BUY, note="x")) == b'{"qty":2,"side":0,"note":"x"}'
    assert jsonfast.dumps((1, 2)) == b"[1,2]"


def test_dumps_accepts_str_and_int_subclasses_as_their_plain_value():
    assert jsonfast.dumps({"s": _StrSub("x"), "i": _IntSub(3)}) == b'{"s":"x","i":3}'


def test_dumps_accepts_numpy_str_scalar_as_text():
    np = pytest.importorskip("numpy")

    assert jsonfast.dumps(np.str_("npx")) == b'"npx"'


@pytest.mark.parametrize(
    "factory",
    [
        lambda np: np.float64(1.5),
        lambda np: np.float32(1.5),
        lambda np: np.int64(5),
        lambda np: np.bool_(True),
        lambda np: np.arange(3),
        lambda np: object(),
        lambda np: 1j,
        lambda np: _SdkMapping({"a": 1}),
    ],
)
def test_dumps_rejects_unserializable_types_with_type_error(factory):
    np = pytest.importorskip("numpy")

    with pytest.raises(TypeError):
        jsonfast.dumps({"payload": factory(np)})


def test_dumps_rejects_unserializable_value_in_sorted_mode_with_type_error():
    with pytest.raises(TypeError):
        jsonfast.dumps_sorted({"a": object()})


def test_dumps_rejects_lone_surrogate_with_type_error():
    with pytest.raises(TypeError):
        jsonfast.dumps("a" + chr(0xD800))


def test_dumps_rejects_cyclic_structure_with_type_error():
    cyclic: list = []
    cyclic.append(cyclic)

    with pytest.raises(TypeError):
        jsonfast.dumps(cyclic)


def test_dumps_str_returns_decoded_text():
    assert jsonfast.dumps_str({"name": "台灣", "n": 1}) == '{"name":"台灣","n":1}'


# --------------------------------------------------------------------------- #
# Decoding                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "data",
    [b'{"a":1}', bytearray(b'{"a":1}'), memoryview(b'{"a":1}'), '{"a":1}', b'{"a":1}\n', b'  {"a" : 1}  '],
)
def test_loads_accepts_bytes_like_and_str_input(data):
    assert jsonfast.loads(data) == {"a": 1}


@pytest.mark.parametrize("text", ["18446744073709551616", "-9223372036854775809", "123456789012345678901234567890"])
def test_loads_decodes_ints_beyond_64_bits_exactly(text):
    decoded = jsonfast.loads(text)

    assert isinstance(decoded, int)
    assert decoded == int(text)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"   ",
        b"{",
        b'{"a":1,}',
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"1e400",
        b'"\xff"',
        b'{"a":1} x',
        b"[" * 100_000,
    ],
)
def test_loads_raises_value_error_on_malformed_input(data):
    with pytest.raises(ValueError):
        jsonfast.loads(data)


def test_loads_raises_decode_error_for_nesting_deeper_than_the_interpreter():
    with pytest.raises(jsonfast.DecodeError):
        jsonfast.loads(b"[" * 100_000)


def test_loads_round_trips_a_wal_row_through_dumps():
    assert jsonfast.loads(jsonfast.dumps(_WAL_ROW)) == _WAL_ROW


# --------------------------------------------------------------------------- #
# Fail closed, and orjson stays out of the runtime                             #
# --------------------------------------------------------------------------- #


def test_import_fails_closed_when_msgspec_is_missing():
    spec = importlib.util.spec_from_file_location("jsonfast_probe", jsonfast.__file__)
    module = importlib.util.module_from_spec(spec)

    with patch.dict(sys.modules, {"msgspec": None}):
        with pytest.raises(ImportError):
            spec.loader.exec_module(module)


def _runtime_root() -> Path:
    return Path(jsonfast.__file__).resolve().parents[1]


def _imported_module_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return []


def test_runtime_sources_never_import_orjson():
    offenders = []
    for path in sorted(_runtime_root().rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if any(name.split(".")[0] == "orjson" for name in _imported_module_names(node)):
                offenders.append(f"{path.relative_to(_runtime_root())}:{node.lineno}")

    assert offenders == []


@pytest.mark.parametrize(
    "relpath",
    ["execution/router.py", "execution/fill_dlq.py", "order/adapter.py", "execution/checkpoint.py"],
)
def test_persistence_sites_have_no_function_local_codec_import(relpath):
    """A codec imported inside a function sits inside that function's ``except Exception``,
    so a missing serializer would be logged and skipped instead of refusing startup."""
    tree = ast.parse((_runtime_root() / relpath).read_text(encoding="utf-8"))
    codecs = {"orjson", "json", "msgspec"}
    local_imports = []
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                if any(name.split(".")[0] in codecs for name in _imported_module_names(node)):
                    local_imports.append(f"{func.name}:{node.lineno}")

    assert local_imports == []


@pytest.mark.parametrize("relpath", ["execution/router.py", "execution/fill_dlq.py", "order/adapter.py"])
def test_persistence_sites_import_the_codec_at_module_scope(relpath):
    tree = ast.parse((_runtime_root() / relpath).read_text(encoding="utf-8"))
    top_level_imports = {
        (node.module, alias.name) for node in tree.body if isinstance(node, ast.ImportFrom) for alias in node.names
    }

    assert ("hft_platform.utils", "jsonfast") in top_level_imports


def test_runtime_modules_import_when_orjson_is_unavailable():
    """The free-threaded build has no orjson wheel: every runtime module must import without it.

    Third-party packages may still try ``import orjson`` and fall back (clickhouse_connect
    does); blocking the name in ``sys.modules`` reproduces that environment.
    """
    code = (
        "import sys\n"
        "sys.modules['orjson'] = None\n"
        "import hft_platform.execution.router\n"
        "import hft_platform.execution.checkpoint\n"
        "import hft_platform.execution.fill_dlq\n"
        "import hft_platform.order.adapter\n"
        "import hft_platform.gateway.dedup\n"
        "import hft_platform.recorder.wal\n"
        "import hft_platform.recorder.batcher\n"
        "import hft_platform.recorder.worker\n"
        "import hft_platform.recorder._loader_ch\n"
        "import hft_platform.monitor._redis_publish\n"
        "import hft_platform.monitor._redis_poller\n"
        "import hft_platform.observability.health\n"
        "import hft_platform.services.system\n"
        "print('imported')\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HFT_", "SHIOAJI_"))}
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)

    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=25)

    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "imported"


# --------------------------------------------------------------------------- #
# Files written by orjson load through the real readers                        #
# --------------------------------------------------------------------------- #


def _make_checkpoint_store() -> MagicMock:
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
        "acc:r47:台指期": _pos("台指期", 1, -1, _BIG, 0),
    }
    store = MagicMock()
    store._peak_equity_scaled = 9223372036854775807
    store._total_realized_pnl_scaled = -_BIG
    store.snapshot_positions_with_recovery.return_value = (positions, {})
    return store


def test_load_checkpoint_accepts_orjson_written_file_with_valid_sha(tmp_path):
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    path = tmp_path / "ckpt.json"
    path.write_bytes(_LEGACY_CHECKPOINT)

    loaded = PositionCheckpointWriter.load_checkpoint(str(path))

    assert loaded is not None
    assert loaded["sha256"] == _LEGACY_CHECKPOINT_SHA
    assert loaded["total_realized_pnl_scaled"] == -_BIG
    assert loaded["peak_equity_scaled"] == 9223372036854775807
    assert loaded["positions"]["acc:r47:TMFK6"]["net_qty"] == -3
    assert loaded["positions"]["acc:r47:台指期"]["realized_pnl_scaled"] == _BIG
    assert loaded["positions"]["acc:r47:台指期"]["unknown_basis"] is True


def test_load_checkpoint_rejects_orjson_written_file_after_tampering(tmp_path):
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    path = tmp_path / "ckpt.json"
    path.write_bytes(_LEGACY_CHECKPOINT.replace(b"331235000", b"331235001"))

    assert PositionCheckpointWriter.load_checkpoint(str(path)) is None


def test_write_checkpoint_reproduces_orjson_bytes_exactly(tmp_path):
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    path = tmp_path / "ckpt.json"
    writer = PositionCheckpointWriter(
        _make_checkpoint_store(),
        path=str(path),
        interval_s=60,
        trading_date_provider=lambda: "2026-11-23",
    )
    writer.identity = {"broker": "shioaji", "account": "0123456789abcdef", "venue": "paper"}

    with patch("hft_platform.execution.checkpoint.now_ns", return_value=1763884800123456789):
        writer.write_checkpoint()

    assert path.read_bytes() == _LEGACY_CHECKPOINT


def test_wal_writer_reproduces_orjson_row_line_exactly(tmp_path):
    from hft_platform.recorder.wal import WALWriter

    target = tmp_path / "market_data_1_0.jsonl"
    WALWriter(str(tmp_path))._write_sync_atomic(str(target), [_WAL_ROW])

    assert target.read_bytes() == _LEGACY_WAL_ROW + b"\n"


def test_wal_loader_codec_decodes_orjson_written_row_and_header():
    from hft_platform.recorder import _loader_common

    assert _loader_common._loads(_LEGACY_WAL_ROW.decode("utf-8")) == _WAL_ROW
    assert _loader_common._loads(_LEGACY_WAL_HEADER) == {"__wal_table__": "hft.market_data", "__row_count__": 3}
    assert jsonfast.dumps({"__wal_table__": "hft.market_data", "__row_count__": 3}) == _LEGACY_WAL_HEADER


def test_wal_content_hash_equals_orjson_era_hash():
    from hft_platform.recorder._loader_ch import insert_with_dedup

    captured: list[str] = []
    svc = MagicMock()
    svc._dedup_enabled = True
    svc.ch_client = MagicMock()
    svc._is_duplicate.return_value = False
    svc.insert_batch.return_value = True
    svc._record_dedup.side_effect = lambda table, content_hash, row_count: captured.append(content_hash)

    insert_with_dedup(svc, "hft.market_data", _HASH_ROWS, "f.jsonl")

    assert captured == [_LEGACY_HASH_ROWS_SHA]
    assert hashlib.sha256(b"".join(jsonfast.dumps_sorted(r) for r in _HASH_ROWS)).hexdigest() == _LEGACY_HASH_ROWS_SHA


def test_fill_dlq_load_restores_fill_from_orjson_written_line(tmp_path):
    from hft_platform.execution.fill_dlq import OrphanedFillDLQ

    path = tmp_path / "fill_dlq.jsonl"
    path.write_bytes(_LEGACY_DLQ_LINE + b"\n")
    dlq = OrphanedFillDLQ(persist_path=str(path))

    dlq.load()

    assert dlq.drain() == [_DLQ_FILL]


def test_fill_dlq_persist_reproduces_orjson_line_exactly(tmp_path):
    from hft_platform.execution.fill_dlq import OrphanedFillDLQ

    path = tmp_path / "fill_dlq.jsonl"
    dlq = OrphanedFillDLQ(persist_path=str(path))
    dlq.add(_DLQ_FILL)

    dlq.persist()

    assert path.read_bytes() == _LEGACY_DLQ_LINE + b"\n"


def test_idempotency_store_loads_orjson_written_records(tmp_path):
    from hft_platform.gateway.dedup import IdempotencyStore

    path = tmp_path / "dedup.jsonl"
    path.write_bytes(_LEGACY_DEDUP_COMMITTED + b"\n" + _LEGACY_DEDUP_RESERVED + b"\n")
    store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=str(path))

    store.load()

    committed = store.check_or_reserve("k-1")
    reserved = store.check_or_reserve("k-2")
    assert committed is not None
    assert (committed.approved, committed.reason_code, committed.cmd_id) == (True, "OK", 42)
    assert reserved is not None
    assert (reserved.approved, reserved.reason_code, reserved.cmd_id) == (None, "", 0)


def test_idempotency_store_persist_reproduces_orjson_lines_exactly(tmp_path):
    from hft_platform.gateway.dedup import IdempotencyStore

    path = tmp_path / "dedup.jsonl"
    store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=str(path))
    store.check_or_reserve("k-1")
    store.commit("k-1", approved=True, reason_code="OK", cmd_id=42)
    store.check_or_reserve("k-2")

    store.persist()

    assert path.read_bytes() == _LEGACY_DEDUP_COMMITTED + b"\n" + _LEGACY_DEDUP_RESERVED + b"\n"


def _make_router(persist_path: str):
    from hft_platform.execution.router import ExecutionRouter

    with patch.dict(os.environ, {"HFT_FILL_DEDUP_PERSIST_PATH": persist_path}):
        return ExecutionRouter(
            bus=MagicMock(),
            raw_queue=asyncio.Queue(),
            order_id_map={},
            position_store=MagicMock(),
            terminal_handler=MagicMock(),
        )


def test_fill_dedup_load_restores_keys_from_orjson_written_file(tmp_path):
    path = tmp_path / "fill_dedup.jsonl"
    path.write_bytes(_LEGACY_FILL_DEDUP_KEY + b"\n" + b'"F-2"\n')

    router = _make_router(str(path))

    assert router._legacy_fill_ids == {"F-1:O-1:台", "F-2"}


def test_fill_dedup_persist_reproduces_orjson_lines_exactly(tmp_path):
    path = tmp_path / "fill_dedup.jsonl"
    router = _make_router(str(path))
    router._seen_fill_ids["F-1:O-1:台"] = None
    router._seen_fill_ids["F-2"] = None

    router.persist_fill_dedup()

    assert path.read_bytes() == _LEGACY_FILL_DEDUP_KEY + b"\n" + b'"F-2"\n'


def test_order_id_map_line_matches_orjson_bytes_and_decodes():
    entry = {"k": "TOKEN1", "v": "r47:100", "t_ns": 1763884800000000000, "s": "live"}

    assert jsonfast.dumps(entry) == _LEGACY_ORDER_ID_MAP_LINE
    assert jsonfast.loads(_LEGACY_ORDER_ID_MAP_LINE) == entry


def test_decode_error_is_the_codecs_own_type():
    assert issubclass(jsonfast.DecodeError, msgspec.DecodeError)
