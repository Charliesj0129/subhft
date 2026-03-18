"""End-to-end data contract test: BidAskEvent → publisher → Redis JSON → poller → RowView → enrich_tick.

Validates that the fields published by MonitorLivePublisher are correctly decoded
by RedisPoller._decode_row() and produce a valid enrich_tick() payload.
"""

from __future__ import annotations

import json

from hft_platform.monitor._enrichment import enrich_tick, validate_l1_row
from hft_platform.monitor._redis_poller import RedisPoller
from hft_platform.monitor._types import RowView, SymbolState, WatchlistSymbol


def _make_publisher_payload() -> dict:
    """Simulate what map_event_to_record() produces for a BidAskEvent,
    after MonitorLivePublisher._publish_now() serializes it."""
    return {
        "symbol": "2330",
        "ingest_ts": 1710000000000000000,
        "bids_price": [210_000_000],  # 210.0 NTD × 1e6
        "asks_price": [210_500_000],  # 210.5 NTD × 1e6
        "bids_vol": [100],
        "asks_vol": [80],
        "price_scaled": 0,
        "volume": 0,
    }


def _publisher_json(payload: dict) -> str:
    """Simulate _publish_now()'s JSON encoding."""
    return json.dumps(
        {
            "symbol": payload["symbol"],
            "ingest_ts": int(payload["ingest_ts"]),
            "bids_price": payload.get("bids_price", []),
            "asks_price": payload.get("asks_price", []),
            "bids_vol": payload.get("bids_vol", []),
            "asks_vol": payload.get("asks_vol", []),
            "price_scaled": int(payload.get("price_scaled", 0) or 0),
            "volume": int(payload.get("volume", 0) or 0),
        },
        separators=(",", ":"),
    )


def test_publisher_json_roundtrips_through_poller_decode() -> None:
    """Publisher JSON → RedisPoller._decode_row() → RowView matches original."""
    payload = _make_publisher_payload()
    json_str = _publisher_json(payload)

    row = RedisPoller._decode_row(json_str)

    assert row.symbol == "2330"
    assert row.ingest_ts == 1710000000000000000
    assert row.bids_price == [210_000_000]
    assert row.asks_price == [210_500_000]
    assert row.bids_vol == [100]
    assert row.asks_vol == [80]
    assert row.price_scaled == 0
    assert row.volume == 0


def test_decoded_row_passes_validation() -> None:
    """A correctly-structured row from Redis should pass validate_l1_row."""
    payload = _make_publisher_payload()
    json_str = _publisher_json(payload)
    row = RedisPoller._decode_row(json_str)

    reason = validate_l1_row(row)
    assert reason is None, f"Validation failed: {reason}"


def test_decoded_row_produces_valid_enrich_payload() -> None:
    """Full pipeline: publisher JSON → decode → enrich_tick → payload has all expected keys."""
    payload = _make_publisher_payload()
    json_str = _publisher_json(payload)
    row = RedisPoller._decode_row(json_str)

    ws = WatchlistSymbol(code="2330", name="TSMC", product_type="stock", alpha_ids=("queue_imbalance",))
    ss = SymbolState(symbol=ws)

    enriched = enrich_tick(row, ss)

    # Core price fields
    assert enriched["bid_px"] > 0
    assert enriched["ask_px"] > 0
    assert enriched["mid_price"] > 0
    assert enriched["spread_scaled"] > 0
    assert enriched["spread_bps"] > 0
    assert "microprice_x2" in enriched
    assert "imbalance" in enriched
    assert "ofi_l1_raw" in enriched
    assert "ofi_l1_cum" in enriched
    assert "local_ts" in enriched

    # State was updated
    assert ss.tick_count == 1
    assert ss.last_price > 0
    assert ss.spread_bps > 0


def test_second_tick_produces_nonzero_ofi() -> None:
    """After two ticks with different quantities, OFI should be non-zero."""
    ws = WatchlistSymbol(code="2330", name="TSMC", product_type="stock", alpha_ids=("queue_imbalance",))
    ss = SymbolState(symbol=ws)

    # Tick 1
    row1 = RowView(
        symbol="2330",
        ingest_ts=1000,
        bids_price=[210_000_000],
        asks_price=[210_500_000],
        bids_vol=[100],
        asks_vol=[80],
        price_scaled=0,
        volume=0,
    )
    enrich_tick(row1, ss)

    # Tick 2 — quantities changed
    row2 = RowView(
        symbol="2330",
        ingest_ts=2000,
        bids_price=[210_000_000],
        asks_price=[210_500_000],
        bids_vol=[120],  # +20
        asks_vol=[70],   # -10
        price_scaled=0,
        volume=0,
    )
    enriched = enrich_tick(row2, ss)

    # OFI = (bid_qty_change) - (ask_qty_change) = (120-100) - (70-80) = 20 - (-10) = 30
    assert enriched["ofi_l1_raw"] == 30.0
    assert enriched["ofi_l1_cum"] == 30.0


def test_empty_book_fails_validation() -> None:
    """A row with empty book from a bad publisher should fail validation."""
    bad_json = json.dumps({
        "symbol": "2330",
        "ingest_ts": 1000,
        "bids_price": [],
        "asks_price": [],
        "bids_vol": [],
        "asks_vol": [],
        "price_scaled": 0,
        "volume": 0,
    })
    row = RedisPoller._decode_row(bad_json)
    reason = validate_l1_row(row)
    assert reason is not None
    assert "empty" in reason


def test_publisher_payload_field_completeness() -> None:
    """Verify that the publisher payload contains all fields that poller expects."""
    payload = _make_publisher_payload()
    json_str = _publisher_json(payload)
    decoded = json.loads(json_str)

    required_fields = {"symbol", "ingest_ts", "bids_price", "asks_price", "bids_vol", "asks_vol", "price_scaled", "volume"}
    assert required_fields.issubset(set(decoded.keys())), f"Missing: {required_fields - set(decoded.keys())}"
