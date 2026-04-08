"""Per-table row formatting for ClickHouse batch inserts.

Each ``format_*`` function transforms a list of raw WAL dicts into
``(cols, data)`` tuples ready for ``_insert_with_retry``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from hft_platform.recorder._loader_common import (
    _TS_MAX_FUTURE_NS,
    _dumps,
    _to_scaled,
    logger,
    timebase,
)

_EPOCH = date(1970, 1, 1)


def _to_date(value: object) -> date:
    """Convert a string or date-like value to datetime.date for ClickHouse."""
    if isinstance(value, date):
        return value
    if value and isinstance(value, str) and value != "1970-01-01":
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return _EPOCH


# ---------------------------------------------------------------------------
# market_data
# ---------------------------------------------------------------------------

_MARKET_DATA_COLS: list[str] = [
    "symbol",
    "exchange",
    "type",
    "exch_ts",
    "ingest_ts",
    "price_scaled",
    "volume",
    "bids_price",
    "bids_vol",
    "asks_price",
    "asks_vol",
    "seq_no",
    "trade_direction",
    # Multi-instrument fields (added 2026-03-30)
    "instrument_type",
    "underlying",
    "strike_scaled",
    "option_right",
    "expiry",
]


def format_market_data(
    rows: list[dict[str, Any]],
) -> tuple[list[str], list[list]]:
    """Return ``(cols, data)`` for the ``hft.market_data`` table."""
    data: list[list] = []
    for r in rows:
        meta = r.get("meta") or {}
        ts = int(
            r.get("exch_ts") or r.get("ts") or r.get("timestamp") or r.get("event_ts") or meta.get("source_ts") or 0
        )
        ingest_ts = int(
            r.get("recv_ts")
            or r.get("ingest_ts")
            or r.get("ts")
            or r.get("timestamp")
            or meta.get("local_ts")
            or timebase.now_ns()
        )

        price_scaled = r.get("price_scaled")
        bids_price = r.get("bids_price") or r.get("bid_price")
        asks_price = r.get("asks_price") or r.get("ask_price")
        bids_vol = r.get("bids_vol") or r.get("bid_vol")
        asks_vol = r.get("asks_vol") or r.get("ask_vol")

        # Normalize bid/ask arrays when provided as [[price, vol], ...]
        raw_bids = r.get("bids")
        raw_asks = r.get("asks")
        if raw_bids and isinstance(raw_bids, (list, tuple)) and isinstance(raw_bids[0], (list, tuple)):
            bids_price = [_to_scaled(p[0]) for p in raw_bids]
            bids_vol = [int(p[1]) for p in raw_bids]
        if raw_asks and isinstance(raw_asks, (list, tuple)) and isinstance(raw_asks[0], (list, tuple)):
            asks_price = [_to_scaled(p[0]) for p in raw_asks]
            asks_vol = [int(p[1]) for p in raw_asks]

        # Convert float arrays to scaled int arrays (legacy support)
        if bids_price and isinstance(bids_price[0], float):
            bids_price = [_to_scaled(p) for p in bids_price]
        if asks_price and isinstance(asks_price[0], float):
            asks_price = [_to_scaled(p) for p in asks_price]

        best_bid = r.get("best_bid") or (bids_price[0] if bids_price else None)
        best_ask = r.get("best_ask") or (asks_price[0] if asks_price else None)

        # Handle price: prefer price_scaled, fallback to scaling float price
        if price_scaled is None:
            price_float = r.get("price") or r.get("mid_price")
            if price_float is None and best_bid is not None and best_ask is not None:
                if isinstance(best_bid, int) and best_bid > 10000:
                    price_scaled = (best_bid + best_ask) // 2
                else:
                    price_scaled = _to_scaled((float(best_bid) + float(best_ask)) / 2)
            elif price_float is not None:
                price_scaled = _to_scaled(price_float)
            else:
                price_scaled = 0

        # If we only have top-of-book, still store it as depth-1 arrays
        if not bids_price and best_bid is not None:
            bids_price = [_to_scaled(best_bid) if isinstance(best_bid, float) else int(best_bid)]
            bids_vol = [int(r.get("bid_depth") or 0)]
        if not asks_price and best_ask is not None:
            asks_price = [_to_scaled(best_ask) if isinstance(best_ask, float) else int(best_ask)]
            asks_vol = [int(r.get("ask_depth") or 0)]

        # Timestamp validation
        if ts:
            if _TS_MAX_FUTURE_NS:
                now_ns = timebase.now_ns()
                if ts - now_ns > _TS_MAX_FUTURE_NS:
                    logger.warning(
                        "Exchange timestamp in future",
                        symbol=r.get("symbol"),
                        delta_ns=ts - now_ns,
                        max_future_ns=_TS_MAX_FUTURE_NS,
                    )
                    ts = now_ns
            if ingest_ts < ts:
                ingest_ts = ts

        # Warn on one-sided book (not tick, not both-empty)
        row_type = str(r.get("type") or "").strip().lower()
        has_bids = bool(bids_price)
        has_asks = bool(asks_price)
        if row_type != "tick" and has_bids != has_asks:
            logger.warning(
                "Missing orderbook side in WAL row",
                symbol=r.get("symbol"),
                has_bids=has_bids,
                has_asks=has_asks,
            )

        row_data = [
            r.get("symbol", ""),
            r.get("exchange", r.get("exch", "TSE")),
            r.get("type", meta.get("topic", "")),
            ts,
            ingest_ts,
            int(price_scaled),
            int(r.get("volume", r.get("total_volume", 0)) or 0),
            bids_price or [],
            bids_vol or [],
            asks_price or [],
            asks_vol or [],
            int(r.get("seq_no", r.get("seq") or 0)),
            int(r.get("trade_direction", 0)),
            # Multi-instrument fields (added 2026-03-30)
            r.get("instrument_type", ""),
            r.get("underlying", ""),
            int(r.get("strike_scaled", 0)),
            r.get("option_right", ""),
            _to_date(r.get("expiry", "1970-01-01")),
        ]
        data.append(row_data)

    return _MARKET_DATA_COLS, data


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------

_ORDERS_COLS: list[str] = [
    "order_id",
    "strategy_id",
    "symbol",
    "side",
    "price_scaled",
    "qty",
    "status",
    "ingest_ts",
    "latency_us",
    "instrument_type",
    "oc_type",
]


def format_orders(
    rows: list[dict[str, Any]],
) -> tuple[list[str], list[list]]:
    """Return ``(cols, data)`` for the ``hft.orders`` table."""
    data: list[list] = []
    for r in rows:
        price = r.get("price_scaled")
        if price is None:
            price_float = r.get("price")
            price = _to_scaled(price_float) if price_float is not None else 0

        ingest_ts = int(r.get("ingest_ts") or r.get("recv_ts") or timebase.now_ns())

        row_data = [
            str(r.get("order_id", "")),
            str(r.get("strategy_id", "")),
            str(r.get("symbol", "")),
            str(r.get("side", r.get("action", ""))),
            int(price),
            int(r.get("qty", r.get("quantity", 0)) or 0),
            str(r.get("status", "")),
            ingest_ts,
            int(r.get("latency_us", 0) or 0),
            str(r.get("instrument_type", "")),
            str(r.get("oc_type", "")),
        ]
        data.append(row_data)

    return _ORDERS_COLS, data


# ---------------------------------------------------------------------------
# trades / fills
# ---------------------------------------------------------------------------

_TRADES_COLS: list[str] = [
    "trade_id",
    "order_id",
    "symbol",
    "exchange",
    "side",
    "price_scaled",
    "qty",
    "exch_ts",
    "ingest_ts",
]


def format_trades(
    rows: list[dict[str, Any]],
) -> tuple[list[str], list[list]]:
    """Return ``(cols, data)`` for the ``hft.trades`` table (legacy)."""
    data: list[list] = []
    for r in rows:
        price = r.get("price_scaled")
        if price is None:
            price_float = r.get("price")
            price = _to_scaled(price_float) if price_float is not None else 0

        exch_ts = int(r.get("exch_ts") or r.get("ts") or r.get("timestamp") or 0)
        ingest_ts = int(r.get("ingest_ts") or r.get("recv_ts") or timebase.now_ns())

        row_data = [
            str(r.get("trade_id", r.get("fill_id", ""))),
            str(r.get("order_id", "")),
            str(r.get("symbol", "")),
            str(r.get("exchange", r.get("exch", ""))),
            str(r.get("side", r.get("action", ""))),
            int(price),
            int(r.get("qty", r.get("quantity", 0)) or 0),
            exch_ts,
            ingest_ts,
        ]
        data.append(row_data)

    return _TRADES_COLS, data


# ---------------------------------------------------------------------------
# fills (unified — targets hft.fills)
# ---------------------------------------------------------------------------

_FILLS_COLS: list[str] = [
    "ts_exchange",
    "ts_local",
    "client_order_id",
    "broker_order_id",
    "fill_id",
    "strategy_id",
    "symbol",
    "side",
    "qty",
    "price_scaled",
    "fee_scaled",
    "tax_scaled",
    "decision_price",
    "arrival_price",
    "source",
    "instrument_type",
    "oc_type",
]


def format_fills(
    rows: list[dict[str, Any]],
) -> tuple[list[str], list[list]]:
    """Return ``(cols, data)`` for the ``hft.fills`` table."""
    data: list[list] = []
    for r in rows:
        price = r.get("price_scaled")
        if price is None:
            price_float = r.get("price")
            price = _to_scaled(price_float) if price_float is not None else 0

        ts_exchange = int(r.get("ts_exchange") or r.get("match_ts") or r.get("exch_ts") or r.get("ts") or 0)
        ts_local = int(r.get("ts_local") or r.get("ingest_ts") or r.get("recv_ts") or timebase.now_ns())

        row_data = [
            ts_exchange,
            ts_local,
            str(r.get("client_order_id", "")),
            str(r.get("broker_order_id", r.get("order_id", ""))),
            str(r.get("fill_id", r.get("trade_id", ""))),
            str(r.get("strategy_id", "")),
            str(r.get("symbol", "")),
            str(r.get("side", r.get("action", ""))),
            int(r.get("qty", r.get("quantity", 0)) or 0),
            int(price),
            int(r.get("fee_scaled", 0) or 0),
            int(r.get("tax_scaled", 0) or 0),
            int(r.get("decision_price", 0) or 0),
            int(r.get("arrival_price", 0) or 0),
            str(r.get("source", "")),
            str(r.get("instrument_type", "")),
            str(r.get("oc_type", "")),
        ]
        data.append(row_data)

    return _FILLS_COLS, data


# ---------------------------------------------------------------------------
# risk_log
# ---------------------------------------------------------------------------

_RISK_LOG_COLS: list[str] = [
    "ts",
    "strategy_id",
    "metric",
    "value",
    "context",
]


def format_risk_log(
    rows: list[dict[str, Any]],
) -> tuple[list[str], list[list]]:
    """Return ``(cols, data)`` for the ``hft.risk_log`` table."""
    data: list[list] = []
    for r in rows:
        ts = int(r.get("ts") or r.get("timestamp") or r.get("ingest_ts") or timebase.now_ns())
        context = r.get("context", {})
        if isinstance(context, dict):
            context = _dumps(context)

        row_data = [
            ts,
            str(r.get("strategy_id", "")),
            str(r.get("metric", "")),
            float(r.get("value", 0)),
            str(context),
        ]
        data.append(row_data)

    return _RISK_LOG_COLS, data


# ---------------------------------------------------------------------------
# backtest_runs
# ---------------------------------------------------------------------------

_BACKTEST_RUNS_COLS: list[str] = [
    "run_id",
    "strategy_id",
    "start_ts",
    "end_ts",
    "params",
    "metrics",
]


def format_backtest_runs(
    rows: list[dict[str, Any]],
) -> tuple[list[str], list[list]]:
    """Return ``(cols, data)`` for the ``hft.backtest_runs`` table."""
    data: list[list] = []
    for r in rows:
        params = r.get("params", {})
        if isinstance(params, dict):
            params = _dumps(params)
        metrics = r.get("metrics", {})
        if isinstance(metrics, dict):
            metrics = _dumps(metrics)

        row_data = [
            str(r.get("run_id", "")),
            str(r.get("strategy_id", "")),
            int(r.get("start_ts", 0)),
            int(r.get("end_ts", 0)),
            str(params),
            str(metrics),
        ]
        data.append(row_data)

    return _BACKTEST_RUNS_COLS, data


# ---------------------------------------------------------------------------
# pnl_snapshots
# ---------------------------------------------------------------------------

_PNL_SNAPSHOTS_COLS: list[str] = [
    "snapshot_ts",
    "account_id",
    "strategy_id",
    "symbol",
    "net_qty",
    "avg_price_scaled",
    "realized_pnl_scaled",
    "fees_scaled",
    "total_pnl_scaled",
    "peak_equity_scaled",
    "drawdown_pct",
]


def format_pnl_snapshots(
    rows: list[dict[str, Any]],
) -> tuple[list[str], list[list]]:
    """Return ``(cols, data)`` for the ``hft.pnl_snapshots`` table."""
    data: list[list] = []
    for r in rows:
        ts = int(r.get("snapshot_ts") or r.get("ts") or r.get("timestamp") or timebase.now_ns())

        row_data = [
            ts,
            str(r.get("account_id", "")),
            str(r.get("strategy_id", "")),
            str(r.get("symbol", "")),
            int(r.get("net_qty", 0) or 0),
            int(r.get("avg_price_scaled", 0) or 0),
            int(r.get("realized_pnl_scaled", 0) or 0),
            int(r.get("fees_scaled", 0) or 0),
            int(r.get("total_pnl_scaled", 0) or 0),
            int(r.get("peak_equity_scaled", 0) or 0),
            float(r.get("drawdown_pct", 0.0) or 0.0),
        ]
        data.append(row_data)

    return _PNL_SNAPSHOTS_COLS, data


# ---------------------------------------------------------------------------
# latency_spans
# ---------------------------------------------------------------------------

_LATENCY_SPANS_COLS: list[str] = [
    "ingest_ts",
    "stage",
    "latency_us",
    "trace_id",
    "symbol",
    "strategy_id",
]


def format_latency_spans(
    rows: list[dict[str, Any]],
) -> tuple[list[str], list[list]]:
    """Return ``(cols, data)`` for the ``hft.latency_spans`` table."""
    data: list[list] = []
    for r in rows:
        ts = int(r.get("ingest_ts") or r.get("ts") or r.get("timestamp") or timebase.now_ns())

        row_data = [
            ts,
            str(r.get("stage", "")),
            int(r.get("latency_us", 0) or 0),
            str(r.get("trace_id", "")),
            str(r.get("symbol", "")),
            str(r.get("strategy_id", "")),
        ]
        data.append(row_data)

    return _LATENCY_SPANS_COLS, data


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_TABLE_FORMATTERS = {
    "market_data": ("hft.market_data", format_market_data),
    "orders": ("hft.orders", format_orders),
    "trades": ("hft.trades", format_trades),
    "fills": ("hft.fills", format_fills),
    "risk_log": ("hft.risk_log", format_risk_log),
    "backtest_runs": ("hft.backtest_runs", format_backtest_runs),
    "pnl_snapshots": ("hft.pnl_snapshots", format_pnl_snapshots),
    "latency_spans": ("hft.latency_spans", format_latency_spans),
}


def insert_batch_for_table(svc: Any, table: str, rows: list[dict[str, Any]]) -> bool:
    """Format *rows* for *table* and insert via ``svc._insert_with_retry``.

    Returns ``True`` on success, ``False`` on failure or unknown table.
    """
    entry = _TABLE_FORMATTERS.get(table)
    if entry is None:
        logger.warning("No insert logic for table", table=table, count=len(rows))
        return False

    full_table_name, formatter = entry
    cols, data = formatter(rows)
    return svc._insert_with_retry(full_table_name, cols, data, table, len(rows))
