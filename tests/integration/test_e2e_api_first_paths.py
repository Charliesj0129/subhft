from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus, PositionDelta, Side
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.order.adapter import OrderAdapter
from hft_platform.recorder.loader import WALLoaderService
from hft_platform.recorder.writer import DataWriter
from hft_platform.risk.engine import RiskEngine
from hft_platform.services.execution import ExecutionService

ROOT = Path(__file__).resolve().parents[2]


class InMemoryBrokerAPI:
    """API-compatible in-memory broker for integration tests (no monkeypatch/magic mock)."""

    def __init__(self) -> None:
        self._order_seq = 0
        self.placed_orders: list[dict[str, Any]] = []
        self.last_trade: dict[str, Any] | None = None

    def get_exchange(self, symbol: str) -> str:
        del symbol
        return "TSE"

    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        self._order_seq += 1
        ord_no = f"O{self._order_seq}"
        seq_no = f"S{self._order_seq}"
        self.placed_orders.append(dict(kwargs))
        self.last_trade = {
            "ord_no": ord_no,
            "seq_no": seq_no,
            "order": {
                "ord_no": ord_no,
                "seq_no": seq_no,
            },
        }
        return dict(self.last_trade)

    def cancel_order(self, trade: dict[str, Any]) -> dict[str, Any]:
        return {
            "ord_no": str(trade.get("ord_no", "")),
            "status": "Cancelled",
        }

    def update_order(self, trade: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {
            "ord_no": str(trade.get("ord_no", "")),
            "status": "Updated",
            **kwargs,
        }


async def _wait_for(predicate, timeout: float = 2.0, step: float = 0.01) -> None:
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError("Timed out waiting for condition")


async def _collect_events(bus: RingBufferBus, count: int, timeout: float = 2.0) -> list[Any]:
    events: list[Any] = []

    async def _consume() -> None:
        async for event in bus.consume(start_cursor=-1):
            events.append(event)
            if len(events) >= count:
                break

    await asyncio.wait_for(_consume(), timeout=timeout)
    return events


def _wait_for_clickhouse(client_mod: Any, host: str, port: int, timeout_s: float = 20.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            client = client_mod.get_client(host=host, port=port)
            client.command("SELECT 1")
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    pythonpath_entries = [str(ROOT), str(ROOT / "src")]
    existing = env.get("PYTHONPATH")
    if existing:
        pythonpath_entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    return env


def _build_research_input(path: Path, rows: int = 96) -> None:
    dtype = np.dtype(
        [
            ("best_bid", np.float64),
            ("best_ask", np.float64),
            ("bid_depth", np.float64),
            ("ask_depth", np.float64),
            ("qty", np.float64),
            ("mid", np.float64),
        ]
    )
    data = np.zeros(rows, dtype=dtype)
    base = 100.0

    for i in range(rows):
        bid = base + i * 0.02
        ask = bid + 0.04
        data["best_bid"][i] = bid
        data["best_ask"][i] = ask
        data["bid_depth"][i] = 100 + (i % 11)
        data["ask_depth"][i] = 90 + (i % 7)
        data["qty"][i] = 1 + (i % 3)
        data["mid"][i] = (bid + ask) / 2.0

    np.savez(path, data=data)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hotpath_intent_to_execution_api_first(tmp_path, monkeypatch):
    symbols_cfg = tmp_path / "symbols.yaml"
    symbols_cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(symbols_cfg))
    monkeypatch.setenv("HFT_API_COALESCE_WINDOW_S", "0.001")

    risk_cfg = tmp_path / "risk.yaml"
    risk_cfg.write_text(
        "\n".join(
            [
                "global_defaults:",
                "  max_notional: 1000000",
                "  max_price_cap: 100000",
                "storm_guard:",
                "  warm_threshold: -1000000",
                "  storm_threshold: -2000000",
                "  halt_threshold: -3000000",
            ]
        )
        + "\n"
    )

    adapter_cfg = tmp_path / "order_adapter.yaml"
    adapter_cfg.write_text(
        "\n".join(
            [
                "rate_limits:",
                "  shioaji_soft_cap: 1000",
                "  shioaji_hard_cap: 2000",
                "  window_seconds: 10",
            ]
        )
        + "\n"
    )

    bus = RingBufferBus()
    intent_q: asyncio.Queue[OrderIntent] = asyncio.Queue()
    order_q: asyncio.Queue[Any] = asyncio.Queue()
    raw_exec_q: asyncio.Queue[RawExecEvent] = asyncio.Queue()

    order_id_map: dict[str, str] = {}
    broker_api = InMemoryBrokerAPI()
    order_adapter = OrderAdapter(str(adapter_cfg), order_q, broker_api, order_id_map)
    risk_engine = RiskEngine(str(risk_cfg), intent_q, order_q)
    position_store = PositionStore()
    exec_service = ExecutionService(bus, raw_exec_q, order_id_map, position_store, order_adapter)

    tasks = [
        asyncio.create_task(risk_engine.run()),
        asyncio.create_task(order_adapter.run()),
        asyncio.create_task(exec_service.run()),
    ]

    try:
        intent = OrderIntent(
            intent_id=1,
            strategy_id="strat",
            symbol="AAA",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=10000,
            qty=2,
            tif=TIF.LIMIT,
            timestamp_ns=time.time_ns(),
        )
        await intent_q.put(intent)
        await asyncio.wait_for(intent_q.join(), timeout=2.0)

        await _wait_for(lambda: len(broker_api.placed_orders) == 1, timeout=2.0)
        assert broker_api.placed_orders[0]["contract_code"] == "AAA"
        assert broker_api.placed_orders[0]["qty"] == 2
        assert broker_api.last_trade is not None

        ord_no = str(broker_api.last_trade["ord_no"])
        assert order_id_map.get(ord_no) == "strat:1"

        raw_order = RawExecEvent(
            "order",
            {
                "ord_no": ord_no,
                "status": {"status": "Filled"},
                "contract": {"code": "AAA"},
                "order": {"action": "Buy", "price": 1.0, "quantity": 2},
            },
            time.time_ns(),
        )
        raw_fill = RawExecEvent(
            "deal",
            {
                "seq_no": "F1",
                "ord_no": ord_no,
                "code": "AAA",
                "action": "Buy",
                "quantity": 2,
                "price": 1.0,
                "ts": time.time_ns(),
            },
            time.time_ns(),
        )
        await raw_exec_q.put(raw_order)
        await raw_exec_q.put(raw_fill)
        await asyncio.wait_for(raw_exec_q.join(), timeout=2.0)

        events = await _collect_events(bus, count=3, timeout=2.0)
        order_events = [evt for evt in events if isinstance(evt, OrderEvent)]
        fill_events = [evt for evt in events if isinstance(evt, FillEvent)]
        deltas = [evt for evt in events if isinstance(evt, PositionDelta)]

        assert order_events and fill_events and deltas
        assert order_events[0].status == OrderStatus.FILLED
        assert fill_events[0].strategy_id == "strat"
        assert deltas[0].net_qty == 2

        await _wait_for(lambda: not order_adapter.live_orders, timeout=2.0)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coldpath_wal_replay_to_clickhouse_api_first(tmp_path, monkeypatch):
    clickhouse_connect = pytest.importorskip("clickhouse_connect")

    host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))
    if not _wait_for_clickhouse(clickhouse_connect, host, port):
        pytest.skip("ClickHouse not reachable")

    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "0")
    monkeypatch.setenv("HFT_DISABLE_CLICKHOUSE", "1")
    monkeypatch.setenv("HFT_WAL_BATCH_ENABLED", "0")

    wal_dir = tmp_path / "wal"
    archive_dir = wal_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    writer = DataWriter(ch_host=host, ch_port=port, wal_dir=str(wal_dir))
    ingest_ts = time.time_ns()
    symbol = f"E2E_COLD_{ingest_ts}"

    rows = [
        {
            "symbol": symbol,
            "exchange": "TEST",
            "type": "Tick",
            "exch_ts": ingest_ts,
            "ingest_ts": ingest_ts,
            "price_scaled": 1234500,
            "volume": 10,
            "bids_price": [1234400],
            "bids_vol": [5],
            "asks_price": [1234600],
            "asks_vol": [5],
            "seq_no": 1,
        }
    ]

    await writer.write("market_data", rows)
    await writer.shutdown()

    wal_files = list(wal_dir.glob("market_data_*.jsonl"))
    assert wal_files, "Expected WAL fallback file"

    loader = WALLoaderService(
        wal_dir=str(wal_dir),
        archive_dir=str(archive_dir),
        ch_host=host,
        ch_port=port,
    )
    loader.connect()
    if loader.ch_client is None:
        pytest.skip("WAL loader cannot connect to ClickHouse")
    loader.process_files(force=True)

    client = clickhouse_connect.get_client(host=host, port=port)
    result = client.query(
        "SELECT count() FROM hft.market_data WHERE symbol=%(symbol)s AND ingest_ts=%(ingest_ts)s",
        parameters={"symbol": symbol, "ingest_ts": ingest_ts},
    )
    assert result.result_rows[0][0] >= 1
    assert list(archive_dir.glob("market_data_*.jsonl")), "WAL file should be archived after replay"

    client.command(f"ALTER TABLE hft.market_data DELETE WHERE symbol = '{symbol}'")


@pytest.mark.integration
def test_research_path_hbt_runner_cli_api_first(tmp_path):
    if not (ROOT / "research" / "alphas" / "ofi_mc" / "impl.py").exists():
        pytest.skip("research/alphas/ofi_mc not available")

    data_path = tmp_path / "ofi_mc_input.npz"
    out_path = tmp_path / "hbt_summary.json"
    _build_research_input(data_path)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "research.backtest.hbt_runner",
            "--alpha",
            "ofi_mc",
            "--data",
            str(data_path),
            "--out",
            str(out_path),
        ],
        cwd=str(ROOT),
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert out_path.exists(), "Expected backtest summary output"

    payload = json.loads(out_path.read_text())
    assert payload["alpha_id"] == "ofi_mc"
    assert isinstance(payload["config_hash"], str) and payload["config_hash"]
    assert "sharpe_is" in payload
    assert "sharpe_oos" in payload
