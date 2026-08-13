"""Verify mapper populates instrument metadata fields."""

from __future__ import annotations

import datetime

import numpy as np
import pytest
import yaml

from hft_platform.events import BidAskEvent, MetaData, TickEvent
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.recorder.mapper import map_event_to_record


@pytest.fixture
def metadata_with_registry(tmp_path):
    data = {
        "symbols": [
            {
                "code": "TXFC0",
                "exchange": "FUT",
                "tags": ["futures"],
                "point_value": 200,
                "tick_size": 1.0,
            },
        ],
    }
    path = tmp_path / "symbols.yaml"
    path.write_text(yaml.dump(data))
    return SymbolMetadata(str(path))


class TestMapperInstrumentFields:
    def test_tick_event_has_instrument_type(self, metadata_with_registry):
        meta = MetaData(seq=1, topic="tick", source_ts=1000000000, local_ts=1000000001)
        tick = TickEvent(
            meta=meta,
            symbol="TXFC0",
            price=220000000,
            volume=1,
        )
        result = map_event_to_record(tick, metadata_with_registry)
        assert result is not None
        table, record = result
        assert record.get("instrument_type") == "future"
        assert record.get("underlying") == ""  # not set in yaml
        assert record.get("strike_scaled") == 0
        assert record.get("option_right") == ""
        # Was `date(1970, 1, 1)` — the ClickHouse schema default, i.e. "never
        # written", which is the defect the expiry work fixed rather than a
        # property worth pinning. The yaml above sets no expiry, so `TXFC0`
        # (root TXF, month C = March, year digit 0, rolled forward out of the
        # past) derives the third Wednesday of March 2030.
        assert record.get("expiry") == datetime.date(2030, 3, 20)

    def test_bidask_event_has_instrument_type(self, metadata_with_registry):
        meta = MetaData(seq=2, topic="bidask", source_ts=1000000000, local_ts=1000000001)
        ba = BidAskEvent(
            meta=meta,
            symbol="TXFC0",
            bids=np.array([[220000000, 5]], dtype=np.int64),
            asks=np.array([[220010000, 3]], dtype=np.int64),
        )
        result = map_event_to_record(ba, metadata_with_registry)
        assert result is not None
        table, record = result
        assert record.get("instrument_type") == "future"

    def test_unknown_symbol_gets_empty_defaults(self, metadata_with_registry):
        meta = MetaData(seq=3, topic="tick", source_ts=1000000000, local_ts=1000000001)
        tick = TickEvent(
            meta=meta,
            symbol="UNKNOWN",
            price=100,
            volume=1,
        )
        result = map_event_to_record(tick, metadata_with_registry)
        assert result is not None
        _, record = result
        assert record.get("instrument_type") == ""
        assert record.get("strike_scaled") == 0
