from hft_platform.backtest.ch_data_source import (
    BacktestDataSource,
    ChDataSource,
    DataValidationError,
)


def test_data_validation_error_is_exception():
    assert issubclass(DataValidationError, Exception)


def test_ch_data_source_implements_protocol():
    src = ChDataSource(ch_host="localhost", ch_port=9000, price_scale=1_000_000)
    assert isinstance(src, BacktestDataSource)


def test_ch_data_source_default_config():
    src = ChDataSource()
    assert src.price_scale == 1_000_000
    assert src.ch_host == "localhost"
    assert src.ch_port == 9000
