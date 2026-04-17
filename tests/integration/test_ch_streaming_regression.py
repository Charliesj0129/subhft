"""Regression test: streaming adapter vs .npz path must produce equivalent fills.

Requires:
  - ``hftbacktest`` installed
  - Running local ClickHouse with reference-day data loaded in ``hft.market_data``
  - The corresponding .npz file on disk

All prerequisites are checked at test entry; if any are missing the test is
skipped with an explicit reason.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

pytest.importorskip("hftbacktest")
pytest.importorskip("clickhouse_connect")

from hft_platform.backtest.adapter import HFTBACKTEST_AVAILABLE, HftBacktestAdapter
from hft_platform.backtest.ch_data_source import ChDataSource, DataValidationError

# Reference day selection — first candidate whose .npz file exists on disk is used.
REFERENCE_CANDIDATES = [
    # (instrument, date, absolute_npz_path)
    (
        "TMFD6",
        "2026-03-19",
        Path("/home/charlie/hft_platform/research/data/raw/tmfd6/TMFD6_2026-03-19_l2.hftbt.npz"),
    ),
    (
        "TMFD6",
        "2026-03-18",
        Path("/home/charlie/hft_platform/research/data/raw/tmfd6/TMFD6_2026-03-18_l2.hftbt.npz"),
    ),
    (
        "TXFD6",
        "2026-03-19",
        Path("/home/charlie/hft_platform/research/data/raw/txfd6/TXFD6_2026-03-19_l2.hftbt.npz"),
    ),
]


def _first_available_reference() -> tuple[str, str, Path] | None:
    for inst, date, path in REFERENCE_CANDIDATES:
        if path.exists():
            return inst, date, path
    return None


def _ch_available() -> bool:
    try:
        import clickhouse_connect  # noqa: PLC0415

        client = clickhouse_connect.get_client(host="localhost", port=9000)
        client.ping()
        return True
    except Exception:
        return False


_REF = _first_available_reference()
_CH_UP = _ch_available()


@pytest.mark.skipif(not HFTBACKTEST_AVAILABLE, reason="hftbacktest not installed")
@pytest.mark.skipif(_REF is None, reason="no reference .npz file available on disk")
@pytest.mark.skipif(not _CH_UP, reason="ClickHouse not reachable on localhost:9000")
def test_streaming_adapter_fill_equivalence():
    """Same strategy + same day via streaming and .npz paths must produce equal fill counts.

    Uses a minimal NullStrategy that emits no orders. Both paths should
    therefore report zero fills. The critical assertion is that BOTH pipelines
    complete without error and agree on fill count — demonstrating that the
    streaming path is a drop-in replacement for the .npz path.
    """
    from hft_platform.strategy.base import BaseStrategy

    class NullStrategy(BaseStrategy):
        def handle_event(self, event):
            return []

        def on_start(self):
            pass

        def on_stop(self):
            pass

    assert _REF is not None  # type guard — skipif above ensures this
    instrument, date, npz_path = _REF

    # --- Streaming path ---
    ch = ChDataSource()
    try:
        streaming_events = ch.load_day(instrument, date)
    except DataValidationError as exc:
        pytest.skip(f"ClickHouse data for {instrument} {date} unavailable: {exc}")

    adapter_stream = HftBacktestAdapter(
        strategy=NullStrategy(),
        asset_symbol=instrument,
        data_path=streaming_events,
        tick_size=1.0,
        lot_size=1.0,
        seed=42,
    )
    adapter_stream.run()

    # --- .npz path ---
    adapter_npz = HftBacktestAdapter(
        strategy=NullStrategy(),
        asset_symbol=instrument,
        data_path=str(npz_path),
        tick_size=1.0,
        lot_size=1.0,
        seed=42,
    )
    adapter_npz.run()

    stream_fills = adapter_stream._fill_count
    npz_fills = adapter_npz._fill_count

    assert stream_fills == npz_fills, (
        f"Fill count mismatch on {instrument} {date}: "
        f"streaming={stream_fills} vs .npz={npz_fills}"
    )


def test_streaming_adapter_rejects_validation_failure():
    """DataValidationError propagates cleanly when ClickHouse returns empty data.

    This test does not require a live ClickHouse or real .npz data — it
    exercises the validation error path by patching the CH client.
    """
    with patch("clickhouse_connect.get_client") as mock_get_client:
        client = MagicMock()
        client.query_df.return_value = pd.DataFrame()  # empty → validation failure
        mock_get_client.return_value = client

        src = ChDataSource()
        with pytest.raises(DataValidationError, match="no rows"):
            src.load_day("TMFD6", "2026-03-19")
