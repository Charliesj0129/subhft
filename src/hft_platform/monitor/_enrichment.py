"""Tick enrichment: transform CH raw rows into alpha payload dicts."""

from __future__ import annotations

from typing import Any

from hft_platform.monitor._types import CH_PRICE_SCALE, CH_TO_PLATFORM_DIVISOR, PLATFORM_SCALE, RowView, SymbolState


def validate_l1_row(row: RowView) -> str | None:
    """Return None when a CH market-data row is usable for L1 enrichment."""
    bids_price = row.bids_price
    asks_price = row.asks_price
    bids_vol = row.bids_vol
    asks_vol = row.asks_vol

    if not isinstance(bids_price, (list, tuple)) or not bids_price:
        return "bids_price_empty" if isinstance(bids_price, (list, tuple)) else "bids_price_not_array"
    if not isinstance(asks_price, (list, tuple)) or not asks_price:
        return "asks_price_empty" if isinstance(asks_price, (list, tuple)) else "asks_price_not_array"
    if not isinstance(bids_vol, (list, tuple)) or not bids_vol:
        return "bids_vol_empty" if isinstance(bids_vol, (list, tuple)) else "bids_vol_not_array"
    if not isinstance(asks_vol, (list, tuple)) or not asks_vol:
        return "asks_vol_empty" if isinstance(asks_vol, (list, tuple)) else "asks_vol_not_array"

    if len(bids_price) != len(bids_vol):
        return "bid_book_mismatch"
    if len(asks_price) != len(asks_vol):
        return "ask_book_mismatch"

    if bids_price[0] is None or asks_price[0] is None or bids_vol[0] is None or asks_vol[0] is None:
        return "l1_none"

    return None


def enrich_tick(
    row: RowView,
    sym_state: SymbolState,
    _ch_scale: int = CH_PRICE_SCALE,
    _ch_div: int = CH_TO_PLATFORM_DIVISOR,
) -> dict[str, Any]:
    """Convert a raw CH RowView + symbol state into an alpha-ready payload.

    Mutates sym_state._payload_buf in-place and returns it.
    Safe because the caller consumes synchronously before the next row.
    Default args bound at definition time → LOAD_FAST instead of LOAD_GLOBAL.
    """
    bids_price_0 = row.bids_price[0]
    asks_price_0 = row.asks_price[0]

    # Prices in NTD (float, for display only)
    bid_px = bids_price_0 / _ch_scale
    ask_px = asks_price_0 / _ch_scale
    mid = (bid_px + ask_px) * 0.5

    bid_qty = float(row.bids_vol[0])
    ask_qty = float(row.asks_vol[0])

    # Platform-convention scaled ints (x10000)
    bid_x10k = bids_price_0 // _ch_div
    ask_x10k = asks_price_0 // _ch_div

    spread_scaled = ask_x10k - bid_x10k

    # Microprice (LOBStatsEvent convention: x2 for integer precision)
    denom = bid_qty + ask_qty
    if denom > 0:
        microprice_x2 = int((bid_x10k * ask_qty + ask_x10k * bid_qty) / denom * 2)
    else:
        microprice_x2 = bid_x10k + ask_x10k  # = mid * 2

    # Spread in bps
    spread_bps = (ask_px - bid_px) / max(mid, 1e-8) * 10000.0 if mid > 0 else 0.0

    # Imbalance
    imbalance = (bid_qty - ask_qty) / max(denom, 1.0)

    # OFI L1 (first tick: 0)
    if sym_state.tick_count == 0:
        ofi_l1_raw = 0.0
    else:
        ofi_l1_raw = (bid_qty - sym_state.prev_bid_qty) - (ask_qty - sym_state.prev_ask_qty)

    ofi_l1_cum = sym_state.ofi_l1_cum + ofi_l1_raw

    # Update symbol state (in-place mutation OK for pre-allocated state)
    sym_state.prev_bid_qty = bid_qty
    sym_state.prev_ask_qty = ask_qty
    sym_state.ofi_l1_cum = ofi_l1_cum
    sym_state.last_price = mid
    sym_state.spread_bps = spread_bps
    sym_state.bid_qty = bid_qty
    sym_state.ask_qty = ask_qty
    sym_state.tick_count += 1
    sym_state.last_update_ns = row.ingest_ts
    sym_state.cursor_ts_ns = max(sym_state.cursor_ts_ns, row.ingest_ts)

    # Mutate pre-allocated payload buffer in-place
    buf = sym_state._payload_buf
    buf["bid_px"] = bid_px
    buf["ask_px"] = ask_px
    buf["bid_qty"] = bid_qty
    buf["ask_qty"] = ask_qty
    buf["mid_price"] = mid
    buf["microprice_x2"] = microprice_x2
    buf["spread_scaled"] = spread_scaled
    buf["spread_bps"] = spread_bps
    buf["imbalance"] = imbalance
    buf["ofi_l1_raw"] = ofi_l1_raw
    buf["ofi_l1_cum"] = ofi_l1_cum
    buf["local_ts"] = row.ingest_ts
    return buf


def enrich_from_snapshot(
    slot: Any,
    sym_state: SymbolState,
    _scale: int = PLATFORM_SCALE,
) -> dict[str, Any]:
    """Convert a ShmSnapshotSlot directly into alpha payload (no CH scale conversion).

    SHM lob_fields use platform x10000 convention directly:
      [best_bid, best_ask, mid_price_x2, spread_scaled,
       bid_depth, ask_depth, l1_bid_qty, l1_ask_qty, microprice_x2]

    SHM features tuple: 16 i64 values (same as FeatureEngine output).
    """
    lob = slot.lob_fields
    best_bid = lob[0] if len(lob) > 0 else 0
    best_ask = lob[1] if len(lob) > 1 else 0
    spread_scaled = lob[3] if len(lob) > 3 else 0
    l1_bid_qty = float(lob[6]) if len(lob) > 6 else 0.0
    l1_ask_qty = float(lob[7]) if len(lob) > 7 else 0.0
    microprice_x2 = lob[8] if len(lob) > 8 else 0

    # NTD prices for display
    bid_px = best_bid / _scale
    ask_px = best_ask / _scale
    mid = (bid_px + ask_px) * 0.5

    denom = l1_bid_qty + l1_ask_qty
    spread_bps = (ask_px - bid_px) / max(mid, 1e-8) * 10000.0 if mid > 0 else 0.0
    imbalance = (l1_bid_qty - l1_ask_qty) / max(denom, 1.0)

    # OFI L1
    if sym_state.tick_count == 0:
        ofi_l1_raw = 0.0
    else:
        ofi_l1_raw = (l1_bid_qty - sym_state.prev_bid_qty) - (l1_ask_qty - sym_state.prev_ask_qty)

    ofi_l1_cum = sym_state.ofi_l1_cum + ofi_l1_raw

    # Update symbol state
    sym_state.prev_bid_qty = l1_bid_qty
    sym_state.prev_ask_qty = l1_ask_qty
    sym_state.ofi_l1_cum = ofi_l1_cum
    sym_state.last_price = mid
    sym_state.spread_bps = spread_bps
    sym_state.bid_qty = l1_bid_qty
    sym_state.ask_qty = l1_ask_qty
    sym_state.tick_count += 1
    sym_state.last_update_ns = slot.ts_ns
    sym_state.cursor_ts_ns = max(sym_state.cursor_ts_ns, slot.ts_ns)

    buf = sym_state._payload_buf
    buf["bid_px"] = bid_px
    buf["ask_px"] = ask_px
    buf["bid_qty"] = l1_bid_qty
    buf["ask_qty"] = l1_ask_qty
    buf["mid_price"] = mid
    buf["microprice_x2"] = microprice_x2
    buf["spread_scaled"] = spread_scaled
    buf["spread_bps"] = spread_bps
    buf["imbalance"] = imbalance
    buf["ofi_l1_raw"] = ofi_l1_raw
    buf["ofi_l1_cum"] = ofi_l1_cum
    buf["local_ts"] = slot.ts_ns
    return buf
