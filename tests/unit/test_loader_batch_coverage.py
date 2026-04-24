"""Coverage tests for hft_platform.recorder._loader_batch — missing line ranges.

Targets: _to_date, format_market_data (price fallback, bid/ask normalization,
float array scaling, timestamp validation, one-sided book warning),
format_orders, format_trades, format_fills, format_risk_log,
format_backtest_runs, format_pnl_snapshots, format_latency_spans,
insert_batch_for_table.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from hft_platform.recorder._loader_batch import (
    _to_date,
    format_backtest_runs,
    format_fills,
    format_latency_spans,
    format_market_data,
    format_orders,
    format_pnl_snapshots,
    format_risk_log,
    format_trades,
    insert_batch_for_table,
)

# ---------------------------------------------------------------------------
# _to_date (lines 26, 28-31)
# ---------------------------------------------------------------------------


def test_to_date_with_date_object():
    d = date(2026, 4, 16)
    result = _to_date(d)
    assert result == d


def test_to_date_with_valid_string():
    result = _to_date("2026-04-16")
    assert result == date(2026, 4, 16)


def test_to_date_with_invalid_string():
    result = _to_date("not-a-date")
    assert result == date(1970, 1, 1)


def test_to_date_with_epoch_string():
    result = _to_date("1970-01-01")
    assert result == date(1970, 1, 1)


def test_to_date_with_none():
    result = _to_date(None)
    assert result == date(1970, 1, 1)


def test_to_date_with_empty_string():
    result = _to_date("")
    assert result == date(1970, 1, 1)


# ---------------------------------------------------------------------------
# format_market_data: basic tick (lines 91-92, 94-95)
# ---------------------------------------------------------------------------


def test_format_market_data_basic_tick():
    rows = [
        {
            "symbol": "TXFD6",
            "exchange": "TAIFEX",
            "type": "tick",
            "exch_ts": 1_000_000_000,
            "price_scaled": 200000000,
            "volume": 10,
        }
    ]
    cols, data = format_market_data(rows)
    assert "symbol" in cols
    assert len(data) == 1
    assert data[0][0] == "TXFD6"
    assert data[0][5] == 200000000  # price_scaled


# ---------------------------------------------------------------------------
# format_market_data: bid/ask as nested arrays (lines 91-92, 94-95)
# ---------------------------------------------------------------------------


def test_format_market_data_nested_bidask():
    rows = [
        {
            "symbol": "TXFD6",
            "type": "bidask",
            "bids": [[20000.0, 5], [19900.0, 10]],
            "asks": [[20100.0, 3], [20200.0, 8]],
            "exch_ts": 1_000_000_000,
        }
    ]
    cols, data = format_market_data(rows)
    assert len(data) == 1
    # bids_price should be scaled
    assert isinstance(data[0][7], list)
    assert len(data[0][7]) == 2


# ---------------------------------------------------------------------------
# format_market_data: float arrays (lines 110-111, 113)
# ---------------------------------------------------------------------------


def test_format_market_data_float_arrays():
    rows = [
        {
            "symbol": "TEST",
            "type": "bidask",
            "bids_price": [20000.0, 19900.0],
            "bids_vol": [5, 10],
            "asks_price": [20100.0, 20200.0],
            "asks_vol": [3, 8],
            "exch_ts": 1_000_000_000,
        }
    ]
    cols, data = format_market_data(rows)
    assert len(data) == 1
    # float prices should be scaled to int
    assert all(isinstance(p, int) for p in data[0][7])


# ---------------------------------------------------------------------------
# format_market_data: price fallback from mid of bids/asks (lines 117, 121-122, 124-125)
# ---------------------------------------------------------------------------


def test_format_market_data_price_from_best_bid_ask():
    rows = [
        {
            "symbol": "TEST",
            "type": "bidask",
            "best_bid": 20000.0,
            "best_ask": 20100.0,
            "exch_ts": 1_000_000_000,
        }
    ]
    cols, data = format_market_data(rows)
    assert len(data) == 1
    assert data[0][5] > 0  # price_scaled derived from mid


def test_format_market_data_price_from_float_price():
    rows = [
        {
            "symbol": "TEST",
            "type": "tick",
            "price": 20050.0,
            "exch_ts": 1_000_000_000,
        }
    ]
    cols, data = format_market_data(rows)
    assert data[0][5] > 0


# ---------------------------------------------------------------------------
# format_market_data: top-of-book only (lines 132, 138, 140)
# ---------------------------------------------------------------------------


def test_format_market_data_top_of_book_only():
    rows = [
        {
            "symbol": "TEST",
            "type": "bidask",
            "best_bid": 200000000,
            "best_ask": 201000000,
            "bid_depth": 5,
            "ask_depth": 3,
            "exch_ts": 1_000_000_000,
        }
    ]
    cols, data = format_market_data(rows)
    assert len(data[0][7]) == 1  # single-level bids_price
    assert len(data[0][9]) == 1  # single-level asks_price


# ---------------------------------------------------------------------------
# format_market_data: one-sided book warning (lines 251-256, 258-259, 261)
# ---------------------------------------------------------------------------


def test_format_market_data_one_sided_book():
    rows = [
        {
            "symbol": "TEST",
            "type": "bidask",
            "bids": [[20000.0, 5]],
            "exch_ts": 1_000_000_000,
        }
    ]
    cols, data = format_market_data(rows)
    assert len(data) == 1  # still processes the row


# ---------------------------------------------------------------------------
# format_market_data: timestamp validation (lines 272, 274)
# ---------------------------------------------------------------------------


def test_format_market_data_future_timestamp():
    """Timestamp far in the future should be clamped."""
    rows = [
        {
            "symbol": "TEST",
            "type": "tick",
            "exch_ts": 99_999_999_999_999_999_999,  # way in the future
            "price_scaled": 100000,
        }
    ]
    cols, data = format_market_data(rows)
    assert len(data) == 1
    # The ts should be clamped (not the original huge value)
    assert data[0][3] < 99_999_999_999_999_999_999


def test_format_market_data_ingest_before_exch():
    """If ingest_ts < exch_ts, ingest_ts is set to exch_ts."""
    rows = [
        {
            "symbol": "TEST",
            "type": "tick",
            "exch_ts": 2_000_000_000_000_000_000,
            "recv_ts": 1_000_000_000,
            "price_scaled": 100000,
        }
    ]
    cols, data = format_market_data(rows)
    assert data[0][4] >= data[0][3]  # ingest_ts >= exch_ts


# ---------------------------------------------------------------------------
# format_market_data: meta fallback (lines 357-362, 364)
# ---------------------------------------------------------------------------


def test_format_market_data_meta_timestamps():
    rows = [
        {
            "symbol": "TEST",
            "type": "tick",
            "meta": {"source_ts": 500, "local_ts": 600, "topic": "test_topic"},
            "price_scaled": 100000,
        }
    ]
    cols, data = format_market_data(rows)
    assert data[0][3] == 500  # exch_ts from meta.source_ts


# ---------------------------------------------------------------------------
# format_orders (lines 371, 373, 394-401, 403, 411, 413)
# ---------------------------------------------------------------------------


def test_format_orders_basic():
    rows = [
        {
            "order_id": "o1",
            "strategy_id": "s1",
            "symbol": "TXFD6",
            "side": "BUY",
            "price_scaled": 200000000,
            "qty": 1,
            "status": "filled",
            "latency_us": 50,
        }
    ]
    cols, data = format_orders(rows)
    assert "order_id" in cols
    assert data[0][0] == "o1"


def test_format_orders_float_price():
    rows = [
        {
            "order_id": "o2",
            "price": 20000.5,
            "qty": 2,
        }
    ]
    cols, data = format_orders(rows)
    assert data[0][5] > 0  # price scaled from float


def test_format_orders_action_fallback():
    rows = [
        {
            "order_id": "o3",
            "action": "SELL",
            "quantity": 5,
        }
    ]
    cols, data = format_orders(rows)
    assert data[0][4] == "SELL"
    assert data[0][6] == 5


# ---------------------------------------------------------------------------
# format_trades (lines 439-441, 443, 456, 458)
# ---------------------------------------------------------------------------


def test_format_trades_basic():
    rows = [
        {
            "trade_id": "t1",
            "order_id": "o1",
            "symbol": "TXFD6",
            "side": "BUY",
            "price_scaled": 200000000,
            "qty": 1,
            "exch_ts": 1_000_000_000,
        }
    ]
    cols, data = format_trades(rows)
    assert "trade_id" in cols
    assert data[0][0] == "t1"


def test_format_trades_float_price():
    rows = [
        {
            "fill_id": "f1",
            "price": 20050.0,
        }
    ]
    cols, data = format_trades(rows)
    assert data[0][5] > 0


# ---------------------------------------------------------------------------
# format_fills (lines 479-481)
# ---------------------------------------------------------------------------


def test_format_fills_basic():
    rows = [
        {
            "ts_exchange": 1_000,
            "ts_local": 2_000,
            "client_order_id": "co1",
            "broker_order_id": "bo1",
            "fill_id": "f1",
            "strategy_id": "s1",
            "symbol": "TXFD6",
            "side": "BUY",
            "qty": 1,
            "price_scaled": 200000000,
            "fee_scaled": 100,
            "tax_scaled": 50,
        }
    ]
    cols, data = format_fills(rows)
    assert len(cols) == 17
    assert data[0][0] == 1_000


def test_format_fills_fallback_fields():
    rows = [
        {
            "match_ts": 3_000,
            "ingest_ts": 4_000,
            "order_id": "o1",
            "trade_id": "t1",
            "price": 20000.0,
            "quantity": 5,
        }
    ]
    cols, data = format_fills(rows)
    assert data[0][0] == 3_000  # ts_exchange from match_ts
    assert data[0][8] == 5  # qty from quantity


# ---------------------------------------------------------------------------
# format_risk_log (lines 357-362, 364)
# ---------------------------------------------------------------------------


def test_format_risk_log_basic():
    rows = [
        {
            "ts": 1_000,
            "strategy_id": "s1",
            "metric": "pnl",
            "value": 100.5,
            "context": {"key": "val"},
        }
    ]
    cols, data = format_risk_log(rows)
    assert "ts" in cols
    assert data[0][0] == 1_000
    assert '"key"' in data[0][4]


def test_format_risk_log_string_context():
    rows = [
        {
            "timestamp": 2_000,
            "metric": "drawdown",
            "value": 0.05,
            "context": "raw_string_context",
        }
    ]
    cols, data = format_risk_log(rows)
    assert data[0][4] == "raw_string_context"


# ---------------------------------------------------------------------------
# format_backtest_runs (lines 394-401, 403)
# ---------------------------------------------------------------------------


def test_format_backtest_runs_basic():
    rows = [
        {
            "run_id": "r1",
            "strategy_id": "s1",
            "start_ts": 100,
            "end_ts": 200,
            "params": {"lr": 0.01},
            "metrics": {"sharpe": 1.5},
        }
    ]
    cols, data = format_backtest_runs(rows)
    assert "run_id" in cols
    assert data[0][0] == "r1"


def test_format_backtest_runs_string_params():
    rows = [
        {
            "run_id": "r2",
            "params": '{"already": "json"}',
            "metrics": '{"already": "json"}',
        }
    ]
    cols, data = format_backtest_runs(rows)
    assert data[0][4] == '{"already": "json"}'


# ---------------------------------------------------------------------------
# format_pnl_snapshots (lines 439-441, 443, 456, 458)
# ---------------------------------------------------------------------------


def test_format_pnl_snapshots_basic():
    rows = [
        {
            "snapshot_ts": 1_000,
            "account_id": "acc1",
            "strategy_id": "s1",
            "symbol": "TXFD6",
            "net_qty": 5,
            "avg_price_scaled": 200000000,
            "realized_pnl_scaled": 50000,
            "fees_scaled": 100,
            "total_pnl_scaled": 49900,
            "peak_equity_scaled": 1000000000,
            "drawdown_pct": 0.01,
        }
    ]
    cols, data = format_pnl_snapshots(rows)
    assert len(cols) == 11
    assert data[0][0] == 1_000


def test_format_pnl_snapshots_missing_fields():
    rows = [{"strategy_id": "s1"}]
    cols, data = format_pnl_snapshots(rows)
    assert data[0][4] == 0  # net_qty default


# ---------------------------------------------------------------------------
# format_latency_spans (lines 479-481)
# ---------------------------------------------------------------------------


def test_format_latency_spans_basic():
    rows = [
        {
            "ingest_ts": 1_000,
            "stage": "normalize",
            "latency_us": 50,
            "trace_id": "tr1",
            "symbol": "TXFD6",
            "strategy_id": "s1",
        }
    ]
    cols, data = format_latency_spans(rows)
    assert len(cols) == 6
    assert data[0][1] == "normalize"


def test_format_latency_spans_ts_fallback():
    rows = [{"ts": 2_000, "stage": "lob"}]
    cols, data = format_latency_spans(rows)
    assert data[0][0] == 2_000


# ---------------------------------------------------------------------------
# insert_batch_for_table (lines 411, 413)
# ---------------------------------------------------------------------------


def test_insert_batch_for_table_unknown_table():
    svc = MagicMock()
    result = insert_batch_for_table(svc, "unknown_table", [{"a": 1}])
    assert result is False


def test_insert_batch_for_table_market_data():
    svc = MagicMock()
    svc._insert_with_retry.return_value = True
    rows = [
        {
            "symbol": "TEST",
            "type": "tick",
            "exch_ts": 1_000,
            "price_scaled": 100000,
        }
    ]
    result = insert_batch_for_table(svc, "market_data", rows)
    assert result is True
    svc._insert_with_retry.assert_called_once()


def test_insert_batch_for_table_fills():
    svc = MagicMock()
    svc._insert_with_retry.return_value = True
    rows = [{"fill_id": "f1", "price_scaled": 100}]
    result = insert_batch_for_table(svc, "fills", rows)
    assert result is True
