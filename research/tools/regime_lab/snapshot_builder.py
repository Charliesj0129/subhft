"""Replay an L2 event stream into periodic top-of-book snapshots.

Inputs
------
- ``data``: structured event_dtype array from a continuous shard.
- ``contract_id`` / ``is_roll_boundary``: tag arrays from the same shard.
- ``cfg.sample_period_ns``: snapshot grid (default 100 ms).

Outputs
-------
A dict-of-arrays with one row per snapshot tick on the grid:

    exch_ts_ns        i8   snapshot timestamp (grid-aligned, end-of-bucket)
    contract_id       i4   active contract id
    is_roll_boundary  bool True iff a roll boundary fell inside this bucket
    best_bid_px       f8   level-0 bid price (raw px units = TMF/TXF points)
    best_ask_px       f8   level-0 ask price
    bid_qty_l1..l5    f8   bid depth at levels 1..5
    ask_qty_l1..l5    f8   ask depth at levels 1..5
    n_trades          i4   trade events in this bucket
    trade_buy_qty     f8   sum of buy-side trade qty in this bucket
    trade_sell_qty    f8   sum of sell-side trade qty in this bucket
    min_trade_px      f8   min trade price in this bucket (NaN if no trade)
    max_trade_px      f8   max trade price in this bucket (NaN if no trade)
    n_quote_events    i4   depth events in this bucket
    bid_changes       i4   # of best-bid price changes in this bucket
    ask_changes       i4   # of best-ask price changes in this bucket

Pure-Python / numpy replay; no file I/O.

Event encoding (verified against ``research/tools/ch_batch_export.py:_export_l2_day``):
    base = ev & 0xFF
        1 = DEPTH_EVENT, 2 = TRADE_EVENT, 3 = DEPTH_CLEAR_EVENT,
        4 = DEPTH_SNAPSHOT_EVENT
    bit 28 = SELL/ASK side, bit 29 = BUY/BID side

Data format quirks (`ch_batch_export._export_l2_day`):
    * The exporter emits **full L5 re-emission** at every snapshot batch
      (every consecutive event with the same ``exch_ts`` is one batch).
    * Phantom-clear ``qty=0`` events are emitted only for prev L5 prices that
      drifted *inside* the new spread; deeper-than-L5 stale prices are left in
      place ("can't tell from L5 whether they still have quantity").
    * Therefore we MUST NOT accumulate a price-keyed book across batches —
      stale L6+ entries would never get cleaned up. Instead each batch's
      qty>0 events define the new L5 for that side.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

DEPTH_EVENT = 1
TRADE_EVENT = 2
DEPTH_CLEAR_EVENT = 3
DEPTH_SNAPSHOT_EVENT = 4

L_MAX = 5  # levels we track


@dataclass(frozen=True, slots=True)
class SnapshotConfig:
    sample_period_ns: int = 100_000_000  # 100 ms default
    drop_warmup_seconds: float = 5.0     # discard first N s of grid (book warming)


def replay_to_snapshots(
    data: np.ndarray,
    contract_id: np.ndarray,
    is_roll_boundary: np.ndarray,
    cfg: SnapshotConfig | None = None,
) -> tuple[dict[str, np.ndarray], int]:
    """Replay events into a fixed-grid snapshot table.

    Returns (cols_dict, n_snapshots). Caller can wrap into pandas without
    making this module depend on pandas.
    """
    cfg = cfg or SnapshotConfig()
    period = int(cfg.sample_period_ns)
    if data.size == 0:
        return {}, 0

    ev = data["ev"].astype(np.int64)
    base = (ev & 0xFF).astype(np.int8)
    is_bid = ((ev >> 29) & 1).astype(bool)
    is_ask = ((ev >> 28) & 1).astype(bool)
    exch_ts = data["exch_ts"].astype(np.int64)
    px = data["px"].astype(np.float64)
    qty = data["qty"].astype(np.float64)

    # Current L5 view (replaced at each batch; carried across batches if a
    # batch is single-sided).
    cur_bid_px = np.full(L_MAX, np.nan)
    cur_bid_qty = np.zeros(L_MAX)
    cur_ask_px = np.full(L_MAX, np.nan)
    cur_ask_qty = np.zeros(L_MAX)
    cur_n_bids = 0
    cur_n_asks = 0

    # Per-batch staging (events are processed in order; a batch ends when
    # exch_ts changes OR a different event type / side appears in a way that
    # contradicts the snapshot semantics — we simply use exch_ts boundary).
    batch_bid_px: list[float] = []
    batch_bid_qty: list[float] = []
    batch_ask_px: list[float] = []
    batch_ask_qty: list[float] = []
    batch_ts: int = -1
    batch_has_any_bid_event = False
    batch_has_any_ask_event = False

    t0 = int(exch_ts[0])
    grid_start = (t0 // period) * period
    last_t = int(exch_ts[-1])
    grid_end = ((last_t // period) + 1) * period
    n_grid = (grid_end - grid_start) // period
    if n_grid <= 0:
        return {}, 0

    out_ts = np.arange(grid_start, grid_end, period, dtype=np.int64)
    n = int(out_ts.size)
    out_cid = np.zeros(n, dtype=np.int32)
    out_bnd = np.zeros(n, dtype=np.bool_)
    out_bbid = np.full(n, np.nan)
    out_bask = np.full(n, np.nan)
    out_bid_qtys = np.zeros((n, L_MAX))
    out_ask_qtys = np.zeros((n, L_MAX))
    out_ntrades = np.zeros(n, dtype=np.int32)
    out_trade_buy = np.zeros(n)
    out_trade_sell = np.zeros(n)
    out_min_trade_px = np.full(n, np.nan)
    out_max_trade_px = np.full(n, np.nan)
    out_nquote = np.zeros(n, dtype=np.int32)
    out_bid_changes = np.zeros(n, dtype=np.int32)
    out_ask_changes = np.zeros(n, dtype=np.int32)

    cur_grid_idx = 0
    cur_grid_ts = grid_start + period

    n_trades_b = 0
    buy_qty_b = 0.0
    sell_qty_b = 0.0
    min_trade_px_b = np.nan
    max_trade_px_b = np.nan
    quote_b = 0
    bid_chg_b = 0
    ask_chg_b = 0
    bnd_b = False
    last_bid0 = np.nan
    last_ask0 = np.nan
    current_cid = int(contract_id[0]) if contract_id.size else 0

    def _flush_batch() -> None:
        """Promote the staged batch into the current L5 view."""
        nonlocal cur_n_bids, cur_n_asks, last_bid0, last_ask0, bid_chg_b, ask_chg_b
        if batch_has_any_bid_event:
            # Sort qty>0 bids descending, take top L_MAX
            pairs = sorted(
                ((p, q) for p, q in zip(batch_bid_px, batch_bid_qty) if q > 0.0),
                key=lambda pq: -pq[0],
            )[:L_MAX]
            cur_bid_px[:] = np.nan
            cur_bid_qty[:] = 0.0
            for i, (p, q) in enumerate(pairs):
                cur_bid_px[i] = p
                cur_bid_qty[i] = q
            cur_n_bids = len(pairs)
            new_bb = pairs[0][0] if pairs else np.nan
            if not (np.isnan(last_bid0) and np.isnan(new_bb)) and last_bid0 != new_bb:
                bid_chg_b += 1
            last_bid0 = new_bb
        if batch_has_any_ask_event:
            pairs = sorted(
                ((p, q) for p, q in zip(batch_ask_px, batch_ask_qty) if q > 0.0),
                key=lambda pq: pq[0],
            )[:L_MAX]
            cur_ask_px[:] = np.nan
            cur_ask_qty[:] = 0.0
            for i, (p, q) in enumerate(pairs):
                cur_ask_px[i] = p
                cur_ask_qty[i] = q
            cur_n_asks = len(pairs)
            new_ba = pairs[0][0] if pairs else np.nan
            if not (np.isnan(last_ask0) and np.isnan(new_ba)) and last_ask0 != new_ba:
                ask_chg_b += 1
            last_ask0 = new_ba

    def _reset_batch(new_ts: int) -> None:
        nonlocal batch_ts, batch_has_any_bid_event, batch_has_any_ask_event
        batch_bid_px.clear()
        batch_bid_qty.clear()
        batch_ask_px.clear()
        batch_ask_qty.clear()
        batch_ts = new_ts
        batch_has_any_bid_event = False
        batch_has_any_ask_event = False

    def _emit(idx: int) -> None:
        out_ts[idx] = cur_grid_ts - period
        out_cid[idx] = current_cid
        out_bnd[idx] = bnd_b
        out_bbid[idx] = cur_bid_px[0]
        out_bask[idx] = cur_ask_px[0]
        out_bid_qtys[idx] = cur_bid_qty
        out_ask_qtys[idx] = cur_ask_qty
        out_ntrades[idx] = n_trades_b
        out_trade_buy[idx] = buy_qty_b
        out_trade_sell[idx] = sell_qty_b
        out_min_trade_px[idx] = min_trade_px_b
        out_max_trade_px[idx] = max_trade_px_b
        out_nquote[idx] = quote_b
        out_bid_changes[idx] = bid_chg_b
        out_ask_changes[idx] = ask_chg_b

    for i in range(data.shape[0]):
        ts = int(exch_ts[i])

        # Close staged batch when ts changes (snapshot boundary).
        if batch_ts != -1 and ts != batch_ts:
            _flush_batch()
            _reset_batch(ts)
        elif batch_ts == -1:
            _reset_batch(ts)

        # Advance grid cursor up to ts; emit per grid cell.
        while ts >= cur_grid_ts and cur_grid_idx < n:
            _emit(cur_grid_idx)
            n_trades_b = 0
            buy_qty_b = 0.0
            sell_qty_b = 0.0
            min_trade_px_b = np.nan
            max_trade_px_b = np.nan
            quote_b = 0
            bid_chg_b = 0
            ask_chg_b = 0
            bnd_b = False
            cur_grid_idx += 1
            cur_grid_ts += period

        cid_here = int(contract_id[i])
        if cid_here != current_cid:
            current_cid = cid_here
            bnd_b = True
            cur_bid_px[:] = np.nan
            cur_bid_qty[:] = 0.0
            cur_ask_px[:] = np.nan
            cur_ask_qty[:] = 0.0
            cur_n_bids = 0
            cur_n_asks = 0
            last_bid0 = np.nan
            last_ask0 = np.nan
        if bool(is_roll_boundary[i]):
            bnd_b = True

        b = int(base[i])
        if b == DEPTH_EVENT or b == DEPTH_SNAPSHOT_EVENT:
            p = float(px[i])
            q = float(qty[i])
            if is_bid[i]:
                batch_bid_px.append(p)
                batch_bid_qty.append(q)
                batch_has_any_bid_event = True
            elif is_ask[i]:
                batch_ask_px.append(p)
                batch_ask_qty.append(q)
                batch_has_any_ask_event = True
            quote_b += 1
        elif b == DEPTH_CLEAR_EVENT:
            # Treat CLEAR as a fresh-start sentinel: reset both sides and any
            # staged batch entries on the cleared side.
            if is_bid[i] or (not is_bid[i] and not is_ask[i]):
                cur_bid_px[:] = np.nan
                cur_bid_qty[:] = 0.0
                cur_n_bids = 0
            if is_ask[i] or (not is_bid[i] and not is_ask[i]):
                cur_ask_px[:] = np.nan
                cur_ask_qty[:] = 0.0
                cur_n_asks = 0
            quote_b += 1
        elif b == TRADE_EVENT:
            q = float(qty[i])
            tp = float(px[i])
            n_trades_b += 1
            if is_bid[i]:
                buy_qty_b += q
            elif is_ask[i]:
                sell_qty_b += q
            if np.isnan(min_trade_px_b) or tp < min_trade_px_b:
                min_trade_px_b = tp
            if np.isnan(max_trade_px_b) or tp > max_trade_px_b:
                max_trade_px_b = tp

    # Final batch + grid cells.
    if batch_ts != -1:
        _flush_batch()
    while cur_grid_idx < n:
        _emit(cur_grid_idx)
        cur_grid_idx += 1
        cur_grid_ts += period

    drop_n = int(cfg.drop_warmup_seconds * 1e9 / period)
    sl = slice(drop_n, None) if 0 < drop_n < n else slice(0, None)

    cols: dict[str, np.ndarray] = {
        "exch_ts_ns": out_ts[sl],
        "contract_id": out_cid[sl],
        "is_roll_boundary": out_bnd[sl],
        "best_bid_px": out_bbid[sl],
        "best_ask_px": out_bask[sl],
        "n_trades": out_ntrades[sl],
        "trade_buy_qty": out_trade_buy[sl],
        "trade_sell_qty": out_trade_sell[sl],
        "min_trade_px": out_min_trade_px[sl],
        "max_trade_px": out_max_trade_px[sl],
        "n_quote_events": out_nquote[sl],
        "bid_changes": out_bid_changes[sl],
        "ask_changes": out_ask_changes[sl],
    }
    for lvl in range(L_MAX):
        cols[f"bid_qty_l{lvl + 1}"] = out_bid_qtys[sl, lvl]
        cols[f"ask_qty_l{lvl + 1}"] = out_ask_qtys[sl, lvl]
    return cols, int(out_ts[sl].size)
