"""Unit tests for ``hft_platform.replay.cli_runner.run_replay_session`` (loop_v1 L4).

Mirrors the integration test in ``tests/integration/test_cli_run_replay.py``
but lives under ``tests/unit/`` so the per-package coverage floor for the
``replay`` domain is satisfied (CI runs ``coverage-domain`` only against
``tests/unit``).

All branches are exercised hermetically:
  * ``eligible``     -- match_pct == 100 with FakeCK matching live intents.
  * ``divergence``   -- replayed prices offset from live -> match_pct < 95.
  * ``pre_recorder`` -- CK count==0 and no override -> exit 1.
  * ``pre_recorder + override`` -- empty live stream + replayed -> exit 0.
  * ``no_fixture``   -- missing fixture -> exit 1.
  * ``strategy_unbuildable`` -- factory builder returns None -> exit 1.
  * ``_build_strategy_factory`` happy + bad-import paths.
  * ``_file_sha256`` covers the chunked-read helper.
  * ``_row_to_canonical`` matches integration assumptions.
"""

from __future__ import annotations

import io
import json
import tarfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _build_fixture(path: Path, rows: list[dict]) -> Path:
    header = {"__wal_table__": "hft.market_data"}
    payload_lines = [json.dumps(header)] + [json.dumps(r) for r in rows]
    payload = ("\n".join(payload_lines) + "\n").encode("utf-8")
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(name="shard.jsonl")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return path


class _EchoIntent:
    def __init__(
        self,
        *,
        intent_id: int,
        symbol: str,
        price: int,
        qty: int,
        timestamp_ns: int,
    ) -> None:
        self.intent_id = intent_id
        self.strategy_id = "ECHO"
        self.symbol = symbol
        self.intent_type = SimpleNamespace(name="NEW")
        self.side = SimpleNamespace(name="BUY")
        self.tif = SimpleNamespace(name="LIMIT")
        self.price = price
        self.qty = qty
        self.target_order_id = ""
        self.timestamp_ns = timestamp_ns
        self.decision_price = price
        self.price_type = "LMT"


class _EchoStrategy:
    """Emit one ``_EchoIntent`` per BidAsk event using best-bid as price."""

    def __init__(self, *, rng: Any | None = None, strategy_id: str = "ECHO") -> None:
        self.strategy_id = strategy_id
        self._counter = 0

    def handle_event(self, ctx: Any, event: Any) -> list[_EchoIntent]:
        bids = getattr(event, "bids", None)
        if bids is None or len(bids) == 0:
            return []
        self._counter += 1
        return [
            _EchoIntent(
                intent_id=self._counter,
                symbol=str(getattr(event, "symbol", "")),
                price=int(bids[0, 0]),
                qty=1,
                timestamp_ns=int(getattr(event.meta, "source_ts", 0)),
            )
        ]


def _factory(*, rng: Any | None = None) -> _EchoStrategy:
    return _EchoStrategy(rng=rng)


def _market_rows(prices: list[int]) -> list[dict]:
    return [
        {
            "symbol": "TMFD6",
            "exchange": "TAIFEX",
            "type": "BidAsk",
            "exch_ts": (i + 1) * 1_000_000_000,
            "ingest_ts": (i + 1) * 1_000_000_000,
            "seq_no": i + 1,
            "bids_price": [p],
            "bids_vol": [10],
            "asks_price": [p + 100],
            "asks_vol": [10],
        }
        for i, p in enumerate(prices)
    ]


def _live_intents_canonical(prices: list[int], offset: int = 0) -> list[dict]:
    return [
        {
            "intent_id": i + 1,
            "strategy_id": "ECHO",
            "symbol": "TMFD6",
            "intent_type": "NEW",
            "side": "BUY",
            "tif": "LIMIT",
            "price": p + offset,
            "qty": 1,
            "target_order_id": "",
            "timestamp_us": (i + 1) * 1_000_000,
            "decision_price": p + offset,
            "price_type": "LMT",
        }
        for i, p in enumerate(prices)
    ]


class _FakeCKResult:
    def __init__(self, rows: list[tuple]) -> None:
        self.result_rows = rows


class _FakeCKClient:
    """Mocks the eligibility count() and the canonical-loader SELECT."""

    def __init__(self, intents: list[dict]) -> None:
        self._intents = intents

    def query(self, sql: str, *, parameters: dict | None = None) -> _FakeCKResult:
        if "count()" in sql:
            return _FakeCKResult([(len(self._intents),)])
        rows = []
        for d in self._intents:
            rows.append(
                (
                    d["intent_id"],
                    d["strategy_id"],
                    d["symbol"],
                    d["intent_type"],
                    d["side"],
                    int(d["price"]),
                    d["qty"],
                    d["tif"],
                    d["target_order_id"],
                    int(d["timestamp_us"]) * 1000,
                    d["decision_price"],
                    d["price_type"],
                )
            )
        return _FakeCKResult(rows)


@pytest.fixture
def _settings() -> dict:
    return {
        "loop_id": "echo_v1",
        "strategy": {
            "id": "ECHO",
            "module": "tests.unit.replay.test_cli_runner",
            "class": "_EchoStrategy",
        },
    }


def test_replay_eligible_full_match(tmp_path: Path, _settings: dict) -> None:
    from hft_platform.replay.cli_runner import run_replay_session

    prices = [1_000_000, 1_000_100, 1_000_200]
    fixture = _build_fixture(tmp_path / "wal.tar.gz", _market_rows(prices))
    out_root = tmp_path / "out"
    fake_ck = _FakeCKClient(_live_intents_canonical(prices))

    rc = run_replay_session(
        _settings,
        session_date=date(2026, 4, 21),
        fixture_path=fixture,
        allow_pre_recorder=False,
        out_root=out_root,
        ck_client=fake_ck,
        strategy_factory_override=_factory,
    )

    assert rc == 0
    report = json.loads((out_root / "2026-04-21" / "report.json").read_text())
    assert report["eligibility_status"] == "eligible"
    assert report["n_live_intents"] == 3
    assert report["n_replayed_intents"] == 3
    assert report["match_pct"] == 100.0
    assert report["first_divergence_idx"] is None
    assert report["fixture_sha256"]
    assert (out_root / "2026-04-21" / "timeline.md").exists()


def test_replay_eligible_with_divergence(tmp_path: Path, _settings: dict) -> None:
    from hft_platform.replay.cli_runner import run_replay_session

    prices = [1_000_000, 1_000_100, 1_000_200, 1_000_300]
    fixture = _build_fixture(tmp_path / "wal.tar.gz", _market_rows(prices))
    out_root = tmp_path / "out"
    fake_ck = _FakeCKClient(_live_intents_canonical(prices, offset=50))

    rc = run_replay_session(
        _settings,
        session_date=date(2026, 4, 21),
        fixture_path=fixture,
        allow_pre_recorder=False,
        out_root=out_root,
        ck_client=fake_ck,
        strategy_factory_override=_factory,
    )

    assert rc == 0
    report = json.loads((out_root / "2026-04-21" / "report.json").read_text())
    assert report["match_pct"] < 95.0
    assert report["first_divergence_idx"] == 0
    histogram = json.loads((out_root / "2026-04-21" / "divergence_histogram.json").read_text())
    assert histogram.get("price", 0) == 4


def test_replay_pre_recorder_blocks_without_override(tmp_path: Path, _settings: dict) -> None:
    from hft_platform.replay.cli_runner import run_replay_session

    fixture = _build_fixture(tmp_path / "wal.tar.gz", _market_rows([1_000_000]))
    out_root = tmp_path / "out"

    rc = run_replay_session(
        _settings,
        session_date=date(2026, 4, 21),
        fixture_path=fixture,
        allow_pre_recorder=False,
        out_root=out_root,
        ck_client=_FakeCKClient(intents=[]),
        strategy_factory_override=_factory,
    )

    assert rc == 1
    report = json.loads((out_root / "2026-04-21" / "report.json").read_text())
    assert report["eligibility_status"] == "pre_recorder"
    assert "no_intents_recorded_for_2026-04-21" in report["error"]
    assert not (out_root / "2026-04-21" / "divergence_histogram.json").exists()


def test_replay_pre_recorder_with_override(tmp_path: Path, _settings: dict) -> None:
    from hft_platform.replay.cli_runner import run_replay_session

    prices = [1_000_000, 1_000_100]
    fixture = _build_fixture(tmp_path / "wal.tar.gz", _market_rows(prices))
    out_root = tmp_path / "out"

    rc = run_replay_session(
        _settings,
        session_date=date(2026, 4, 21),
        fixture_path=fixture,
        allow_pre_recorder=True,
        out_root=out_root,
        ck_client=_FakeCKClient(intents=[]),
        strategy_factory_override=_factory,
    )

    assert rc == 0
    report = json.loads((out_root / "2026-04-21" / "report.json").read_text())
    assert report["eligibility_status"] == "pre_recorder"
    assert report["n_live_intents"] == 0
    assert report["n_replayed_intents"] == 2
    histogram = json.loads((out_root / "2026-04-21" / "divergence_histogram.json").read_text())
    assert histogram.get("__missing__", 0) == 2


def test_replay_no_fixture_exits_1(tmp_path: Path, _settings: dict) -> None:
    from hft_platform.replay.cli_runner import run_replay_session

    out_root = tmp_path / "out"
    rc = run_replay_session(
        _settings,
        session_date=date(2026, 4, 21),
        fixture_path=tmp_path / "missing.tar.gz",
        allow_pre_recorder=False,
        out_root=out_root,
        ck_client=_FakeCKClient(intents=[]),
        strategy_factory_override=_factory,
    )
    assert rc == 1
    report = json.loads((out_root / "2026-04-21" / "report.json").read_text())
    assert report["eligibility_status"] == "no_fixture"
    assert "fixture_not_found" in report["error"]


def test_replay_strategy_unbuildable_returns_1(tmp_path: Path) -> None:
    from hft_platform.replay.cli_runner import run_replay_session

    fixture = _build_fixture(tmp_path / "wal.tar.gz", _market_rows([1_000_000]))
    out_root = tmp_path / "out"
    bad_settings = {
        "loop_id": "echo_v1",
        "strategy": {"id": "X", "module": "", "class": ""},
    }

    rc = run_replay_session(
        bad_settings,
        session_date=date(2026, 4, 21),
        fixture_path=fixture,
        allow_pre_recorder=True,
        out_root=out_root,
        ck_client=_FakeCKClient(intents=[]),
        strategy_factory_override=None,
    )
    assert rc == 1
    report = json.loads((out_root / "2026-04-21" / "report.json").read_text())
    assert "strategy_unbuildable" in report["error"]


def test_build_strategy_factory_returns_none_for_bad_import() -> None:
    from hft_platform.replay.cli_runner import _build_strategy_factory

    assert _build_strategy_factory({}) is None
    assert _build_strategy_factory({"strategy": {"id": "X"}}) is None
    assert (
        _build_strategy_factory(
            {
                "strategy": {
                    "id": "X",
                    "module": "no_such_module_xyz_123",
                    "class": "Bogus",
                }
            }
        )
        is None
    )


def test_build_strategy_factory_happy_path_returns_callable() -> None:
    from hft_platform.replay.cli_runner import _build_strategy_factory

    factory = _build_strategy_factory(
        {
            "strategy": {
                "id": "ECHO",
                "module": "tests.unit.replay.test_cli_runner",
                "class": "_EchoStrategy",
            }
        }
    )
    assert factory is not None
    instance = factory(rng=None)
    # importlib.import_module may resolve to a re-imported copy of the test
    # module under conftest collection, so compare by class qualname rather
    # than identity.
    assert type(instance).__name__ == "_EchoStrategy"


def test_file_sha256_helper_matches_known_digest(tmp_path: Path) -> None:
    import hashlib

    from hft_platform.replay.cli_runner import _file_sha256

    payload = b"hft-replay-cli-runner-test\n" * 1024
    fp = tmp_path / "blob.bin"
    fp.write_bytes(payload)

    assert _file_sha256(fp) == hashlib.sha256(payload).hexdigest()


def test_row_to_canonical_projects_clickhouse_row() -> None:
    from hft_platform.replay.cli_runner import _row_to_canonical

    columns = [
        "intent_id",
        "strategy_id",
        "symbol",
        "intent_type",
        "side",
        "price_scaled",
        "qty",
        "tif",
        "target_order_id",
        "timestamp_ns",
        "decision_price",
        "price_type",
    ]
    row = (
        7,
        "ECHO",
        "TMFD6",
        "NEW",
        "BUY",
        1_234_567,
        2,
        "LIMIT",
        "",
        5_000_000_000,
        1_234_567,
        "LMT",
    )

    canonical = _row_to_canonical(row, columns)
    assert canonical["intent_id"] == 7
    assert canonical["price"] == 1_234_567
    assert canonical["timestamp_us"] == 5_000_000
    assert canonical["price_type"] == "LMT"
