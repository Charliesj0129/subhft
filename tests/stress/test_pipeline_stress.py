import os
import time

import pytest

from hft_platform.events import BidAskEvent, MetaData
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.feed_adapter.lob_engine import LOBEngine


def _require_stress():
    if os.getenv("HFT_RUN_STRESS") != "1":
        pytest.skip("Set HFT_RUN_STRESS=1 to run stress tests")


@pytest.mark.stress
def test_lob_engine_stress_updates():
    _require_stress()

    updates = int(os.getenv("HFT_STRESS_LOB_UPDATES", "10000"))
    max_s = float(os.getenv("HFT_STRESS_LOB_MAX_S", "5.0"))

    engine = LOBEngine()
    base_ts = time.time_ns()
    bids = [[10000, 10], [9990, 8], [9980, 6], [9970, 4], [9960, 2]]
    asks = [[10010, 9], [10020, 7], [10030, 5], [10040, 3], [10050, 1]]

    start = time.time()
    for i in range(updates):
        meta = MetaData(seq=i + 1, topic="bidask", source_ts=base_ts + i, local_ts=base_ts + i)
        event = BidAskEvent(meta=meta, symbol="STRESS", bids=bids, asks=asks, is_snapshot=False)
        engine.process_event(event)
    elapsed = time.time() - start

    book = engine.get_book("STRESS")
    assert book.version == updates
    assert book.bids and book.asks
    assert elapsed <= max_s


@pytest.mark.stress
def test_execution_normalizer_stress():
    _require_stress()

    total = int(os.getenv("HFT_STRESS_NORMALIZER_EVENTS", "10000"))
    max_s = float(os.getenv("HFT_STRESS_NORMALIZER_MAX_S", "5.0"))

    normalizer = ExecutionNormalizer()
    base_ts = time.time_ns()

    start = time.time()
    for i in range(total):
        raw = RawExecEvent(
            "deal",
            {
                "seq_no": f"F{i}",
                "ord_no": f"O{i}",
                "code": "STRESS",
                "action": "Buy",
                "quantity": 1,
                "price": 12.34,
                "ts": base_ts + i,
            },
            base_ts + i,
        )
        event = normalizer.normalize_fill(raw)
        assert event is not None
        assert event.symbol == "STRESS"
    elapsed = time.time() - start

    assert elapsed <= max_s
