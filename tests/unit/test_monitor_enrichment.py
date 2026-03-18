from __future__ import annotations

import math

from hft_platform.monitor._enrichment import enrich_tick, validate_l1_row
from hft_platform.monitor._types import RowView, SymbolState, WatchlistSymbol


def _symbol_state(code: str = "TMFC6") -> SymbolState:
    return SymbolState(
        symbol=WatchlistSymbol(
            code=code,
            name=code,
            product_type="future",
            alpha_ids=("queue_imbalance",),
        )
    )


def test_enrich_tick_updates_payload_and_symbol_state() -> None:
    state = _symbol_state()
    row = RowView(
        symbol="TMFC6",
        ingest_ts=1_000,
        bids_price=[210_000_000],
        asks_price=[210_500_000],
        bids_vol=[100],
        asks_vol=[80],
        price_scaled=0,
        volume=1,
    )

    payload = enrich_tick(row, state)

    assert payload["bid_px"] == 210.0
    assert payload["ask_px"] == 210.5
    assert payload["mid_price"] == 210.25
    assert payload["spread_scaled"] == 5_000
    assert math.isclose(payload["spread_bps"], (0.5 / 210.25) * 10_000, rel_tol=1e-6)
    assert math.isclose(payload["imbalance"], 20 / 180, rel_tol=1e-6)
    assert payload["ofi_l1_raw"] == 0.0
    assert payload["ofi_l1_cum"] == 0.0
    assert state.tick_count == 1
    assert state.cursor_ts_ns == 1_000
    assert state.last_update_ns == 1_000


def test_enrich_tick_computes_incremental_ofi() -> None:
    state = _symbol_state()
    first = RowView(
        symbol="TMFC6",
        ingest_ts=1_000,
        bids_price=[210_000_000],
        asks_price=[210_500_000],
        bids_vol=[100],
        asks_vol=[80],
        price_scaled=0,
        volume=1,
    )
    second = RowView(
        symbol="TMFC6",
        ingest_ts=2_000,
        bids_price=[210_100_000],
        asks_price=[210_600_000],
        bids_vol=[115],
        asks_vol=[70],
        price_scaled=0,
        volume=1,
    )

    enrich_tick(first, state)
    payload = enrich_tick(second, state)

    assert payload["ofi_l1_raw"] == 25.0
    assert payload["ofi_l1_cum"] == 25.0
    assert state.tick_count == 2
    assert state.prev_bid_qty == 115.0
    assert state.prev_ask_qty == 70.0
    assert state.cursor_ts_ns == 2_000


def test_validate_l1_row_rejects_empty_or_mismatched_arrays() -> None:
    empty_row = RowView(
        symbol="X", ingest_ts=0,
        bids_price=[], asks_price=[], bids_vol=[], asks_vol=[],
        price_scaled=0, volume=0,
    )
    mismatch_row = RowView(
        symbol="X", ingest_ts=0,
        bids_price=[1, 2], asks_price=[1], bids_vol=[3], asks_vol=[4],
        price_scaled=0, volume=0,
    )

    assert validate_l1_row(empty_row) == "bids_price_empty"
    assert validate_l1_row(mismatch_row) == "bid_book_mismatch"
