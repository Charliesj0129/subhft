"""Tests for G4: Cold-path fill gap recovery via ExecutionRouter.recover_fill_gaps().

Covers:
- DLQ orphaned fills resolved via checkpoint fallback when order_id_map is empty
- Dedup skip for fills already processed
- Empty DLQ short-circuits
- Unresolvable fills remain in DLQ
"""

import asyncio
import collections
import os
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.execution.fill_dlq import OrphanedFillDLQ, _dlq, get_orphaned_fill_dlq
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.router import ExecutionRouter


def _make_orphaned_fill(
    symbol: str = "TXFD6",
    order_id: str = "ORD1",
    fill_id: str = "FILL1",
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        account_id="ACC1",
        order_id=order_id,
        strategy_id="UNKNOWN",
        symbol=symbol,
        side=Side.BUY,
        qty=1,
        price=200000000,
        fee=0,
        tax=0,
        ingest_ts_ns=1000,
        match_ts_ns=2000,
    )


def _make_router(position_store: PositionStore | None = None) -> ExecutionRouter:
    bus = MagicMock()
    raw_queue = asyncio.Queue()
    order_id_map: dict[str, str] = {}
    store = position_store or PositionStore()
    terminal = MagicMock()
    router = ExecutionRouter(
        bus=bus,
        raw_queue=raw_queue,
        order_id_map=order_id_map,
        position_store=store,
        terminal_handler=terminal,
    )
    # Isolate from on-disk dedup state
    router._seen_fill_ids = collections.OrderedDict()
    return router


@pytest.fixture(autouse=True)
def _reset_dlq_singleton(tmp_path, monkeypatch):
    """Reset the DLQ singleton and use a temp path to avoid cross-test state."""
    import hft_platform.execution.fill_dlq as dlq_mod

    old = dlq_mod._dlq
    dlq_mod._dlq = None
    # Use temp path so tests don't load from or persist to the real .state/
    monkeypatch.setenv("HFT_FILL_DLQ_PERSIST_PATH", str(tmp_path / "test_dlq.jsonl"))
    yield
    dlq_mod._dlq = old


class TestRecoverFillGaps:
    @pytest.mark.asyncio
    async def test_empty_dlq_returns_zeros(self):
        router = _make_router()
        result = await router.recover_fill_gaps(checkpoint_path="/nonexistent")
        assert result == {"resolved": 0, "unresolved": 0, "skipped_dedup": 0}

    @pytest.mark.asyncio
    async def test_checkpoint_fallback_resolves_orphaned_fill(self, tmp_path):
        """When order_id_map is empty (post-crash), checkpoint provides
        symbol→strategy mapping to resolve orphaned fills."""
        # Prepare checkpoint with TXFD6 → strat_maker mapping
        import orjson

        ckpt_data = {
            "trading_date": "20260414",
            "timestamp_ns": 0,
            "peak_equity_scaled": 0,
            "total_realized_pnl_scaled": 0,
            "positions": {
                "ACC1:strat_maker:TXFD6": {
                    "symbol": "TXFD6",
                    "net_qty": 1,
                    "avg_price_scaled": 200000000,
                    "realized_pnl_scaled": 0,
                    "fees_scaled": 0,
                },
            },
        }
        body = orjson.dumps(ckpt_data, option=orjson.OPT_SORT_KEYS)
        import hashlib

        sha = hashlib.sha256(body).hexdigest()
        ckpt_data["sha256"] = sha
        ckpt_path = str(tmp_path / "checkpoint.json")
        with open(ckpt_path, "wb") as f:
            f.write(orjson.dumps(ckpt_data, option=orjson.OPT_SORT_KEYS))

        # Seed DLQ with orphaned fill (unique fill_id to avoid dedup)
        dlq = get_orphaned_fill_dlq()
        orphan = _make_orphaned_fill(symbol="TXFD6", fill_id="G4_TEST_CKPT_1")
        dlq.add(orphan)
        assert dlq.count == 1

        # Router with empty order_id_map (simulating crash recovery)
        router = _make_router()
        result = await router.recover_fill_gaps(checkpoint_path=ckpt_path)

        assert result["resolved"] == 1
        assert result["unresolved"] == 0
        assert dlq.count == 0  # DLQ drained

    @pytest.mark.asyncio
    async def test_unresolvable_fills_remain_in_dlq(self):
        """Fills for unknown symbols stay in DLQ."""
        dlq = get_orphaned_fill_dlq()
        dlq.add(_make_orphaned_fill(symbol="UNKNOWN_SYM", fill_id="F_UNK"))
        assert dlq.count == 1

        router = _make_router()
        result = await router.recover_fill_gaps(checkpoint_path="/nonexistent")

        assert result["resolved"] == 0
        assert result["unresolved"] == 1
        assert dlq.count == 1  # Still in DLQ

    @pytest.mark.asyncio
    async def test_dedup_skips_already_processed_fills(self, tmp_path):
        """Fills whose fill_id is already in dedup window are skipped."""
        import hashlib
        import orjson

        ckpt_data = {
            "trading_date": "20260414",
            "timestamp_ns": 0,
            "peak_equity_scaled": 0,
            "total_realized_pnl_scaled": 0,
            "positions": {
                "ACC1:strat_a:TXFD6": {
                    "symbol": "TXFD6",
                    "net_qty": 1,
                    "avg_price_scaled": 200000000,
                    "realized_pnl_scaled": 0,
                    "fees_scaled": 0,
                },
            },
        }
        body = orjson.dumps(ckpt_data, option=orjson.OPT_SORT_KEYS)
        sha = hashlib.sha256(body).hexdigest()
        ckpt_data["sha256"] = sha
        ckpt_path = str(tmp_path / "checkpoint.json")
        with open(ckpt_path, "wb") as f:
            f.write(orjson.dumps(ckpt_data, option=orjson.OPT_SORT_KEYS))

        dlq = get_orphaned_fill_dlq()
        fill = _make_orphaned_fill(symbol="TXFD6", fill_id="ALREADY_SEEN")
        dlq.add(fill)

        router = _make_router()
        # Pre-seed dedup window
        router._seen_fill_ids["ALREADY_SEEN"] = None

        result = await router.recover_fill_gaps(checkpoint_path=ckpt_path)

        assert result["resolved"] == 0
        assert result["skipped_dedup"] == 1
