"""Tests for PositionStore.mark_to_market()."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.execution.positions import Position, PositionStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_rust(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
    monkeypatch.setattr(
        "hft_platform.observability.metrics.MetricsRegistry.get",
        staticmethod(lambda: None),
    )


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> PositionStore:
    mock_metadata = MagicMock()
    mock_metadata.price_scale.return_value = 10_000
    mock_metadata.contract_multiplier.return_value = 1
    monkeypatch.setattr(
        "hft_platform.execution.positions.SymbolMetadata",
        lambda *a, **kw: mock_metadata,
    )
    mock_provider = MagicMock()
    mock_provider.price_scale.return_value = 10_000
    monkeypatch.setattr(
        "hft_platform.execution.positions.SymbolMetadataPriceScaleProvider",
        lambda *a, **kw: mock_provider,
    )
    mock_codec = MagicMock()
    mock_codec.scale_factor.return_value = 10_000
    monkeypatch.setattr(
        "hft_platform.execution.positions.PriceCodec",
        lambda *a, **kw: mock_codec,
    )
    s = PositionStore()
    s._rust_tracker = None
    s.metrics = None
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMarkToMarket:
    def test_long_position_profit(self, store: PositionStore) -> None:
        """Long position gains when mid price rises above avg price."""
        # avg_price=200_0000 (200.0000 points), mid=201_0000 (201.0000 points)
        # unrealized = (201_0000 - 200_0000) * 1 = 1_0000
        pos = Position("acct1", "strat1", "2330")
        pos.net_qty = 1
        pos.avg_price_scaled = 200_0000
        store.positions["acct1:strat1:2330"] = pos

        result = store.mark_to_market({"2330": 201_0000})

        assert result == 1_0000

    def test_short_position_loss(self, store: PositionStore) -> None:
        """Short position loses when mid price rises above avg price."""
        # avg_price=200_0000, mid=201_0000, net_qty=-1
        # unrealized = (201_0000 - 200_0000) * -1 = -1_0000
        pos = Position("acct1", "strat1", "2330")
        pos.net_qty = -1
        pos.avg_price_scaled = 200_0000
        store.positions["acct1:strat1:2330"] = pos

        result = store.mark_to_market({"2330": 201_0000})

        assert result == -1_0000

    def test_flat_store_returns_zero(self, store: PositionStore) -> None:
        """Empty position store returns 0 unrealized PnL."""
        result = store.mark_to_market({"2330": 201_0000})

        assert result == 0

    def test_multiple_symbols_summed(self, store: PositionStore) -> None:
        """Unrealized PnL is summed across all positions."""
        # Symbol A: long 2, avg=100_0000, mid=102_0000 → gain = 2 * 2_0000 = 4_0000
        pos_a = Position("acct1", "strat1", "SYMA")
        pos_a.net_qty = 2
        pos_a.avg_price_scaled = 100_0000
        store.positions["acct1:strat1:SYMA"] = pos_a

        # Symbol B: short 1, avg=200_0000, mid=198_0000 → gain = (198_0000-200_0000)*-1 = 2_0000
        pos_b = Position("acct1", "strat1", "SYMB")
        pos_b.net_qty = -1
        pos_b.avg_price_scaled = 200_0000
        store.positions["acct1:strat1:SYMB"] = pos_b

        result = store.mark_to_market({"SYMA": 102_0000, "SYMB": 198_0000})

        # SYMA: (102_0000 - 100_0000) * 2 = 4_0000
        # SYMB: (198_0000 - 200_0000) * -1 = 2_0000
        assert result == 6_0000

    def test_missing_price_skipped(self, store: PositionStore) -> None:
        """Positions without a mid price entry contribute 0."""
        pos = Position("acct1", "strat1", "2330")
        pos.net_qty = 5
        pos.avg_price_scaled = 200_0000
        store.positions["acct1:strat1:2330"] = pos

        # Provide no price for "2330"
        result = store.mark_to_market({})

        assert result == 0

    def test_flat_position_skipped(self, store: PositionStore) -> None:
        """Positions with net_qty=0 contribute 0 even if price is present."""
        pos = Position("acct1", "strat1", "2330")
        pos.net_qty = 0
        pos.avg_price_scaled = 200_0000
        store.positions["acct1:strat1:2330"] = pos

        result = store.mark_to_market({"2330": 999_0000})

        assert result == 0

    def test_partial_price_coverage(self, store: PositionStore) -> None:
        """Only positions with available mid price are counted."""
        pos_a = Position("acct1", "strat1", "PRICED")
        pos_a.net_qty = 1
        pos_a.avg_price_scaled = 100_0000
        store.positions["acct1:strat1:PRICED"] = pos_a

        pos_b = Position("acct1", "strat1", "UNPRICED")
        pos_b.net_qty = 10
        pos_b.avg_price_scaled = 50_0000
        store.positions["acct1:strat1:UNPRICED"] = pos_b

        # Only price for PRICED symbol: gain = (105_0000 - 100_0000) * 1 = 5_0000
        result = store.mark_to_market({"PRICED": 105_0000})

        assert result == 5_0000

    def test_returns_scaled_int(self, store: PositionStore) -> None:
        """Return value is an int (Precision Law compliance)."""
        pos = Position("acct1", "strat1", "2330")
        pos.net_qty = 1
        pos.avg_price_scaled = 200_0000
        store.positions["acct1:strat1:2330"] = pos

        result = store.mark_to_market({"2330": 201_0000})

        assert isinstance(result, int)
