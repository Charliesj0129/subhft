"""Tests for Cost Attribution Panel (_pnl_panel)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

from hft_platform.monitor._pnl_panel import (
    CostPanelData,
    fetch_cost_attribution,
    render_cost_table,
)


@dataclass
class FakeQueryResult:
    """Minimal stand-in for clickhouse_connect query result."""

    result_rows: list[tuple[Any, ...]]


class FakeCHClient:
    """Fake ClickHouse client that returns pre-configured rows."""

    __slots__ = ("_rows",)

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> FakeQueryResult:
        return FakeQueryResult(result_rows=self._rows)


class TestFetchCostAttribution:
    """Tests for fetch_cost_attribution."""

    def test_fetch_returns_per_strategy_breakdown(self) -> None:
        """Rows are returned per strategy+symbol with correct field mapping."""
        rows = [
            ("alpha_mm", "2330", 50, 125000, 37500),
            ("alpha_ofi", "2317", 20, 48000, 14400),
        ]
        client = FakeCHClient(rows)

        result = fetch_cost_attribution(client, "2026-03-25")

        assert len(result) == 2
        first = result[0]
        assert first.strategy == "alpha_mm"
        assert first.symbol == "2330"
        assert first.fill_count == 50
        assert first.total_fee_scaled == 125000
        assert first.tax_scaled == 37500
        assert first.commission_scaled == 125000 - 37500

    def test_empty_returns_empty(self) -> None:
        """Empty result set yields empty list."""
        client = FakeCHClient([])

        result = fetch_cost_attribution(client, "2026-03-25")

        assert result == []

    def test_ch_failure_returns_empty(self) -> None:
        """ClickHouse exception returns empty list without raising."""
        client = MagicMock()
        client.query.side_effect = Exception("Connection refused")

        result = fetch_cost_attribution(client, "2026-03-25")

        assert result == []

    def test_net_fee_equals_commission_plus_tax(self) -> None:
        """Verify commission_scaled property: total_fee - tax = commission."""
        rows = [
            ("strat_a", "2330", 10, 100000, 30000),
        ]
        client = FakeCHClient(rows)

        result = fetch_cost_attribution(client, "2026-03-25")

        assert len(result) == 1
        entry = result[0]
        assert entry.commission_scaled == entry.total_fee_scaled - entry.tax_scaled
        assert entry.commission_scaled == 70000


class TestRenderCostTable:
    """Tests for render_cost_table."""

    def test_render_with_data(self) -> None:
        """Rendered output includes header, data rows, and total footer."""
        data = [
            CostPanelData(strategy="mm", symbol="2330", fill_count=10, total_fee_scaled=50000, tax_scaled=15000),
            CostPanelData(strategy="ofi", symbol="2317", fill_count=5, total_fee_scaled=20000, tax_scaled=6000),
        ]
        lines = render_cost_table(data)

        assert len(lines) >= 4  # header + 2 data + total
        # Header line should contain column names
        assert "Strategy" in lines[0]
        assert "Total" in lines[0]
        # Total line
        total_line = lines[-1]
        assert "TOTAL" in total_line

    def test_render_empty_data(self) -> None:
        """Empty data returns single 'No fills today' message."""
        lines = render_cost_table([])

        assert lines == ["  No fills today"]
