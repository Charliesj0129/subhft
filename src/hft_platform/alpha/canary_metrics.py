"""CanaryMetricsQuery — ClickHouse-backed canary performance metric queries.

Queries 4 metrics used by the canary scheduler to evaluate alpha strategies:
  - slippage_bps   : average absolute slippage in basis points
  - drawdown       : maximum drawdown from running cumulative PnL
  - error_rate     : fraction of rejected orders
  - sessions       : number of distinct trading days with fills

Environment variables (shared with audit.py pattern):
  HFT_CLICKHOUSE_HOST  (default localhost)
  HFT_CLICKHOUSE_PORT  (default 8123)
"""

from __future__ import annotations

import os
from typing import Any, Callable

from structlog import get_logger

logger = get_logger("alpha.canary_metrics")


def _default_client_factory() -> Any:
    """Create a ClickHouse client from standard env vars."""
    import clickhouse_connect

    host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))
    return clickhouse_connect.get_client(host=host, port=port)


class CanaryMetricsQuery:
    """Query ClickHouse for 4 canary performance metrics.

    Args:
        client_factory: Callable that returns a ClickHouse client.
            Defaults to :func:`_default_client_factory`.
    """

    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self._client_factory = client_factory or _default_client_factory

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(
        self,
        alpha_id: str,
        strategy_id: str,
        since_ns: int,
    ) -> dict[str, Any] | None:
        """Fetch all 4 canary metrics for the given strategy window.

        Returns a dict with keys ``slippage_bps``, ``drawdown``,
        ``error_rate``, ``sessions``, or ``None`` if any error occurs.

        Args:
            alpha_id:    Alpha identifier (used for logging context only).
            strategy_id: Strategy identifier used to filter CK rows.
            since_ns:    Start of evaluation window as nanosecond epoch int.
        """
        try:
            client = self._client_factory()
        except Exception:  # noqa: BLE001
            logger.error(
                "canary_metrics.fetch: client_factory failed",
                alpha_id=alpha_id,
                strategy_id=strategy_id,
                exc_info=True,
            )
            return None

        try:
            slippage = self._query_slippage(client, strategy_id, since_ns)
            drawdown = self._query_drawdown(client, strategy_id, since_ns)
            error_rate = self._query_error_rate(client, strategy_id, since_ns)
            sessions = self._query_sessions(client, strategy_id, since_ns)
        except Exception:  # noqa: BLE001
            logger.error(
                "canary_metrics.fetch: query failed",
                alpha_id=alpha_id,
                strategy_id=strategy_id,
                exc_info=True,
            )
            return None

        return {
            "slippage_bps": slippage,
            "drawdown": drawdown,
            "error_rate": error_rate,
            "sessions": sessions,
        }

    # ------------------------------------------------------------------
    # Private query methods
    # ------------------------------------------------------------------

    def _query_slippage(self, client: Any, strategy_id: str, since_ns: int) -> float:
        """Average absolute slippage in basis points (fills JOIN orders)."""
        sql = """
            SELECT avg(abs(f.price_scaled - o.price_scaled) / o.price_scaled * 10000)
            FROM hft.fills AS f
            INNER JOIN hft.orders AS o ON f.client_order_id = o.order_id
            WHERE f.strategy_id = {strategy_id:String}
              AND f.ts_exchange >= {since_ns:Int64}
              AND o.price_scaled > 0
        """
        result = client.query(sql, parameters={"strategy_id": strategy_id, "since_ns": since_ns})
        rows = result.result_rows
        if not rows or rows[0][0] is None:
            return 0.0
        return float(rows[0][0])

    def _query_drawdown(self, client: Any, strategy_id: str, since_ns: int) -> float:
        """Maximum drawdown computed from running cumulative PnL.

        Uses a window-function subquery to build the running PnL series,
        then derives peak and final values in Python to avoid complex nested
        ClickHouse syntax differences across versions.
        """
        sql = """
            SELECT
                sum((f.price_scaled * f.qty * (CASE WHEN f.side = 'SELL' THEN 1 ELSE -1 END))
                    - f.fee_scaled) OVER (ORDER BY f.ts_exchange) AS running_pnl
            FROM hft.fills AS f
            WHERE f.strategy_id = {strategy_id:String}
              AND f.ts_exchange >= {since_ns:Int64}
            ORDER BY f.ts_exchange
        """
        result = client.query(sql, parameters={"strategy_id": strategy_id, "since_ns": since_ns})
        rows = result.result_rows
        if not rows:
            return 0.0

        pnl_series = [float(row[0]) for row in rows]
        running_peak = pnl_series[0]
        max_dd = 0.0
        for v in pnl_series:
            if v > running_peak:
                running_peak = v
            if running_peak != 0.0:
                dd = (running_peak - v) / abs(running_peak)
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    def _query_error_rate(self, client: Any, strategy_id: str, since_ns: int) -> float:
        """Fraction of rejected orders over total orders."""
        sql = """
            SELECT countIf(status = 'REJECTED') / count(*) AS error_rate
            FROM hft.orders
            WHERE strategy_id = {strategy_id:String}
              AND ingest_ts >= {since_ns:Int64}
        """
        result = client.query(sql, parameters={"strategy_id": strategy_id, "since_ns": since_ns})
        rows = result.result_rows
        if not rows or rows[0][0] is None:
            return 0.0
        value = rows[0][0]
        # Guard against integer 0 returned when no rows matched
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _query_sessions(self, client: Any, strategy_id: str, since_ns: int) -> int:
        """Number of distinct trading days with fills."""
        sql = """
            SELECT count(distinct toDate(toDateTime(ts_exchange / 1000000000)))
            FROM hft.fills
            WHERE strategy_id = {strategy_id:String}
              AND ts_exchange >= {since_ns:Int64}
        """
        result = client.query(sql, parameters={"strategy_id": strategy_id, "since_ns": since_ns})
        rows = result.result_rows
        if not rows or rows[0][0] is None:
            return 0
        return int(rows[0][0])
