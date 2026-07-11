"""Per-day event-clock Panel built from L2 NPZ day files (spec §7).

One row per exch_ts batch (every run of consecutive events sharing the same
``exch_ts`` is one batch), adapted from
``research/tools/regime_lab/snapshot_builder.py``:

* canonical event bits: ``base = ev & 0xFF`` (1=DEPTH, 2=TRADE, 3=CLEAR,
  4=SNAPSHOT), bit 29 = BUY/bid, bit 28 = SELL/ask — do NOT copy the swapped
  constants in ``research/t1/regime_viability.py``;
* the exporter re-emits the full L5 every batch, so each batch's ``qty>0``
  depth events REPLACE that side (never accumulate a price-keyed book);
  single-sided batches carry the other side forward; CLEAR resets;
* ``px`` is already in points (``price_scale_applied`` is recorded in the NPZ
  sidecar meta) — never divide by 1e6 again.

Columns: ``exch_ts``, ``local_ts`` (last event of the batch), L1–L5
``bid_px_i/bid_qty_i/ask_px_i/ask_qty_i``, ``mid``, ``microprice``,
``spread_ticks``, cumulative ``trade_buy_qty``/``trade_sell_qty``.

``dir_coverage`` comes from ClickHouse ``hft.market_data.trade_direction``
(the NPZ trade side bits are tick-rule inferred and must NOT be used as the
dir_clean basis).  Days where ClickHouse has no answer are fail-closed to
``dir_coverage = 0.0`` (trade_imbalance candidates lose the day).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np

PANEL_VERSION = "panel_v1"
DIR_CLEAN_THRESHOLD = 0.95

DEPTH_EVENT = 1
TRADE_EVENT = 2
DEPTH_CLEAR_EVENT = 3
DEPTH_SNAPSHOT_EVENT = 4
L_MAX = 5


@dataclass(frozen=True)
class Panel:
    columns: dict[str, np.ndarray]
    meta: dict[str, Any]

    @property
    def n_rows(self) -> int:
        return int(self.columns["exch_ts"].size) if self.columns else 0


def load_l2_events(npz_path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    """Load the structured event array + sidecar meta (``<npz>.meta.json``)."""
    with np.load(npz_path) as archive:
        data = archive["data"]
    sidecar = Path(str(npz_path) + ".meta.json")
    meta: dict[str, Any] = {}
    if sidecar.exists():
        meta = json.loads(sidecar.read_text())
    return data, meta


def replay_to_panel(data: np.ndarray, tick_size: float) -> dict[str, np.ndarray]:
    """Replay L2 events into one panel row per exch_ts batch. Pure numpy/python."""
    if data.size == 0:
        return {}

    ev = data["ev"].astype(np.int64)
    # .tolist() conversions: python-list scalar access is much faster than
    # np scalar indexing inside the per-event loop.
    base = cast("list[int]", (ev & 0xFF).astype(np.int8).tolist())
    is_bid = cast("list[bool]", (((ev >> 29) & 1) == 1).tolist())
    is_ask = cast("list[bool]", (((ev >> 28) & 1) == 1).tolist())
    exch_ts = data["exch_ts"].astype(np.int64)
    local_ts = cast("list[int]", data["local_ts"].astype(np.int64).tolist())
    px = cast("list[float]", data["px"].astype(np.float64).tolist())
    qty = cast("list[float]", data["qty"].astype(np.float64).tolist())
    exch_ts_list = cast("list[int]", exch_ts.tolist())
    n_events = len(exch_ts_list)

    # Batch starts: index 0 plus every exch_ts change.
    change = np.flatnonzero(np.diff(exch_ts) != 0) + 1
    starts = np.concatenate(([0], change))
    n_batches = int(starts.size)
    ends = np.concatenate((starts[1:], [n_events]))

    out_exch = np.empty(n_batches, dtype=np.int64)
    out_local = np.empty(n_batches, dtype=np.int64)
    out_bid_px = np.full((n_batches, L_MAX), np.nan)
    out_bid_qty = np.zeros((n_batches, L_MAX))
    out_ask_px = np.full((n_batches, L_MAX), np.nan)
    out_ask_qty = np.zeros((n_batches, L_MAX))
    out_buy = np.empty(n_batches)
    out_sell = np.empty(n_batches)

    cur_bid_px = [float("nan")] * L_MAX
    cur_bid_qty = [0.0] * L_MAX
    cur_ask_px = [float("nan")] * L_MAX
    cur_ask_qty = [0.0] * L_MAX
    cum_buy = 0.0
    cum_sell = 0.0

    for k in range(n_batches):
        s = int(starts[k])
        e = int(ends[k])
        batch_bid: list[tuple[float, float]] = []
        batch_ask: list[tuple[float, float]] = []
        has_bid = False
        has_ask = False
        for i in range(s, e):
            b = base[i]
            if b == DEPTH_EVENT or b == DEPTH_SNAPSHOT_EVENT:
                if is_bid[i]:
                    batch_bid.append((px[i], qty[i]))
                    has_bid = True
                elif is_ask[i]:
                    batch_ask.append((px[i], qty[i]))
                    has_ask = True
            elif b == TRADE_EVENT:
                if is_bid[i]:
                    cum_buy += qty[i]
                elif is_ask[i]:
                    cum_sell += qty[i]
            elif b == DEPTH_CLEAR_EVENT:
                both = not is_bid[i] and not is_ask[i]
                if is_bid[i] or both:
                    cur_bid_px = [float("nan")] * L_MAX
                    cur_bid_qty = [0.0] * L_MAX
                if is_ask[i] or both:
                    cur_ask_px = [float("nan")] * L_MAX
                    cur_ask_qty = [0.0] * L_MAX

        if has_bid:
            pairs = sorted(((p, q) for p, q in batch_bid if q > 0.0), key=lambda pq: -pq[0])[:L_MAX]
            cur_bid_px = [p for p, _ in pairs] + [float("nan")] * (L_MAX - len(pairs))
            cur_bid_qty = [q for _, q in pairs] + [0.0] * (L_MAX - len(pairs))
        if has_ask:
            pairs = sorted(((p, q) for p, q in batch_ask if q > 0.0), key=lambda pq: pq[0])[:L_MAX]
            cur_ask_px = [p for p, _ in pairs] + [float("nan")] * (L_MAX - len(pairs))
            cur_ask_qty = [q for _, q in pairs] + [0.0] * (L_MAX - len(pairs))

        out_exch[k] = exch_ts_list[s]
        out_local[k] = local_ts[e - 1]
        out_bid_px[k] = cur_bid_px
        out_bid_qty[k] = cur_bid_qty
        out_ask_px[k] = cur_ask_px
        out_ask_qty[k] = cur_ask_qty
        out_buy[k] = cum_buy
        out_sell[k] = cum_sell

    bid1_px = out_bid_px[:, 0]
    ask1_px = out_ask_px[:, 0]
    bid1_qty = out_bid_qty[:, 0]
    ask1_qty = out_ask_qty[:, 0]
    mid = (bid1_px + ask1_px) / 2.0
    with np.errstate(invalid="ignore", divide="ignore"):
        denom = bid1_qty + ask1_qty
        microprice = np.where(denom > 0.0, (bid1_px * ask1_qty + ask1_px * bid1_qty) / denom, np.nan)
        spread_ticks = (ask1_px - bid1_px) / float(tick_size)

    cols: dict[str, np.ndarray] = {
        "exch_ts": out_exch,
        "local_ts": out_local,
        "mid": mid,
        "microprice": microprice,
        "spread_ticks": spread_ticks,
        "trade_buy_qty": out_buy,
        "trade_sell_qty": out_sell,
    }
    for lvl in range(L_MAX):
        cols[f"bid_px_{lvl + 1}"] = out_bid_px[:, lvl]
        cols[f"bid_qty_{lvl + 1}"] = out_bid_qty[:, lvl]
        cols[f"ask_px_{lvl + 1}"] = out_ask_px[:, lvl]
        cols[f"ask_qty_{lvl + 1}"] = out_ask_qty[:, lvl]
    return cols


def fetch_dir_coverage(ch_client: Any, symbol: str, day: str) -> tuple[float, str]:
    """Fraction of that day's trades with ``trade_direction != 0`` in ClickHouse.

    Fail-closed: any error or a day with no trade rows returns
    ``(0.0, <source>)`` so trade_imbalance candidates cannot silently use
    tick-rule directions.
    """
    sql = (
        "SELECT countIf(trade_direction != 0), count() FROM hft.market_data "
        "WHERE symbol = %(symbol)s AND type = 'Tick' "
        "AND toDate(fromUnixTimestamp64Nano(exch_ts)) = %(day)s"
    )
    try:
        result = ch_client.query(sql, parameters={"symbol": symbol, "day": day})
        rows = result.result_rows
    except Exception as exc:  # noqa: BLE001 - fail-closed on any CH failure
        return 0.0, f"ch_error:{type(exc).__name__}"
    if not rows or int(rows[0][1]) == 0:
        return 0.0, "ch_no_trades"
    classified, total = int(rows[0][0]), int(rows[0][1])
    return classified / total, "ch"


def build_panel(
    npz_path: Path,
    symbol: str,
    day: str,
    tick_size: float,
    cache_dir: Path,
    dir_coverage: float | None = None,
    dir_coverage_source: str = "not_queried",
) -> Panel:
    """Build (or load from cache) the panel for one (symbol, day).

    Cache key = NPZ ``data_fingerprint`` + ``PANEL_VERSION``; a fingerprint or
    version mismatch rebuilds.  ``dir_coverage`` is stored in the panel meta;
    passing a fresh value on a cache hit updates the cached meta in place.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_npz = cache_dir / f"{symbol}_{day}.panel.npz"
    cache_meta_path = cache_dir / f"{symbol}_{day}.panel.meta.json"

    _, source_meta = load_l2_events_meta_only(npz_path)
    fingerprint = str(source_meta.get("data_fingerprint", ""))

    if cache_npz.exists() and cache_meta_path.exists():
        meta = json.loads(cache_meta_path.read_text())
        if meta.get("panel_version") == PANEL_VERSION and meta.get("data_fingerprint") == fingerprint:
            if dir_coverage is not None and meta.get("dir_coverage") != dir_coverage:
                meta["dir_coverage"] = dir_coverage
                meta["dir_coverage_source"] = dir_coverage_source
                meta["dir_clean"] = dir_coverage >= DIR_CLEAN_THRESHOLD
                cache_meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
            with np.load(cache_npz) as archive:
                cols = {name: archive[name] for name in archive.files}
            return Panel(columns=cols, meta=meta)

    data, _ = load_l2_events(npz_path)
    cols = replay_to_panel(data, tick_size)
    exch = data["exch_ts"].astype(np.int64)
    local = data["local_ts"].astype(np.int64)
    coverage = 0.0 if dir_coverage is None else float(dir_coverage)
    meta = {
        "panel_version": PANEL_VERSION,
        "symbol": symbol,
        "day": day,
        "tick_size": float(tick_size),
        "n_rows": int(cols["exch_ts"].size) if cols else 0,
        "n_events": int(data.size),
        "data_fingerprint": fingerprint,
        "source_npz": str(npz_path),
        "source_generator": str(source_meta.get("generator", "")),
        # Jan-era exports carry local_ts==exch_ts (no real latency info);
        # latency-shift realism per day depends on this fraction.
        "local_ts_equals_exch_ts_fraction": float(np.mean(local == exch)) if data.size else 1.0,
        "dir_coverage": coverage,
        "dir_coverage_source": dir_coverage_source if dir_coverage is not None else "not_queried",
        "dir_clean": coverage >= DIR_CLEAN_THRESHOLD,
        "dir_clean_threshold": DIR_CLEAN_THRESHOLD,
    }
    np.savez_compressed(cache_npz, **cols)  # type: ignore[arg-type]  # numpy stub lacks **kwds overload
    cache_meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return Panel(columns=cols, meta=meta)


def load_l2_events_meta_only(npz_path: Path) -> tuple[None, dict[str, Any]]:
    """Sidecar meta without loading the (large) event array."""
    sidecar = Path(str(npz_path) + ".meta.json")
    meta: dict[str, Any] = {}
    if sidecar.exists():
        meta = json.loads(sidecar.read_text())
    return None, meta


__all__ = [
    "DEPTH_CLEAR_EVENT",
    "DEPTH_EVENT",
    "DEPTH_SNAPSHOT_EVENT",
    "DIR_CLEAN_THRESHOLD",
    "L_MAX",
    "PANEL_VERSION",
    "Panel",
    "TRADE_EVENT",
    "build_panel",
    "fetch_dir_coverage",
    "load_l2_events",
    "replay_to_panel",
]
