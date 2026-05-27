"""Integration tests for ``hft run --mode replay`` (loop_v1 L4).

Covers the eligibility branches per plan section L4:

* ``eligible``     -- live intents in CK match replayed intents -> match_pct=100.
* ``eligible+divergence`` -- live and replayed differ -> match_pct<100.
* ``pre_recorder`` -- CK returns 0 rows -> exit 1 without --allow-pre-recorder.
* ``pre_recorder + override`` -- exit 0 with empty live stream report.
* ``no_fixture``   -- fixture path missing -> exit 1, no parity output.

The harness invokes ``run_replay_session`` directly (avoiding argparse)
since CLI parser surface is covered by ``test_cli_smoke``. We mock
``ck_client`` to keep ClickHouse out of the test path, and use a minimal
echo strategy whose ``handle_event`` emits one OrderIntent-shaped object
per ``BidAskEvent`` so canonical projection is well-defined.
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
    """Minimal canonical-shape stand-in for OrderIntent.

    Mirrors the keys consumed by ``replay.intent_log._intent_to_canonical``;
    enum-typed fields (intent_type/side/tif) expose ``.name`` like the real
    ``OrderIntent`` so canonical projection works without importing the
    full strategy contract.
    """

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
    """One ``OrderIntent`` per ``BidAskEvent`` -- price = best_bid."""

    def __init__(self, *, rng: Any | None = None, strategy_id: str = "ECHO") -> None:
        self.strategy_id = strategy_id
        self._counter = 0

    def handle_event(self, ctx: Any, event: Any) -> list[_EchoIntent]:
        bids = getattr(event, "bids", None)
        if bids is None or len(bids) == 0:
            return []
        self._counter += 1
        price = int(bids[0, 0])
        return [
            _EchoIntent(
                intent_id=self._counter,
                symbol=str(getattr(event, "symbol", "")),
                price=price,
                qty=1,
                timestamp_ns=int(getattr(event.meta, "source_ts", 0)),
            )
        ]


def _factory(*, rng: Any | None = None) -> _EchoStrategy:
    return _EchoStrategy(rng=rng)


def _market_rows(prices: list[int]) -> list[dict]:
    """Build BidAsk WAL rows; one event per price level."""
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
    """Produce canonical-form live intents matching what _EchoStrategy would emit."""
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
    """Mocks the two queries L4 issues against ``hft.order_intents``.

    1) ``SELECT count() ...`` -- eligibility check.
    2) ``SELECT intent_id, ... ORDER BY timestamp_ns`` -- canonical loader.
    """

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
                    int(d["price"]),  # price_scaled in CK
                    d["qty"],
                    d["tif"],
                    d["target_order_id"],
                    int(d["timestamp_us"]) * 1000,  # timestamp_ns
                    int(d.get("source_ts_ns", 0)),  # source_ts_ns
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
            "module": "tests.integration.test_cli_run_replay",
            "class": "_EchoStrategy",
        },
    }


def test_replay_eligible_full_match(tmp_path: Path, _settings: dict) -> None:
    from hft_platform.replay.cli_runner import run_replay_session

    prices = [1000000, 1000100, 1000200]
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


def test_replay_eligible_with_divergence(tmp_path: Path, _settings: dict) -> None:
    from hft_platform.replay.cli_runner import run_replay_session

    prices = [1000000, 1000100, 1000200, 1000300]
    fixture = _build_fixture(tmp_path / "wal.tar.gz", _market_rows(prices))
    out_root = tmp_path / "out"
    # Live intents differ from replayed by +50 on every price -> 0% match.
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

    # Strict fail-closed: an eligible session that diverges exits non-zero.
    assert rc == 1
    report = json.loads((out_root / "2026-04-21" / "report.json").read_text())
    assert report["eligibility_status"] == "eligible"
    assert report["ok"] is False
    assert report["match_pct"] < 95.0
    assert report["first_divergence_idx"] == 0
    assert report["mismatch_type"]
    histogram = json.loads((out_root / "2026-04-21" / "divergence_histogram.json").read_text())
    assert histogram.get("price", 0) == 4
    assert histogram.get("decision_price", 0) == 4


def test_replay_pre_recorder_session_blocks_without_override(tmp_path: Path, _settings: dict) -> None:
    from hft_platform.replay.cli_runner import run_replay_session

    fixture = _build_fixture(tmp_path / "wal.tar.gz", _market_rows([1000000]))
    out_root = tmp_path / "out"
    fake_ck = _FakeCKClient(intents=[])  # CK returns 0 rows

    rc = run_replay_session(
        _settings,
        session_date=date(2026, 4, 21),
        fixture_path=fixture,
        allow_pre_recorder=False,
        out_root=out_root,
        ck_client=fake_ck,
        strategy_factory_override=_factory,
    )

    assert rc == 1
    report = json.loads((out_root / "2026-04-21" / "report.json").read_text())
    assert report["eligibility_status"] == "pre_recorder"
    assert "no_intents_recorded_for_2026-04-21" in report["error"]
    # No parity report produced when ineligible+blocked.
    assert not (out_root / "2026-04-21" / "divergence_histogram.json").exists()


def test_replay_pre_recorder_with_override_runs_with_empty_live(tmp_path: Path, _settings: dict) -> None:
    from hft_platform.replay.cli_runner import run_replay_session

    prices = [1000000, 1000100]
    fixture = _build_fixture(tmp_path / "wal.tar.gz", _market_rows(prices))
    out_root = tmp_path / "out"
    fake_ck = _FakeCKClient(intents=[])

    rc = run_replay_session(
        _settings,
        session_date=date(2026, 4, 21),
        fixture_path=fixture,
        allow_pre_recorder=True,
        out_root=out_root,
        ck_client=fake_ck,
        strategy_factory_override=_factory,
    )

    assert rc == 0
    report = json.loads((out_root / "2026-04-21" / "report.json").read_text())
    assert report["eligibility_status"] == "pre_recorder"
    assert report["n_live_intents"] == 0
    assert report["n_replayed_intents"] == 2
    # Length mismatch -> all replayed entries bucketed under __missing__.
    histogram = json.loads((out_root / "2026-04-21" / "divergence_histogram.json").read_text())
    assert histogram.get("__missing__", 0) == 2


def test_replay_no_fixture_exits_1(tmp_path: Path, _settings: dict) -> None:
    from hft_platform.replay.cli_runner import run_replay_session

    out_root = tmp_path / "out"
    fake_ck = _FakeCKClient(intents=[])

    rc = run_replay_session(
        _settings,
        session_date=date(2026, 4, 21),
        fixture_path=tmp_path / "does_not_exist.tar.gz",
        allow_pre_recorder=False,
        out_root=out_root,
        ck_client=fake_ck,
        strategy_factory_override=_factory,
    )

    assert rc == 1
    report = json.loads((out_root / "2026-04-21" / "report.json").read_text())
    assert report["eligibility_status"] == "no_fixture"
    assert "fixture_not_found" in report["error"]
