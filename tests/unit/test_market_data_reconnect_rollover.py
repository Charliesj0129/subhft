import asyncio
import datetime as dt
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.services.market_data import MarketDataService


@pytest.fixture
def service(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    os.environ["SYMBOLS_CONFIG"] = str(cfg)
    bus = MagicMock(spec=RingBufferBus)
    raw_queue = asyncio.Queue()
    client = MagicMock()
    return MarketDataService(bus, raw_queue, client)


def test_weekend_rollover_reconnect_once(service):
    # Allow reconnect window unconditionally
    service.reconnect_days = set()
    service.reconnect_hours = ""
    service.reconnect_hours_2 = ""

    tz = service._reconnect_tzinfo
    now = dt.datetime(2026, 2, 2, 9, 0, tzinfo=tz)  # Monday
    last = dt.datetime(2026, 1, 31, 5, 0, tzinfo=tz)  # Saturday
    service.last_event_ts = last.timestamp()
    real_datetime = dt.datetime

    with patch("hft_platform.services.market_data.dt.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.fromtimestamp.side_effect = lambda ts, tz=None: real_datetime.fromtimestamp(ts, tz=tz)
        assert service._should_rollover_reconnect() is True
        # Same day should not trigger again
        assert service._should_rollover_reconnect() is False
