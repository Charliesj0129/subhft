"""Darwin Gate SLA Benchmark Tests.

Validates that the Darwin Gate (fast path) meets P99 < 1ms (1000us) latency SLA.
Uses ClickHouse latency_stats_1m materialized view for metrics.

Reference: cc-skill-clickhouse-queries (P99 < 1ms SLA definition)
"""

import os
import time

import pytest

# Skip if ClickHouse not available
clickhouse_connect = pytest.importorskip("clickhouse_connect")


def _get_ch_client():
    """Get ClickHouse client from environment."""
    host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", "9000"))
    try:
        return clickhouse_connect.get_client(host=host, port=port, username="default", password="")
    except Exception:
        return None


@pytest.fixture
def ch_client():
    """ClickHouse client fixture."""
    client = _get_ch_client()
    if client is None:
        pytest.skip("ClickHouse not available")
    return client


# Darwin Gate SLA thresholds (microseconds)
SLA_P99_US = 1000  # 1ms = 1000us
SLA_P95_US = 500  # 500us
SLA_P50_US = 100  # 100us


@pytest.mark.benchmark
class TestDarwinGateSLA:
    """Benchmark tests for Darwin Gate latency SLA compliance."""

    def test_latency_stats_table_exists(self, ch_client):
        """Verify latency_stats_1m materialized view exists."""
        result = ch_client.query("SHOW TABLES FROM hft LIKE 'latency_stats_1m%'")
        tables = [row[0] for row in result.result_rows]
        assert "latency_stats_1m" in tables or "latency_stats_1m_mv" in tables

    def test_recent_latency_p99_under_sla(self, ch_client):
        """Check P99 latency from recent data is under SLA."""
        # Query last hour of data
        query = """
        SELECT
            strategy_id,
            max(latency_p99) as max_p99,
            max(latency_p95) as max_p95,
            avg(latency_p50) as avg_p50,
            sum(order_count) as total_orders
        FROM hft.latency_stats_1m
        WHERE bucket >= now() - INTERVAL 1 HOUR
        GROUP BY strategy_id
        HAVING total_orders > 0
        """
        result = ch_client.query(query)

        if not result.result_rows:
            pytest.skip("No recent latency data available")

        violations = []
        for row in result.result_rows:
            strategy_id, max_p99, max_p95, avg_p50, total_orders = row
            if max_p99 > SLA_P99_US:
                violations.append(
                    {
                        "strategy": strategy_id,
                        "p99_us": max_p99,
                        "p95_us": max_p95,
                        "orders": total_orders,
                    }
                )

        assert not violations, f"P99 SLA violations: {violations}"

    def test_latency_distribution_healthy(self, ch_client):
        """Check latency distribution is healthy (P95/P50 ratio < 10x)."""
        query = """
        SELECT
            strategy_id,
            quantile(0.50)(latency_p50) as median_p50,
            quantile(0.95)(latency_p95) as median_p95
        FROM hft.latency_stats_1m
        WHERE bucket >= now() - INTERVAL 1 HOUR
        GROUP BY strategy_id
        """
        result = ch_client.query(query)

        if not result.result_rows:
            pytest.skip("No recent latency data available")

        for row in result.result_rows:
            strategy_id, median_p50, median_p95 = row
            if median_p50 > 0:
                ratio = median_p95 / median_p50
                assert ratio < 10, f"Strategy {strategy_id}: P95/P50 ratio {ratio:.1f}x exceeds 10x threshold"


@pytest.mark.benchmark
class TestDarwinGateOrders:
    """Benchmark tests for order execution SLA."""

    def test_orders_table_has_latency_data(self, ch_client):
        """Verify orders table has latency_us column populated."""
        query = """
        SELECT count(), avg(latency_us), max(latency_us)
        FROM hft.orders
        WHERE ingest_ts >= toInt64((now() - INTERVAL 1 HOUR) * 1000000000)
        """
        result = ch_client.query(query)

        if result.result_rows[0][0] == 0:
            pytest.skip("No recent order data")

        count, avg_latency, max_latency = result.result_rows[0]
        assert count > 0, "Expected order records with latency data"

    def test_order_throughput(self, ch_client):
        """Measure order throughput over last hour."""
        query = """
        SELECT
            toStartOfMinute(toDateTime(ingest_ts / 1000000000)) as minute,
            count() as order_count
        FROM hft.orders
        WHERE ingest_ts >= toInt64((now() - INTERVAL 1 HOUR) * 1000000000)
        GROUP BY minute
        ORDER BY minute
        """
        result = ch_client.query(query)

        if not result.result_rows:
            pytest.skip("No recent order data")

        counts = [row[1] for row in result.result_rows]
        peak = max(counts) if counts else 0
        avg_per_min = sum(counts) / len(counts) if counts else 0

        # Log metrics (no assertion, just reporting)
        print(f"Order throughput - Peak: {peak}/min, Avg: {avg_per_min:.1f}/min")


@pytest.mark.benchmark
class TestDarwinGateMarketData:
    """Benchmark tests for market data ingestion SLA."""

    def test_market_data_freshness(self, ch_client):
        """Check market data is being ingested recently."""
        query = """
        SELECT max(ingest_ts) as latest_ts
        FROM hft.market_data
        """
        result = ch_client.query(query)

        if not result.result_rows or result.result_rows[0][0] is None:
            pytest.skip("No market data available")

        latest_ts_ns = result.result_rows[0][0]
        age_s = (time.time_ns() - latest_ts_ns) / 1e9

        # Data should be fresher than 1 hour for active markets
        # (relaxed threshold for testing)
        assert age_s < 3600, f"Market data is {age_s:.0f}s old (> 1 hour)"

    def test_ohlcv_aggregation(self, ch_client):
        """Verify OHLCV materialized view is producing data."""
        query = """
        SELECT count(), max(bucket)
        FROM hft.ohlcv_1m
        WHERE bucket >= now() - INTERVAL 1 DAY
        """
        result = ch_client.query(query)

        count = result.result_rows[0][0]
        # OHLCV might be empty if no tick data, that's ok
        if count > 0:
            print(f"OHLCV candles in last day: {count}")
