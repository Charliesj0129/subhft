"""T1 TXF higher-timeframe regime viability audit.

This module intentionally keeps L2 out of alpha generation.  TXF BBO/trades are
collapsed into higher-timeframe price bars and trade VWAP; TMF BBO is used only
for executable entry/exit evaluation.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time as time_module
from dataclasses import dataclass
from datetime import UTC, datetime, time, timezone
from pathlib import Path
from statistics import median
from typing import Iterable, Sequence

import numpy as np

TRADE_EVENT = 0x1
DEPTH_EVENT = 0x2
DEPTH_SNAPSHOT_EVENT = 0x4
SIDE_MASK = 0xF0000000
BID_SIDE = 0xE0000000
ASK_SIDE = 0xD0000000
NS_PER_SECOND = 1_000_000_000
NS_PER_MINUTE = 60 * NS_PER_SECOND


@dataclass(frozen=True)
class BboFrame:
    ts_ns: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    bid_qty: np.ndarray
    ask_qty: np.ndarray
    mid: np.ndarray


@dataclass(frozen=True)
class TradeFrame:
    ts_ns: np.ndarray
    price: np.ndarray
    qty: np.ndarray


@dataclass(frozen=True)
class TimeBar:
    start_ns: int
    end_ns: int
    open: float
    high: float
    low: float
    close: float
    n_quotes: int


@dataclass(frozen=True)
class OpeningRangeConfig:
    session_start_ns: int
    opening_minutes: int = 30
    confirm_minutes: int = 30
    min_break_points: float = 8.0
    min_rv_ratio: float = 1.25


@dataclass(frozen=True)
class RegimeEvent:
    contract: str
    date: str
    regime_type: str
    trigger_time: str
    trigger_time_ns: int
    direction: int
    txf_entry_ref: float
    opening_range_high: float
    opening_range_low: float
    trade_vwap: float | None
    realized_vol_ratio: float


def load_hftbt_npz(path: str | Path) -> np.ndarray:
    loaded = np.load(Path(path), allow_pickle=False)
    try:
        if "data" not in loaded:
            raise ValueError(f"{path} does not contain a 'data' array")
        return np.asarray(loaded["data"])
    finally:
        loaded.close()


def extract_bbo_and_trades(events: np.ndarray) -> tuple[BboFrame, TradeFrame]:
    ev = events["ev"].astype(np.uint64, copy=False)
    low = ev & np.uint64(0xFF)
    side = ev & np.uint64(SIDE_MASK)
    qty_all = events["qty"].astype(np.float64, copy=False)
    px_all = events["px"].astype(np.float64, copy=False)
    ts_all = events["exch_ts"].astype(np.int64, copy=False)

    trade_mask = (low & np.uint64(TRADE_EVENT) != 0) & (px_all > 0) & (qty_all > 0)
    trade_ts = ts_all[trade_mask].astype(np.int64, copy=True)
    trade_px = px_all[trade_mask].astype(np.float64, copy=True)
    trade_qty = qty_all[trade_mask].astype(np.float64, copy=True)

    depth_mask = (
        ((low & np.uint64(DEPTH_EVENT)) != 0)
        | ((low & np.uint64(DEPTH_SNAPSHOT_EVENT)) != 0)
    ) & (px_all > 0) & (qty_all > 0)
    bid_mask = depth_mask & (side == np.uint64(BID_SIDE))
    ask_mask = depth_mask & (side == np.uint64(ASK_SIDE))

    bid_ts, bid_px = _reduce_depth_side(ts_all[bid_mask], px_all[bid_mask], is_bid=True)
    ask_ts, ask_px = _reduce_depth_side(ts_all[ask_mask], px_all[ask_mask], is_bid=False)

    if len(bid_ts) and len(ask_ts):
        ts_ns = np.union1d(bid_ts, ask_ts)
        bid_idx = np.searchsorted(bid_ts, ts_ns, side="right") - 1
        ask_idx = np.searchsorted(ask_ts, ts_ns, side="right") - 1
        valid_idx = (bid_idx >= 0) & (ask_idx >= 0)
        ts_ns = ts_ns[valid_idx]
        bid = bid_px[bid_idx[valid_idx]]
        ask = ask_px[ask_idx[valid_idx]]
        valid = (bid > 0) & (ask > 0) & (bid < ask)
        ts_ns = ts_ns[valid].astype(np.int64, copy=False)
        bid = bid[valid].astype(np.float64, copy=False)
        ask = ask[valid].astype(np.float64, copy=False)
        bid_qty = np.zeros(len(ts_ns), dtype=np.float64)
        ask_qty = np.zeros(len(ts_ns), dtype=np.float64)
        mid = (bid + ask) / 2.0
    else:
        ts_ns = np.asarray([], dtype=np.int64)
        bid = ask = bid_qty = ask_qty = mid = np.asarray([], dtype=np.float64)

    return (
        BboFrame(ts_ns=ts_ns, bid=bid, ask=ask, bid_qty=bid_qty, ask_qty=ask_qty, mid=mid),
        TradeFrame(ts_ns=trade_ts, price=trade_px, qty=trade_qty),
    )


def _reduce_depth_side(ts: np.ndarray, px: np.ndarray, *, is_bid: bool) -> tuple[np.ndarray, np.ndarray]:
    if len(ts) == 0:
        return np.asarray([], dtype=np.int64), np.asarray([], dtype=np.float64)
    unique_ts, starts = np.unique(ts, return_index=True)
    reduced = np.maximum.reduceat(px, starts) if is_bid else np.minimum.reduceat(px, starts)
    return unique_ts.astype(np.int64, copy=False), reduced.astype(np.float64, copy=False)


def make_time_bars(bbo: BboFrame, *, interval_minutes: int) -> list[TimeBar]:
    if len(bbo.ts_ns) == 0:
        return []
    interval_ns = interval_minutes * NS_PER_MINUTE
    first_start = (int(bbo.ts_ns[0]) // interval_ns) * interval_ns
    bucket = ((bbo.ts_ns - first_start) // interval_ns).astype(np.int64)
    bars: list[TimeBar] = []
    for bucket_id in np.unique(bucket):
        idx = np.flatnonzero(bucket == bucket_id)
        mids = bbo.mid[idx]
        start_ns = first_start + int(bucket_id) * interval_ns
        bars.append(
            TimeBar(
                start_ns=start_ns,
                end_ns=start_ns + interval_ns,
                open=float(mids[0]),
                high=float(np.max(mids)),
                low=float(np.min(mids)),
                close=float(mids[-1]),
                n_quotes=int(len(idx)),
            )
        )
    return bars


def _realized_vol(values: np.ndarray) -> float:
    if len(values) < 3:
        return 0.0
    diffs = np.diff(values)
    return float(np.sqrt(np.mean(diffs * diffs)))


def _trade_vwap_until(trades: TradeFrame, end_ns: int) -> float | None:
    mask = trades.ts_ns <= end_ns
    if not np.any(mask):
        return None
    qty = trades.qty[mask]
    total_qty = float(np.sum(qty))
    if total_qty <= 0:
        return None
    return float(np.sum(trades.price[mask] * qty) / total_qty)


def _iso_from_ns(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / NS_PER_SECOND, tz=UTC).isoformat()


def _seconds_between(start_ns: int, end_ns: int) -> int:
    return int(max(0, end_ns - start_ns) / NS_PER_SECOND)


def _duration_where(ts_ns: np.ndarray, condition: np.ndarray, *, end_ns: int) -> int:
    if len(ts_ns) == 0 or len(condition) == 0:
        return 0
    total = 0
    for idx, ok in enumerate(condition):
        if not bool(ok):
            continue
        cur = int(ts_ns[idx])
        nxt = int(ts_ns[idx + 1]) if idx + 1 < len(ts_ns) else end_ns
        total += _seconds_between(cur, nxt)
    return total


def detect_opening_range_events(
    bbo: BboFrame,
    trades: TradeFrame,
    *,
    contract: str,
    date: str,
    config: OpeningRangeConfig,
) -> list[RegimeEvent]:
    start_ns = config.session_start_ns
    open_end = start_ns + config.opening_minutes * NS_PER_MINUTE
    confirm_end = open_end + config.confirm_minutes * NS_PER_MINUTE

    opening = (bbo.ts_ns >= start_ns) & (bbo.ts_ns < open_end)
    confirm = (bbo.ts_ns >= open_end) & (bbo.ts_ns <= confirm_end)
    if not np.any(opening) or not np.any(confirm):
        return []

    opening_mid = bbo.mid[opening]
    opening_high = float(np.max(opening_mid))
    opening_low = float(np.min(opening_mid))
    opening_rv = _realized_vol(opening_mid)
    if opening_rv <= 0:
        return []

    confirm_idx = np.flatnonzero(confirm)
    confirm_mid = bbo.mid[confirm_idx]
    confirm_rv = _realized_vol(confirm_mid)
    rv_ratio = confirm_rv / opening_rv if opening_rv > 0 else 0.0
    if rv_ratio < config.min_rv_ratio:
        return []

    long_break = confirm_mid >= opening_high + config.min_break_points
    short_break = confirm_mid <= opening_low - config.min_break_points
    if np.any(long_break):
        local_idx = int(np.flatnonzero(long_break)[0])
        direction = 1
    elif np.any(short_break):
        local_idx = int(np.flatnonzero(short_break)[0])
        direction = -1
    else:
        return []

    global_idx = int(confirm_idx[local_idx])
    trigger_ns = int(bbo.ts_ns[global_idx])
    entry_ref = float(bbo.mid[global_idx])
    vwap = _trade_vwap_until(trades, trigger_ns)
    if vwap is not None:
        if direction == 1 and entry_ref <= vwap:
            return []
        if direction == -1 and entry_ref >= vwap:
            return []

    return [
        RegimeEvent(
            contract=contract,
            date=date,
            regime_type="T1-A_opening_range_expansion",
            trigger_time=_iso_from_ns(trigger_ns),
            trigger_time_ns=trigger_ns,
            direction=direction,
            txf_entry_ref=entry_ref,
            opening_range_high=opening_high,
            opening_range_low=opening_low,
            trade_vwap=vwap,
            realized_vol_ratio=float(rv_ratio),
        )
    ]


def coverage_audit_opening_range(
    bbo: BboFrame,
    trades: TradeFrame,
    *,
    contract: str,
    trading_day: str,
    pair_id: str,
    config: OpeningRangeConfig,
    persistence_minutes: int = 5,
) -> dict[str, object]:
    start_ns = config.session_start_ns
    or_end_ns = start_ns + config.opening_minutes * NS_PER_MINUTE
    post_end_ns = or_end_ns + config.confirm_minutes * NS_PER_MINUTE

    opening = (bbo.ts_ns >= start_ns) & (bbo.ts_ns < or_end_ns)
    post = (bbo.ts_ns >= or_end_ns) & (bbo.ts_ns <= post_end_ns)
    row_base: dict[str, object] = {
        "contract": contract,
        "trading_day": trading_day,
        "pair_id": pair_id,
        "or_start": _iso_from_ns(start_ns),
        "or_end": _iso_from_ns(or_end_ns),
        "bbo_first_time": _iso_from_ns(int(bbo.ts_ns[0])) if len(bbo.ts_ns) else None,
        "bbo_last_time": _iso_from_ns(int(bbo.ts_ns[-1])) if len(bbo.ts_ns) else None,
        "event_selected_by_v0": False,
    }
    if not np.any(opening):
        return {
            **row_base,
            "coverage_status": "missing_opening",
            "or_high": None,
            "or_low": None,
            "or_width": None,
            "post_or_high": None,
            "post_or_low": None,
            "max_upside_break_pts": None,
            "max_downside_break_pts": None,
            "first_up_break_time": None,
            "first_down_break_time": None,
            "break_side": "none",
            "break_magnitude_pts": 0.0,
            "break_magnitude_vs_or_width": None,
            "break_magnitude_vs_prior_realized_vol": None,
            "vwap_side_at_break": "not_evaluated",
            "reverted_to_or": False,
            "time_above_or_high": 0,
            "time_below_or_low": 0,
            "persistent_after_break": False,
            "realized_vol_ratio": None,
        }

    opening_mid = bbo.mid[opening]
    or_high = float(np.max(opening_mid))
    or_low = float(np.min(opening_mid))
    or_width = or_high - or_low
    prior_rv = _realized_vol(opening_mid)
    if not np.any(post):
        return {
            **row_base,
            "coverage_status": "missing_post",
            "or_high": or_high,
            "or_low": or_low,
            "or_width": or_width,
            "post_or_high": None,
            "post_or_low": None,
            "max_upside_break_pts": None,
            "max_downside_break_pts": None,
            "first_up_break_time": None,
            "first_down_break_time": None,
            "break_side": "none",
            "break_magnitude_pts": 0.0,
            "break_magnitude_vs_or_width": None,
            "break_magnitude_vs_prior_realized_vol": None,
            "vwap_side_at_break": "not_evaluated",
            "reverted_to_or": False,
            "time_above_or_high": 0,
            "time_below_or_low": 0,
            "persistent_after_break": False,
            "realized_vol_ratio": None,
        }

    post_idx = np.flatnonzero(post)
    post_mid = bbo.mid[post_idx]
    post_ts = bbo.ts_ns[post_idx]
    post_high = float(np.max(post_mid))
    post_low = float(np.min(post_mid))
    max_up = max(0.0, post_high - or_high)
    max_down = max(0.0, or_low - post_low)

    up_break = post_mid > or_high
    down_break = post_mid < or_low
    first_up_ns = int(post_ts[int(np.flatnonzero(up_break)[0])]) if np.any(up_break) else None
    first_down_ns = int(post_ts[int(np.flatnonzero(down_break)[0])]) if np.any(down_break) else None

    if first_up_ns is not None and (first_down_ns is None or first_up_ns <= first_down_ns):
        break_side = "up"
        break_ns = first_up_ns
        break_mid = float(post_mid[int(np.searchsorted(post_ts, break_ns, side="left"))])
        break_magnitude = break_mid - or_high
        reverted = bool(np.any(post_mid[post_ts >= break_ns] <= or_high))
    elif first_down_ns is not None:
        break_side = "down"
        break_ns = first_down_ns
        break_mid = float(post_mid[int(np.searchsorted(post_ts, break_ns, side="left"))])
        break_magnitude = or_low - break_mid
        reverted = bool(np.any(post_mid[post_ts >= break_ns] >= or_low))
    else:
        break_side = "none"
        break_ns = None
        break_mid = None
        break_magnitude = 0.0
        reverted = False

    vwap = _trade_vwap_until(trades, break_ns) if break_ns is not None else None
    if vwap is None or break_mid is None:
        vwap_side = "not_evaluated"
    elif break_mid > vwap:
        vwap_side = "above"
    elif break_mid < vwap:
        vwap_side = "below"
    else:
        vwap_side = "at"

    persistence_ns = persistence_minutes * NS_PER_MINUTE
    if break_side == "up" and break_ns is not None:
        persistence_window = (post_ts >= break_ns) & (post_ts <= break_ns + persistence_ns)
        persistent = bool(np.all(post_mid[persistence_window] > or_high)) if np.any(persistence_window) else False
    elif break_side == "down" and break_ns is not None:
        persistence_window = (post_ts >= break_ns) & (post_ts <= break_ns + persistence_ns)
        persistent = bool(np.all(post_mid[persistence_window] < or_low)) if np.any(persistence_window) else False
    else:
        persistent = False

    rv_ratio = _realized_vol(post_mid) / prior_rv if prior_rv > 0 else None
    event_selected = bool(
        detect_opening_range_events(
            bbo,
            trades,
            contract=contract,
            date=trading_day,
            config=config,
        )
    )

    return {
        **row_base,
        "coverage_status": "ok",
        "or_high": or_high,
        "or_low": or_low,
        "or_width": or_width,
        "post_or_high": post_high,
        "post_or_low": post_low,
        "max_upside_break_pts": max_up,
        "max_downside_break_pts": max_down,
        "first_up_break_time": _iso_from_ns(first_up_ns) if first_up_ns is not None else None,
        "first_down_break_time": _iso_from_ns(first_down_ns) if first_down_ns is not None else None,
        "break_side": break_side,
        "break_magnitude_pts": float(break_magnitude),
        "break_magnitude_vs_or_width": float(break_magnitude / or_width) if or_width > 0 else None,
        "break_magnitude_vs_prior_realized_vol": float(break_magnitude / prior_rv) if prior_rv > 0 else None,
        "vwap_side_at_break": vwap_side,
        "reverted_to_or": reverted,
        "time_above_or_high": _duration_where(post_ts, post_mid > or_high, end_ns=post_end_ns),
        "time_below_or_low": _duration_where(post_ts, post_mid < or_low, end_ns=post_end_ns),
        "event_selected_by_v0": event_selected,
        "persistent_after_break": persistent,
        "realized_vol_ratio": rv_ratio,
    }


def evaluate_executable_returns(
    tmf_bbo: BboFrame,
    *,
    trigger_time_ns: int,
    direction: int,
    horizons_minutes: Sequence[int] = (15, 30, 60),
) -> dict[str, float | int | None]:
    entry_idx = int(np.searchsorted(tmf_bbo.ts_ns, trigger_time_ns, side="left"))
    if entry_idx >= len(tmf_bbo.ts_ns):
        raise ValueError("no TMF executable quote at or after trigger")

    entry = float(tmf_bbo.ask[entry_idx] if direction > 0 else tmf_bbo.bid[entry_idx])
    path_start_ns = int(tmf_bbo.ts_ns[entry_idx])
    result: dict[str, float | int | None] = {"tmf_executable_entry": entry}

    full_last = entry_idx
    for minutes in horizons_minutes:
        horizon_ns = trigger_time_ns + minutes * NS_PER_MINUTE
        end = int(np.searchsorted(tmf_bbo.ts_ns, horizon_ns, side="right"))
        if end <= entry_idx:
            result[f"mfe_{minutes}m"] = None
            result[f"mae_{minutes}m"] = None
            result[f"return_{minutes}m"] = None
            continue
        full_last = max(full_last, end - 1)
        exit_path = tmf_bbo.bid[entry_idx:end] if direction > 0 else tmf_bbo.ask[entry_idx:end]
        pnl_path = (exit_path - entry) * direction
        result[f"mfe_{minutes}m"] = float(np.max(pnl_path))
        result[f"mae_{minutes}m"] = float(np.min(pnl_path))
        result[f"return_{minutes}m"] = float(pnl_path[-1])

    if full_last >= entry_idx:
        exit_path = tmf_bbo.bid[entry_idx : full_last + 1] if direction > 0 else tmf_bbo.ask[entry_idx : full_last + 1]
        pnl_path = (exit_path - entry) * direction
        mfe_idx = int(np.argmax(pnl_path))
        mae_idx = int(np.argmin(pnl_path))
        result["time_to_mfe"] = int((int(tmf_bbo.ts_ns[entry_idx + mfe_idx]) - path_start_ns) / NS_PER_SECOND)
        result["time_to_mae"] = int((int(tmf_bbo.ts_ns[entry_idx + mae_idx]) - path_start_ns) / NS_PER_SECOND)
    else:
        result["time_to_mfe"] = None
        result["time_to_mae"] = None

    return result


def _session_start_ns(date: str, *, hour: int = 8, minute: int = 45, tz_offset_hours: int = 8) -> int:
    tz = timezone.utc if tz_offset_hours == 0 else timezone.utc
    local_dt = datetime.combine(datetime.fromisoformat(date).date(), time(hour, minute), tzinfo=tz)
    utc_ts = local_dt.timestamp() - tz_offset_hours * 3600
    return int(utc_ts * NS_PER_SECOND)


def _date_from_path(path: Path) -> str:
    parts = path.name.split("_")
    if len(parts) < 2:
        raise ValueError(f"cannot parse date from {path.name}")
    return parts[1]


def _load_frames(path: Path) -> tuple[BboFrame, TradeFrame]:
    return extract_bbo_and_trades(load_hftbt_npz(path))


def audit_opening_range_pair(
    *,
    txf_path: Path,
    tmf_path: Path,
    session_tz_offset_hours: int = 8,
    opening_minutes: int = 30,
    confirm_minutes: int = 30,
    min_break_points: float = 8.0,
    min_rv_ratio: float = 1.25,
) -> list[dict[str, object]]:
    date = _date_from_path(txf_path)
    txf_contract = txf_path.name.split("_", 1)[0]
    tmf_contract = tmf_path.name.split("_", 1)[0]
    txf_bbo, txf_trades = _load_frames(txf_path)
    tmf_bbo, _ = _load_frames(tmf_path)
    config = OpeningRangeConfig(
        session_start_ns=_session_start_ns(date, tz_offset_hours=session_tz_offset_hours),
        opening_minutes=opening_minutes,
        confirm_minutes=confirm_minutes,
        min_break_points=min_break_points,
        min_rv_ratio=min_rv_ratio,
    )
    rows: list[dict[str, object]] = []
    for event in detect_opening_range_events(
        txf_bbo,
        txf_trades,
        contract=txf_contract,
        date=date,
        config=config,
    ):
        eval_row = evaluate_executable_returns(
            tmf_bbo,
            trigger_time_ns=event.trigger_time_ns,
            direction=event.direction,
        )
        after = txf_bbo.ts_ns >= event.trigger_time_ns
        post_mid = txf_bbo.mid[after]
        if event.direction > 0:
            reverted = bool(np.any(post_mid <= event.opening_range_high)) if len(post_mid) else False
        else:
            reverted = bool(np.any(post_mid >= event.opening_range_low)) if len(post_mid) else False
        row = {
            "contract": f"{txf_contract}->{tmf_contract}",
            "date": date,
            "regime_type": event.regime_type,
            "trigger_time": event.trigger_time,
            "direction": event.direction,
            "txf_entry_ref": event.txf_entry_ref,
            "opening_range_high": event.opening_range_high,
            "opening_range_low": event.opening_range_low,
            "trade_vwap": event.trade_vwap,
            "realized_vol_ratio": event.realized_vol_ratio,
            "stop_structure_breached": reverted,
            "reverted_to_range": reverted,
            "vwap_reclaim_failed_or_passed": "passed" if event.trade_vwap is not None else "not_evaluated",
            "net_30m_pts": eval_row.get("return_30m"),
            **eval_row,
        }
        rows.append(row)
    return rows


def coverage_audit_opening_range_pair(
    *,
    txf_path: Path,
    tmf_path: Path,
    session_tz_offset_hours: int = 8,
    opening_minutes: int = 30,
    confirm_minutes: int = 30,
    min_break_points: float = 8.0,
    min_rv_ratio: float = 1.25,
    persistence_minutes: int = 5,
) -> dict[str, object]:
    date = _date_from_path(txf_path)
    txf_contract = txf_path.name.split("_", 1)[0]
    tmf_contract = tmf_path.name.split("_", 1)[0]
    txf_bbo, txf_trades = _load_frames(txf_path)
    config = OpeningRangeConfig(
        session_start_ns=_session_start_ns(date, tz_offset_hours=session_tz_offset_hours),
        opening_minutes=opening_minutes,
        confirm_minutes=confirm_minutes,
        min_break_points=min_break_points,
        min_rv_ratio=min_rv_ratio,
    )
    return coverage_audit_opening_range(
        txf_bbo,
        txf_trades,
        contract=txf_contract,
        trading_day=date,
        pair_id=f"{txf_contract}->{tmf_contract}",
        config=config,
        persistence_minutes=persistence_minutes,
    )


def _matching_pairs(raw_dir: Path, months: Iterable[str]) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for month in months:
        txf = f"TXF{month}"
        tmf = f"TMF{month}"
        txf_files = { _date_from_path(p): p for p in sorted((raw_dir / txf.lower()).glob(f"{txf}_*_l2.hftbt.npz")) }
        tmf_files = { _date_from_path(p): p for p in sorted((raw_dir / tmf.lower()).glob(f"{tmf}_*_l2.hftbt.npz")) }
        for date in sorted(set(txf_files) & set(tmf_files)):
            pairs.append((txf_files[date], tmf_files[date]))
    return pairs


def summarize_rows(
    rows: Sequence[dict[str, object]],
    *,
    audited_dates: Sequence[str] | None = None,
) -> dict[str, object]:
    returns = [float(r["return_30m"]) for r in rows if r.get("return_30m") is not None]
    contracts = sorted({str(r["contract"]).split("->", 1)[0] for r in rows})
    event_dates = sorted({str(r["date"]) for r in rows})
    audited_unique_dates = sorted(set(audited_dates or event_dates))
    stop_breaches = sum(1 for r in rows if r.get("stop_structure_breached"))
    best_removed = sorted(returns)[:-1] if len(returns) > 1 else returns
    return {
        "track": "T1: TXF Higher-Timeframe Regime -> TMF Expression",
        "candidate": "T1_regime_viability_audit_v0",
        "events": len(rows),
        "audited_trading_days": len(audited_unique_dates),
        "event_trading_days": len(event_dates),
        "contracts": contracts,
        "median_return_30m": median(returns) if returns else None,
        "p10_return_30m": float(np.percentile(returns, 10)) if returns else None,
        "remove_best_1_median_return_30m": median(best_removed) if best_removed else None,
        "stop_breach_rate": stop_breaches / len(rows) if rows else None,
    }


def summarize_coverage_rows(rows: Sequence[dict[str, object]]) -> dict[str, object]:
    break_rows = [r for r in rows if r.get("break_side") in {"up", "down"}]
    selected_rows = [r for r in rows if r.get("event_selected_by_v0")]
    by_contract: dict[str, dict[str, int]] = {}
    coverage_status: dict[str, int] = {}
    for row in rows:
        contract = str(row["contract"])
        side = str(row.get("break_side", "none"))
        status = str(row.get("coverage_status", "unknown"))
        coverage_status[status] = coverage_status.get(status, 0) + 1
        bucket = by_contract.setdefault(contract, {"days": 0, "breaks": 0, "up": 0, "down": 0, "v0_selected": 0})
        bucket["days"] += 1
        if side in {"up", "down"}:
            bucket["breaks"] += 1
            bucket[side] += 1
        if row.get("event_selected_by_v0"):
            bucket["v0_selected"] += 1
    return {
        "track": "T1: TXF Higher-Timeframe Regime -> TMF Expression",
        "candidate": "T1_A_opening_range_definition_coverage_audit_v0",
        "rows": len(rows),
        "break_rows": len(break_rows),
        "v0_selected_rows": len(selected_rows),
        "contracts": sorted(by_contract),
        "by_contract": by_contract,
        "coverage_status": coverage_status,
        "purpose": "coverage_only_no_pnl",
    }


def run_audit(args: argparse.Namespace) -> dict[str, object]:
    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    pairs = _matching_pairs(raw_dir, args.months.split(","))
    if args.max_pairs is not None:
        pairs = pairs[: args.max_pairs]
    audited_dates = [_date_from_path(txf_path) for txf_path, _ in pairs]
    print(f"t1_audit_start pairs={len(pairs)} months={args.months}", file=sys.stderr, flush=True)
    for idx, (txf_path, tmf_path) in enumerate(pairs, start=1):
        started = time_module.monotonic()
        print(
            f"t1_pair_start {idx}/{len(pairs)} txf={txf_path.name} tmf={tmf_path.name}",
            file=sys.stderr,
            flush=True,
        )
        before = len(rows)
        rows.extend(
            audit_opening_range_pair(
                txf_path=txf_path,
                tmf_path=tmf_path,
                session_tz_offset_hours=args.session_tz_offset_hours,
                opening_minutes=args.opening_minutes,
                confirm_minutes=args.confirm_minutes,
                min_break_points=args.min_break_points,
                min_rv_ratio=args.min_rv_ratio,
            )
        )
        elapsed = time_module.monotonic() - started
        print(
            f"t1_pair_done {idx}/{len(pairs)} events={len(rows) - before} elapsed_s={elapsed:.2f}",
            file=sys.stderr,
            flush=True,
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"{stamp}_opening_range_events.csv"
    json_path = out_dir / f"{stamp}_summary.json"
    if rows:
        fieldnames = list(rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")
    summary = summarize_rows(rows, audited_dates=audited_dates)
    summary["csv_path"] = str(csv_path)
    summary["definition"] = {
        "l2_alpha_restriction": (
            "L2 is used only for executable bid/ask and quote sanity checks; "
            "entry is TXF higher-timeframe regime."
        ),
        "opening_range_minutes": args.opening_minutes,
        "confirm_minutes": args.confirm_minutes,
        "min_break_points": args.min_break_points,
        "min_rv_ratio": args.min_rv_ratio,
        "months": args.months.split(","),
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_coverage_audit(args: argparse.Namespace) -> dict[str, object]:
    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    pairs = _matching_pairs(raw_dir, args.months.split(","))
    if args.max_pairs is not None:
        pairs = pairs[: args.max_pairs]
    print(f"t1_coverage_start pairs={len(pairs)} months={args.months}", file=sys.stderr, flush=True)
    for idx, (txf_path, tmf_path) in enumerate(pairs, start=1):
        started = time_module.monotonic()
        print(
            f"t1_coverage_pair_start {idx}/{len(pairs)} txf={txf_path.name}",
            file=sys.stderr,
            flush=True,
        )
        rows.append(
            coverage_audit_opening_range_pair(
                txf_path=txf_path,
                tmf_path=tmf_path,
                session_tz_offset_hours=args.session_tz_offset_hours,
                opening_minutes=args.opening_minutes,
                confirm_minutes=args.confirm_minutes,
                min_break_points=args.min_break_points,
                min_rv_ratio=args.min_rv_ratio,
                persistence_minutes=args.persistence_minutes,
            )
        )
        elapsed = time_module.monotonic() - started
        print(
            f"t1_coverage_pair_done {idx}/{len(pairs)} break_side={rows[-1].get('break_side')} "
            f"v0={rows[-1].get('event_selected_by_v0')} elapsed_s={elapsed:.2f}",
            file=sys.stderr,
            flush=True,
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"{stamp}_opening_range_coverage.csv"
    json_path = out_dir / f"{stamp}_summary.json"
    fieldnames = [
        "contract",
        "trading_day",
        "pair_id",
        "or_start",
        "or_end",
        "bbo_first_time",
        "bbo_last_time",
        "coverage_status",
        "or_high",
        "or_low",
        "or_width",
        "post_or_high",
        "post_or_low",
        "max_upside_break_pts",
        "max_downside_break_pts",
        "first_up_break_time",
        "first_down_break_time",
        "break_side",
        "break_magnitude_pts",
        "break_magnitude_vs_or_width",
        "break_magnitude_vs_prior_realized_vol",
        "vwap_side_at_break",
        "reverted_to_or",
        "time_above_or_high",
        "time_below_or_low",
        "event_selected_by_v0",
        "persistent_after_break",
        "realized_vol_ratio",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    summary = summarize_coverage_rows(rows)
    summary["csv_path"] = str(csv_path)
    summary["definition"] = {
        "opening_range_minutes": args.opening_minutes,
        "confirm_minutes": args.confirm_minutes,
        "min_break_points_for_v0_flag": args.min_break_points,
        "min_rv_ratio_for_v0_flag": args.min_rv_ratio,
        "persistence_minutes": args.persistence_minutes,
        "months": args.months.split(","),
        "no_pnl": True,
        "vwap_is_diagnostic_for_coverage": True,
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run T1 opening-range regime viability audit.")
    parser.add_argument("--mode", choices=("viability", "coverage"), default="viability")
    parser.add_argument("--raw-dir", default="research/data/raw")
    parser.add_argument("--out-dir", default="research/experiments/validations/T1_regime_viability_audit_v0")
    parser.add_argument("--months", default="B6,C6,D6,E6")
    parser.add_argument("--session-tz-offset-hours", type=int, default=8)
    parser.add_argument("--opening-minutes", type=int, default=30)
    parser.add_argument("--confirm-minutes", type=int, default=30)
    parser.add_argument("--min-break-points", type=float, default=8.0)
    parser.add_argument("--min-rv-ratio", type=float, default=1.25)
    parser.add_argument("--max-pairs", type=int, default=None)
    parser.add_argument("--persistence-minutes", type=int, default=5)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_coverage_audit(args) if args.mode == "coverage" else run_audit(args)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
