import asyncio
import json
from pathlib import Path

import pytest

from hft_platform.order.deadletter import DeadLetterQueue, RejectionReason


@pytest.mark.asyncio
async def test_deadletter_add_flush_and_stats(tmp_path: Path) -> None:
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path), max_buffer_size=2)

    await dlq.add(
        order_id="o1",
        strategy_id="s1",
        symbol="TXF",
        side="BUY",
        price=100,
        qty=1,
        reason=RejectionReason.RATE_LIMIT,
        error_message="rate limited",
    )
    stats = await dlq.get_stats()
    assert stats["buffer_size"] == 1
    assert stats["total_entries"] == 1
    assert stats["total_flushed"] == 0

    await dlq.add(
        order_id="o2",
        strategy_id="s1",
        symbol="TXF",
        side="SELL",
        price=101,
        qty=2,
        reason="custom_reason",
        error_message="custom",
    )

    stats = await dlq.get_stats()
    assert stats["buffer_size"] == 0
    assert stats["total_entries"] == 2
    assert stats["total_flushed"] == 2

    files = list(tmp_path.glob("dlq_*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text().strip().splitlines()
    assert len(lines) == 2
    payload = json.loads(lines[0])
    assert payload["order_id"] in {"o1", "o2"}


def test_deadletter_read_all_skips_bad_lines(tmp_path: Path) -> None:
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path))
    sample = {
        "timestamp_ns": 1,
        "order_id": "o1",
        "strategy_id": "s1",
        "symbol": "TXF",
        "side": "BUY",
        "price": 100,
        "qty": 1,
        "reason": "rate_limit",
        "error_message": "rate limited",
        "intent_type": "NEW",
        "metadata": {},
        "retry_count": 0,
        "trace_id": "",
    }
    fpath = tmp_path / "dlq_1.jsonl"
    fpath.write_text(json.dumps(sample) + "\n" + "not-json\n")

    entries = dlq.read_all()
    assert len(entries) == 1
    assert entries[0].order_id == "o1"

