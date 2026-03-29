"""Verify recorder column lists include instrument metadata fields."""

from __future__ import annotations


def test_market_data_columns_include_instrument_fields():
    from hft_platform.recorder.worker import MARKET_DATA_COLUMNS

    required = ["instrument_type", "underlying", "strike_scaled", "option_right", "expiry"]
    for col in required:
        assert col in MARKET_DATA_COLUMNS, f"Missing column: {col}"


def test_loader_columns_match_worker_columns():
    from hft_platform.recorder._loader_batch import _MARKET_DATA_COLS
    from hft_platform.recorder.worker import MARKET_DATA_COLUMNS

    assert MARKET_DATA_COLUMNS == _MARKET_DATA_COLS, (
        f"Column mismatch!\n  worker: {MARKET_DATA_COLUMNS}\n  loader: {_MARKET_DATA_COLS}"
    )


def test_extract_market_data_values_length_matches_columns():
    from hft_platform.recorder.worker import MARKET_DATA_COLUMNS, _extract_market_data_values

    row = {
        "symbol": "TXFC0",
        "exchange": "TAIFEX",
        "type": "Tick",
        "exch_ts": 1000000000,
        "ingest_ts": 1000000001,
        "price_scaled": 220000000,
        "volume": 1,
        "bids_price": [],
        "bids_vol": [],
        "asks_price": [],
        "asks_vol": [],
        "seq_no": 1,
        "instrument_type": "future",
        "underlying": "TX",
        "strike_scaled": 0,
        "option_right": "",
        "expiry": "1970-01-01",
    }
    values = _extract_market_data_values(row)
    assert values is not None
    assert len(values) == len(MARKET_DATA_COLUMNS), (
        f"Values length {len(values)} != columns length {len(MARKET_DATA_COLUMNS)}"
    )


def test_extract_defaults_for_missing_instrument_fields():
    from hft_platform.recorder.worker import _extract_market_data_values

    row = {
        "symbol": "TXFC0",
        "exchange": "TAIFEX",
        "type": "Tick",
        "exch_ts": 1000000000,
        "ingest_ts": 1000000001,
        "price_scaled": 220000000,
        "volume": 1,
        "bids_price": [],
        "bids_vol": [],
        "asks_price": [],
        "asks_vol": [],
        "seq_no": 1,
        # No instrument fields — should get defaults
    }
    values = _extract_market_data_values(row)
    assert values is not None
    # Last 5 values are instrument fields with defaults
    assert values[-5] == ""  # instrument_type
    assert values[-4] == ""  # underlying
    assert values[-3] == 0  # strike_scaled
    assert values[-2] == ""  # option_right
    assert values[-1] == "1970-01-01"  # expiry


def test_format_market_data_length_matches_loader_cols():
    from hft_platform.recorder._loader_batch import _MARKET_DATA_COLS, format_market_data

    row = {
        "symbol": "TXFC0",
        "exchange": "TAIFEX",
        "type": "Tick",
        "exch_ts": 1000000000,
        "ingest_ts": 1000000001,
        "price_scaled": 220000000,
        "volume": 1,
        "bids_price": [],
        "bids_vol": [],
        "asks_price": [],
        "asks_vol": [],
        "seq_no": 1,
        "instrument_type": "future",
        "underlying": "TX",
        "strike_scaled": 0,
        "option_right": "",
        "expiry": "1970-01-01",
    }
    cols, data = format_market_data([row])
    assert cols == _MARKET_DATA_COLS
    assert len(data) == 1
    assert len(data[0]) == len(_MARKET_DATA_COLS), (
        f"Row length {len(data[0])} != columns length {len(_MARKET_DATA_COLS)}"
    )


def test_format_market_data_defaults_for_missing_instrument_fields():
    from hft_platform.recorder._loader_batch import format_market_data

    row = {
        "symbol": "TXFC0",
        "exchange": "TAIFEX",
        "type": "Tick",
        "exch_ts": 1000000000,
        "ingest_ts": 1000000001,
        "price_scaled": 220000000,
        "volume": 1,
        "bids_price": [],
        "bids_vol": [],
        "asks_price": [],
        "asks_vol": [],
        "seq_no": 1,
        # No instrument fields — should get defaults
    }
    _cols, data = format_market_data([row])
    assert len(data) == 1
    row_data = data[0]
    # Last 5 values are instrument fields with defaults
    assert row_data[-5] == ""  # instrument_type
    assert row_data[-4] == ""  # underlying
    assert row_data[-3] == 0  # strike_scaled
    assert row_data[-2] == ""  # option_right
    assert row_data[-1] == "1970-01-01"  # expiry
