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
from typing import Iterable, Sequence, cast

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


# ---------------------------------------------------------------------------
# T1-B: volatility-compression -> directional expansion
#
# Mechanism (frozen V0, see research/alphas/t1b_txf_volcompress_tmf/README.md):
# slide an anchor across the day session; at each anchor compare the realized
# vol of a compression window against the prior baseline window. When the ratio
# is <= ``max_compression_ratio`` (a genuine "coil"), watch for the first
# directional break out of the compression range by >= ``min_break_points``.
# Direction is the break side; entry is the TMF executable ask/bid at the TXF
# trigger time (reuses ``evaluate_executable_returns``). A cooldown enforces
# no-overlap. L2 is NOT an entry input here -- only TXF mid drives the signal
# and TMF bid/ask provides executable fills.
#
# NB: ``RegimeEvent.opening_range_high``/``opening_range_low`` are reused to
# carry the *compression* range bounds, and ``realized_vol_ratio`` carries the
# compression ratio (compression_rv / baseline_rv, < 1 for a coil -- the
# inverse semantics of T1-A's expansion ratio).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolCompressionConfig:
    session_start_ns: int
    session_minutes: int = 300          # 08:45-13:45 TPE day session
    baseline_minutes: int = 30          # reference vol window
    compression_minutes: int = 30       # coil window
    break_window_minutes: int = 30      # break must occur within this after the coil
    step_minutes: int = 5               # anchor slide granularity
    max_compression_ratio: float = 0.70  # compression_rv <= 0.70 * baseline_rv
    min_break_points: float = 8.0       # break beyond range by >= this many pts
    cooldown_minutes: int = 60          # no overlapping entries (max hold horizon)
    min_window_points: int = 5          # min quotes per window for a valid RV


def detect_vol_compression_events(
    bbo: BboFrame,
    trades: TradeFrame,
    *,
    contract: str,
    date: str,
    config: VolCompressionConfig,
) -> list[RegimeEvent]:
    if len(bbo.ts_ns) < config.min_window_points * 3:
        return []

    start = config.session_start_ns
    session_end = start + config.session_minutes * NS_PER_MINUTE
    baseline_ns = config.baseline_minutes * NS_PER_MINUTE
    compression_ns = config.compression_minutes * NS_PER_MINUTE
    break_ns = config.break_window_minutes * NS_PER_MINUTE
    step_ns = config.step_minutes * NS_PER_MINUTE
    cooldown_ns = config.cooldown_minutes * NS_PER_MINUTE

    ts = bbo.ts_ns
    mid = bbo.mid
    events: list[RegimeEvent] = []
    cooldown_until = start
    anchor = start + baseline_ns + compression_ns
    last_anchor = session_end - break_ns

    while anchor <= last_anchor:
        if anchor < cooldown_until:
            anchor += step_ns
            continue
        base_mask = (ts >= anchor - compression_ns - baseline_ns) & (ts < anchor - compression_ns)
        comp_mask = (ts >= anchor - compression_ns) & (ts < anchor)
        if (
            int(np.count_nonzero(base_mask)) < config.min_window_points
            or int(np.count_nonzero(comp_mask)) < config.min_window_points
        ):
            anchor += step_ns
            continue

        baseline_rv = _realized_vol(mid[base_mask])
        compression_rv = _realized_vol(mid[comp_mask])
        if baseline_rv <= 0.0:
            anchor += step_ns
            continue
        ratio = compression_rv / baseline_rv
        if ratio > config.max_compression_ratio:
            anchor += step_ns
            continue

        comp_mid = mid[comp_mask]
        range_high = float(np.max(comp_mid))
        range_low = float(np.min(comp_mid))

        break_mask = (ts >= anchor) & (ts <= anchor + break_ns)
        if not np.any(break_mask):
            anchor += step_ns
            continue
        b_ts = ts[break_mask]
        b_mid = mid[break_mask]
        long_break = b_mid >= range_high + config.min_break_points
        short_break = b_mid <= range_low - config.min_break_points
        first_long = int(np.flatnonzero(long_break)[0]) if np.any(long_break) else None
        first_short = int(np.flatnonzero(short_break)[0]) if np.any(short_break) else None
        if first_long is not None and (first_short is None or first_long <= first_short):
            direction = 1
            local = first_long
        elif first_short is not None:
            direction = -1
            local = first_short
        else:
            anchor += step_ns
            continue

        trigger_ns = int(b_ts[local])
        entry_ref = float(b_mid[local])
        vwap = _trade_vwap_until(trades, trigger_ns)
        if vwap is not None:
            if direction == 1 and entry_ref <= vwap:
                anchor += step_ns
                continue
            if direction == -1 and entry_ref >= vwap:
                anchor += step_ns
                continue

        events.append(
            RegimeEvent(
                contract=contract,
                date=date,
                regime_type="T1-B_vol_compression_expansion",
                trigger_time=_iso_from_ns(trigger_ns),
                trigger_time_ns=trigger_ns,
                direction=direction,
                txf_entry_ref=entry_ref,
                opening_range_high=range_high,
                opening_range_low=range_low,
                trade_vwap=vwap,
                realized_vol_ratio=float(ratio),
            )
        )
        cooldown_until = trigger_ns + cooldown_ns
        anchor = max(anchor + step_ns, trigger_ns + step_ns)

    return events


def audit_vol_compression_pair(
    *,
    txf_path: Path,
    tmf_path: Path,
    session_tz_offset_hours: int = 8,
    cost_pts: float = 8.0,
    session_minutes: int = 300,
    baseline_minutes: int = 30,
    compression_minutes: int = 30,
    break_window_minutes: int = 30,
    step_minutes: int = 5,
    max_compression_ratio: float = 0.70,
    min_break_points: float = 8.0,
    cooldown_minutes: int = 60,
) -> list[dict[str, object]]:
    date = _date_from_path(txf_path)
    txf_contract = txf_path.name.split("_", 1)[0]
    tmf_contract = tmf_path.name.split("_", 1)[0]
    txf_bbo, txf_trades = _load_frames(txf_path)
    tmf_bbo, _ = _load_frames(tmf_path)
    config = VolCompressionConfig(
        session_start_ns=_session_start_ns(date, tz_offset_hours=session_tz_offset_hours),
        session_minutes=session_minutes,
        baseline_minutes=baseline_minutes,
        compression_minutes=compression_minutes,
        break_window_minutes=break_window_minutes,
        step_minutes=step_minutes,
        max_compression_ratio=max_compression_ratio,
        min_break_points=min_break_points,
        cooldown_minutes=cooldown_minutes,
    )
    rows: list[dict[str, object]] = []
    for event in detect_vol_compression_events(
        txf_bbo,
        txf_trades,
        contract=txf_contract,
        date=date,
        config=config,
    ):
        try:
            eval_row = evaluate_executable_returns(
                tmf_bbo,
                trigger_time_ns=event.trigger_time_ns,
                direction=event.direction,
            )
        except ValueError:
            # No TMF executable quote at/after the trigger -> not tradeable; skip.
            continue
        after = txf_bbo.ts_ns >= event.trigger_time_ns
        post_mid = txf_bbo.mid[after]
        # Stop structure = compression-range opposite side.
        if event.direction > 0:
            reverted = bool(np.any(post_mid <= event.opening_range_low)) if len(post_mid) else False
        else:
            reverted = bool(np.any(post_mid >= event.opening_range_high)) if len(post_mid) else False
        gross = eval_row.get("return_30m")
        net = (float(gross) - cost_pts) if gross is not None else None
        row: dict[str, object] = {
            "contract": f"{txf_contract}->{tmf_contract}",
            "date": date,
            "regime_type": event.regime_type,
            "trigger_time": event.trigger_time,
            "direction": event.direction,
            "txf_entry_ref": event.txf_entry_ref,
            "compression_range_high": event.opening_range_high,
            "compression_range_low": event.opening_range_low,
            "compression_ratio": event.realized_vol_ratio,
            "trade_vwap": event.trade_vwap,
            "stop_structure_breached": reverted,
            "reverted_to_range": reverted,
            "cost_pts": cost_pts,
            "net_after_cost_30m": net,
            "net_30m_pts": gross,
            **eval_row,
        }
        rows.append(row)
    return rows


def _subset_scorecard(rows: Sequence[dict[str, object]]) -> dict[str, object]:
    nets = [float(cast(float, r["net_after_cost_30m"])) for r in rows if r.get("net_after_cost_30m") is not None]
    stop_exit_nets = [
        float(cast(float, r["stop_exit_net_after_cost_30m"]))
        for r in rows
        if r.get("stop_exit_net_after_cost_30m") is not None
    ]
    gross = [float(cast(float, r["net_30m_pts"])) for r in rows if r.get("net_30m_pts") is not None]
    contracts = sorted({str(r["contract"]).split("->", 1)[0] for r in rows})
    event_dates = sorted({str(r["date"]) for r in rows})
    stop_breaches = sum(1 for r in rows if r.get("stop_structure_breached"))
    full_session_stop_rows = [r for r in rows if "full_session_stop_structure_breached" in r]
    full_session_stop_breaches = sum(
        1 for r in full_session_stop_rows if r.get("full_session_stop_structure_breached")
    )
    remove_best_1 = sorted(nets)[:-1] if len(nets) > 1 else nets

    net_by_day: dict[str, float] = {}
    net_by_contract: dict[str, float] = {}
    for r in rows:
        net = r.get("net_after_cost_30m")
        if net is None:
            continue
        net_f = float(cast(float, net))
        net_by_day[str(r["date"])] = net_by_day.get(str(r["date"]), 0.0) + net_f
        c = str(r["contract"]).split("->", 1)[0]
        net_by_contract[c] = net_by_contract.get(c, 0.0) + net_f

    total_net = sum(nets)
    positive_day_total = sum(v for v in net_by_day.values() if v > 0)
    max_day_net = max(net_by_day.values()) if net_by_day else 0.0
    max_contract_net = max(net_by_contract.values()) if net_by_contract else 0.0
    pos_days = sum(1 for v in net_by_day.values() if v > 0)
    monthly_net: dict[str, float] = {}
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for day, day_net in sorted(net_by_day.items()):
        month = day[:7]
        monthly_net[month] = monthly_net.get(month, 0.0) + day_net
        equity += day_net
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    monthly_values = list(monthly_net.values())
    positive_month_total = sum(v for v in monthly_values if v > 0)
    max_month_net = max(monthly_values) if monthly_values else 0.0
    average_monthly_net = (sum(monthly_values) / len(monthly_values)) if monthly_values else None
    if average_monthly_net is None:
        drawdown_within_monthly_gate = None
    elif average_monthly_net <= 0:
        drawdown_within_monthly_gate = False
    else:
        drawdown_within_monthly_gate = bool(max_drawdown <= 2.0 * average_monthly_net)

    mean_net = (sum(nets) / len(nets)) if nets else None
    return {
        "events": len(nets),
        "trading_days": len(event_dates),
        "contracts": contracts,
        "mean_net_after_cost_30m": mean_net,
        "mean_net_edge_pts_per_trade": mean_net,
        "median_net_after_cost_30m": median(nets) if nets else None,
        "mean_stop_exit_net_after_cost_30m": (
            (sum(stop_exit_nets) / len(stop_exit_nets)) if stop_exit_nets else None
        ),
        "median_stop_exit_net_after_cost_30m": median(stop_exit_nets) if stop_exit_nets else None,
        "positive_stop_exit_fraction": (
            (sum(1 for net in stop_exit_nets if net > 0) / len(stop_exit_nets)) if stop_exit_nets else None
        ),
        "median_gross_return_30m": median(gross) if gross else None,
        "p10_net_after_cost_30m": float(np.percentile(nets, 10)) if nets else None,
        "p05_net_after_cost_30m": float(np.percentile(nets, 5)) if nets else None,
        "remove_best_1_median_net": median(remove_best_1) if remove_best_1 else None,
        "stop_breach_rate": (stop_breaches / len(rows)) if rows else None,
        "full_session_stop_breach_rate": (
            (full_session_stop_breaches / len(full_session_stop_rows))
            if full_session_stop_rows
            else None
        ),
        "positive_day_fraction": (pos_days / len(net_by_day)) if net_by_day else None,
        "total_net": total_net,
        "max_single_day_net": max_day_net,
        "max_single_day_net_share_of_positive": (
            (max_day_net / positive_day_total) if positive_day_total > 0 else None
        ),
        "net_by_contract": net_by_contract,
        "max_single_contract_net_share_of_positive": (
            (max_contract_net / positive_day_total) if positive_day_total > 0 and max_contract_net > 0 else None
        ),
        "max_drawdown_net_pts": max_drawdown if net_by_day else None,
        "monthly_net_pnl": monthly_net,
        "average_monthly_net_pnl": average_monthly_net,
        "median_monthly_net_pnl": median(monthly_values) if monthly_values else None,
        "worst_month_net_pnl": min(monthly_values) if monthly_values else None,
        "max_single_month_net_share_of_positive": (
            (max_month_net / positive_month_total) if positive_month_total > 0 and max_month_net > 0 else None
        ),
        "drawdown_within_2x_average_monthly_net_pnl": drawdown_within_monthly_gate,
    }


def _t1b_research_decision(
    *,
    median_net: float | None,
    sample_ok: bool,
    n_events: int,
    min_events: int,
    audited_days: int,
    min_trading_days: int,
    cross_contract_complete: bool,
    dominance_fail: bool,
    stop_breach_fail: bool,
    drawdown_fail: bool,
    edge_floor_cleared: bool,
    risk_controlled_edge_floor_cleared: bool | None = None,
) -> dict[str, object]:
    if median_net is not None and median_net <= 0:
        return {
            "status": "failed",
            "reason": "t1b_kill:median_net_non_positive",
            "evidence": ["median_net_positive"],
            "decided_by": "t1b_v0_hard_gate",
        }
    if not sample_ok:
        evidence = ["min_sample_size"]
        if n_events < min_events:
            evidence.append("events")
        if audited_days < min_trading_days:
            evidence.append("trading_days")
        if not cross_contract_complete:
            evidence.append("cross_contract_complete")
        return {
            "status": "needs_more_sample",
            "reason": "t1b_sample_gate:" + "|".join(evidence[1:]),
            "evidence": evidence,
            "decided_by": "t1b_v0_hard_gate",
        }
    if dominance_fail or stop_breach_fail or drawdown_fail:
        evidence = []
        if dominance_fail:
            evidence.extend(["single_day_dominance", "single_contract_concentration"])
        if stop_breach_fail:
            evidence.append("stop_structure_breach")
        if drawdown_fail:
            evidence.append("max_drawdown_vs_average_monthly_net_pnl")
        return {
            "status": "blocked_by_risk",
            "reason": "t1b_risk_gate:" + "|".join(evidence),
            "evidence": evidence,
            "decided_by": "t1b_v0_hard_gate",
        }
    # Canonical risk-controlled metric gate (T1-F): when a caller declares a
    # risk-controlled (stop-exit) edge as its canonical metric, that metric must
    # clear the floor before the candidate can be research-eligible — otherwise
    # a candidate failing its declared canonical metric could still pass on the
    # legacy time-exit edge alone.  Other tracks pass None and skip this gate.
    if risk_controlled_edge_floor_cleared is False:
        return {
            "status": "failed",
            "reason": "t1f_risk_controlled_edge_floor_not_cleared",
            "evidence": ["stop_exit_net_after_cost_30m"],
            "decided_by": "t1b_v0_hard_gate",
        }
    if not edge_floor_cleared:
        return {
            "status": "failed",
            "reason": "t1b_edge_floor_not_cleared",
            "evidence": ["mean_net_edge_pts_per_trade"],
            "decided_by": "t1b_v0_hard_gate",
        }
    evidence = [
        "v0_latency_profile_deferred",
        "cost_uncertainty",
        "force_flat_residual",
        "inventory_mtm",
        "no_replay_paper_live_parity_evidence",
    ]
    return {
        "status": "blocked_by_audit",
        "reason": "t1b_v0_audit_blocker:" + "|".join(evidence),
        "evidence": evidence,
        "decided_by": "t1b_v0_hard_gate",
    }


def summarize_vol_compression_rows(
    rows: Sequence[dict[str, object]],
    *,
    audited_dates: Sequence[str] | None = None,
    oos_start: str | None = None,
    edge_floor_pts: float = 10.0,
    min_events: int = 80,
    min_trading_days: int = 20,
    required_contracts: Sequence[str] = ("TXFB6", "TXFC6", "TXFD6", "TXFE6"),
    h9_stop_breach_baseline: float = 0.50,
    max_single_day_share: float = 0.50,
) -> dict[str, object]:
    full = _subset_scorecard(rows)
    splits: dict[str, object] = {"full": full}
    if oos_start is not None:
        in_sample = [r for r in rows if str(r["date"]) < oos_start]
        out_sample = [r for r in rows if str(r["date"]) >= oos_start]
        splits["in_sample"] = _subset_scorecard(in_sample)
        splits["out_of_sample"] = _subset_scorecard(out_sample)

    def _f(value: object) -> float | None:
        return None if value is None else float(cast(float, value))

    audited_unique = sorted(set(audited_dates or [str(r["date"]) for r in rows]))
    contracts_present = {str(c) for c in cast("list[str]", full["contracts"])}
    cross_contract_complete = set(required_contracts).issubset(contracts_present)

    n_events = cast(int, full["events"])
    mean_net_edge = _f(full["mean_net_edge_pts_per_trade"])
    median_net = _f(full["median_net_after_cost_30m"])
    remove_best = _f(full["remove_best_1_median_net"])
    p10 = _f(full["p10_net_after_cost_30m"])
    stop_breach = _f(full["stop_breach_rate"])
    day_share = _f(full["max_single_day_net_share_of_positive"])
    contract_share = _f(full["max_single_contract_net_share_of_positive"])
    drawdown_within_monthly_gate = cast(
        bool | None,
        full["drawdown_within_2x_average_monthly_net_pnl"],
    )

    sample_ok = (
        n_events >= min_events
        and len(audited_unique) >= min_trading_days
        and cross_contract_complete
    )
    dominance_fail = (day_share is not None and day_share > max_single_day_share) or (
        contract_share is not None and contract_share >= 0.999
    )
    stop_breach_fail = stop_breach is not None and stop_breach >= h9_stop_breach_baseline
    drawdown_fail = drawdown_within_monthly_gate is False
    edge_floor_cleared = bool(mean_net_edge is not None and mean_net_edge > edge_floor_pts)

    # A clearly negative net edge is a KILL regardless of sample size. A
    # positive-but-undersized sample is NEEDS-MORE-DAYS -- dominance and
    # stop-breach are only assessable once the sample clears the hard-gate
    # floors (otherwise single-day/contract share is trivially 1.0).
    if median_net is not None and median_net <= 0:
        verdict = "KILL"
    elif not sample_ok:
        verdict = "NEEDS-MORE-DAYS"
    elif dominance_fail or stop_breach_fail or drawdown_fail:
        verdict = "KILL"
    elif median_net is not None and median_net > 0:
        verdict = "PROCEED"
    else:
        verdict = "NEEDS-MORE-DAYS"

    research_decision = _t1b_research_decision(
        median_net=median_net,
        sample_ok=sample_ok,
        n_events=n_events,
        min_events=min_events,
        audited_days=len(audited_unique),
        min_trading_days=min_trading_days,
        cross_contract_complete=cross_contract_complete,
        dominance_fail=dominance_fail,
        stop_breach_fail=stop_breach_fail,
        drawdown_fail=drawdown_fail,
        edge_floor_cleared=edge_floor_cleared,
    )

    return {
        "track": "T1-B: TXF Volatility-Compression -> TMF Directional Expansion",
        "candidate": "t1b_txf_volcompress_tmf",
        "audited_trading_days": len(audited_unique),
        "edge_floor_pts": edge_floor_pts,
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "edge_floor_cleared": edge_floor_cleared,
        "verdict": verdict,
        "research_decision": research_decision,
        "hard_gate": {
            "min_events": min_events,
            "events": n_events,
            "events_ok": bool(n_events >= min_events),
            "min_trading_days": min_trading_days,
            "trading_days_ok": bool(len(audited_unique) >= min_trading_days),
            "cross_contract_complete": bool(cross_contract_complete),
            "required_contracts": list(required_contracts),
            "median_net_positive": bool(median_net is not None and median_net > 0),
            "remove_best_1_non_collapsing": (bool(remove_best >= 0) if remove_best is not None else None),
            "p10_not_catastrophic": (bool(p10 > -3.0 * edge_floor_pts) if p10 is not None else None),
            "no_single_day_dominance": bool(day_share is None or day_share <= max_single_day_share),
            "no_single_contract_concentration": bool(contract_share is None or contract_share < 0.999),
            "stop_breach_below_h9_baseline": bool(stop_breach is None or stop_breach < h9_stop_breach_baseline),
            "drawdown_within_2x_average_monthly_net_pnl": drawdown_within_monthly_gate,
        },
        "splits": splits,
    }


def run_vol_compression_audit(args: argparse.Namespace) -> dict[str, object]:
    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    pairs = _matching_pairs(raw_dir, args.months.split(","))
    if args.max_date is not None:
        pairs = [(t, m) for (t, m) in pairs if _date_from_path(t) <= args.max_date]
    if args.min_date is not None:
        pairs = [(t, m) for (t, m) in pairs if _date_from_path(t) >= args.min_date]
    if args.max_pairs is not None:
        pairs = pairs[: args.max_pairs]
    audited_dates = sorted({_date_from_path(t) for t, _ in pairs})
    print(f"t1b_audit_start pairs={len(pairs)} months={args.months}", file=sys.stderr, flush=True)
    for idx, (txf_path, tmf_path) in enumerate(pairs, start=1):
        started = time_module.monotonic()
        before = len(rows)
        rows.extend(
            audit_vol_compression_pair(
                txf_path=txf_path,
                tmf_path=tmf_path,
                session_tz_offset_hours=args.session_tz_offset_hours,
                cost_pts=args.cost_pts,
                session_minutes=args.session_minutes,
                baseline_minutes=args.baseline_minutes,
                compression_minutes=args.compression_minutes,
                break_window_minutes=args.break_window_minutes,
                step_minutes=args.step_minutes,
                max_compression_ratio=args.max_compression_ratio,
                min_break_points=args.min_break_points,
                cooldown_minutes=args.cooldown_minutes,
            )
        )
        elapsed = time_module.monotonic() - started
        print(
            f"t1b_pair_done {idx}/{len(pairs)} txf={txf_path.name} events={len(rows) - before} elapsed_s={elapsed:.2f}",
            file=sys.stderr,
            flush=True,
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"{stamp}_vol_compression_events.csv"
    json_path = out_dir / f"{stamp}_summary.json"
    if rows:
        fieldnames = list(rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")
    summary = summarize_vol_compression_rows(
        rows,
        audited_dates=audited_dates,
        oos_start=args.oos_start,
        edge_floor_pts=args.edge_floor_pts,
    )
    summary["summary_path"] = str(json_path)
    summary["csv_path"] = str(csv_path)
    summary["artifact_scope"] = "validation_summary"
    summary["definition"] = {
        "l2_alpha_restriction": (
            "L2 used only for executable bid/ask and quote sanity; entry is TXF "
            "higher-timeframe volatility compression then directional break."
        ),
        "session_minutes": args.session_minutes,
        "baseline_minutes": args.baseline_minutes,
        "compression_minutes": args.compression_minutes,
        "break_window_minutes": args.break_window_minutes,
        "step_minutes": args.step_minutes,
        "max_compression_ratio": args.max_compression_ratio,
        "min_break_points": args.min_break_points,
        "cooldown_minutes": args.cooldown_minutes,
        "cost_pts": args.cost_pts,
        "oos_start": args.oos_start,
        "min_date": args.min_date,
        "max_date": args.max_date,
        "months": args.months.split(","),
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


@dataclass(frozen=True)
class IntradayMomentumConfig:
    session_start_ns: int
    session_minutes: int = 300          # 08:45-13:45 TPE day session
    open_window_minutes: int = 30       # first window: directional signal (08:45-09:15)
    predict_window_minutes: int = 30    # last window: trade window (13:15-13:45)
    min_open_move_pts: float = 10.0     # |first-window return| must be >= this (TXF pts)
    min_window_points: int = 5          # min quotes per window for a valid signal/trade


def detect_intraday_momentum_events(
    bbo: BboFrame,
    trades: TradeFrame,
    *,
    contract: str,
    date: str,
    config: IntradayMomentumConfig,
) -> list[RegimeEvent]:
    """Market-intraday-momentum signal (Gao-Han-Li-Zhou, JFE 2018; TAIEX-adapted).

    The first-window (open) TXF return predicts the last-window return with the
    SAME sign. Backward-looking only: the trigger fires at the start of the last
    window using information from the open window. ``min_open_move_pts`` is an
    absolute (not cross-day percentile) magnitude filter so the rule carries no
    look-ahead and avoids same-day distribution thresholds.
    """
    if len(bbo.ts_ns) < config.min_window_points * 2:
        return []

    start = config.session_start_ns
    session_end = start + config.session_minutes * NS_PER_MINUTE
    open_end = start + config.open_window_minutes * NS_PER_MINUTE
    entry_ns = session_end - config.predict_window_minutes * NS_PER_MINUTE
    if open_end > entry_ns:
        # windows overlap -> ill-posed config for this session length
        return []

    ts = bbo.ts_ns
    mid = bbo.mid

    open_mask = (ts >= start) & (ts < open_end)
    predict_mask = (ts >= entry_ns) & (ts <= session_end)
    if (
        int(np.count_nonzero(open_mask)) < config.min_window_points
        or int(np.count_nonzero(predict_mask)) < config.min_window_points
    ):
        return []

    open_mid = mid[open_mask]
    open_first = float(open_mid[0])
    open_last = float(open_mid[-1])
    ret_open = open_last - open_first
    if abs(ret_open) < config.min_open_move_pts:
        return []
    direction = 1 if ret_open > 0 else -1

    open_high = float(np.max(open_mid))
    open_low = float(np.min(open_mid))
    open_rv = _realized_vol(open_mid)
    move_vol_ratio = float(abs(ret_open) / open_rv) if open_rv > 0 else 0.0

    entry_idx = int(np.searchsorted(ts, entry_ns, side="left"))
    if entry_idx >= len(ts):
        return []
    entry_ref = float(mid[entry_idx])

    # Trend-consistency guard: entry must sit on the directional side of session
    # VWAP (long above, short below). Mirrors the T1-A/B VWAP-alignment filter.
    vwap = _trade_vwap_until(trades, entry_ns)
    if vwap is not None:
        if direction == 1 and entry_ref <= vwap:
            return []
        if direction == -1 and entry_ref >= vwap:
            return []

    return [
        RegimeEvent(
            contract=contract,
            date=date,
            regime_type="T1-D_intraday_session_momentum",
            trigger_time=_iso_from_ns(entry_ns),
            trigger_time_ns=entry_ns,
            direction=direction,
            txf_entry_ref=entry_ref,
            opening_range_high=open_high,
            opening_range_low=open_low,
            trade_vwap=vwap,
            realized_vol_ratio=move_vol_ratio,
        )
    ]


def audit_intraday_momentum_pair(
    *,
    txf_path: Path,
    tmf_path: Path,
    session_tz_offset_hours: int = 8,
    cost_pts: float = 8.0,
    session_minutes: int = 300,
    open_window_minutes: int = 30,
    predict_window_minutes: int = 30,
    min_open_move_pts: float = 10.0,
) -> list[dict[str, object]]:
    date = _date_from_path(txf_path)
    txf_contract = txf_path.name.split("_", 1)[0]
    tmf_contract = tmf_path.name.split("_", 1)[0]
    txf_bbo, txf_trades = _load_frames(txf_path)
    tmf_bbo, _ = _load_frames(tmf_path)
    config = IntradayMomentumConfig(
        session_start_ns=_session_start_ns(date, tz_offset_hours=session_tz_offset_hours),
        session_minutes=session_minutes,
        open_window_minutes=open_window_minutes,
        predict_window_minutes=predict_window_minutes,
        min_open_move_pts=min_open_move_pts,
    )
    rows: list[dict[str, object]] = []
    for event in detect_intraday_momentum_events(
        txf_bbo,
        txf_trades,
        contract=txf_contract,
        date=date,
        config=config,
    ):
        try:
            eval_row = evaluate_executable_returns(
                tmf_bbo,
                trigger_time_ns=event.trigger_time_ns,
                direction=event.direction,
            )
        except ValueError:
            # No TMF executable quote at/after the trigger -> not tradeable; skip.
            continue
        after = txf_bbo.ts_ns >= event.trigger_time_ns
        post_mid = txf_bbo.mid[after]
        # Stop structure = opposite side of the open-window range (full give-back
        # of the morning directional move).
        if event.direction > 0:
            reverted = bool(np.any(post_mid <= event.opening_range_low)) if len(post_mid) else False
        else:
            reverted = bool(np.any(post_mid >= event.opening_range_high)) if len(post_mid) else False
        gross = eval_row.get("return_30m")
        net = (float(gross) - cost_pts) if gross is not None else None
        row: dict[str, object] = {
            "contract": f"{txf_contract}->{tmf_contract}",
            "date": date,
            "regime_type": event.regime_type,
            "trigger_time": event.trigger_time,
            "direction": event.direction,
            "txf_entry_ref": event.txf_entry_ref,
            "open_window_high": event.opening_range_high,
            "open_window_low": event.opening_range_low,
            "morning_move_vol_ratio": event.realized_vol_ratio,
            "trade_vwap": event.trade_vwap,
            "stop_structure_breached": reverted,
            "reverted_to_open_range": reverted,
            "cost_pts": cost_pts,
            "net_after_cost_30m": net,
            "net_30m_pts": gross,
            **eval_row,
        }
        rows.append(row)
    return rows


def summarize_intraday_momentum_rows(
    rows: Sequence[dict[str, object]],
    *,
    audited_dates: Sequence[str] | None = None,
    oos_start: str | None = None,
    edge_floor_pts: float = 10.0,
    min_events: int = 80,
    min_trading_days: int = 20,
    required_contracts: Sequence[str] = ("TXFB6", "TXFC6", "TXFD6", "TXFE6"),
    h9_stop_breach_baseline: float = 0.50,
    max_single_day_share: float = 0.50,
) -> dict[str, object]:
    full = _subset_scorecard(rows)
    splits: dict[str, object] = {"full": full}
    if oos_start is not None:
        in_sample = [r for r in rows if str(r["date"]) < oos_start]
        out_sample = [r for r in rows if str(r["date"]) >= oos_start]
        splits["in_sample"] = _subset_scorecard(in_sample)
        splits["out_of_sample"] = _subset_scorecard(out_sample)

    def _f(value: object) -> float | None:
        return None if value is None else float(cast(float, value))

    audited_unique = sorted(set(audited_dates or [str(r["date"]) for r in rows]))
    contracts_present = {str(c) for c in cast("list[str]", full["contracts"])}
    cross_contract_complete = set(required_contracts).issubset(contracts_present)

    n_events = cast(int, full["events"])
    mean_net_edge = _f(full["mean_net_edge_pts_per_trade"])
    median_net = _f(full["median_net_after_cost_30m"])
    remove_best = _f(full["remove_best_1_median_net"])
    p10 = _f(full["p10_net_after_cost_30m"])
    stop_breach = _f(full["stop_breach_rate"])
    day_share = _f(full["max_single_day_net_share_of_positive"])
    contract_share = _f(full["max_single_contract_net_share_of_positive"])
    drawdown_within_monthly_gate = cast(
        bool | None,
        full["drawdown_within_2x_average_monthly_net_pnl"],
    )

    sample_ok = (
        n_events >= min_events
        and len(audited_unique) >= min_trading_days
        and cross_contract_complete
    )
    dominance_fail = (day_share is not None and day_share > max_single_day_share) or (
        contract_share is not None and contract_share >= 0.999
    )
    stop_breach_fail = stop_breach is not None and stop_breach >= h9_stop_breach_baseline
    drawdown_fail = drawdown_within_monthly_gate is False
    edge_floor_cleared = bool(mean_net_edge is not None and mean_net_edge > edge_floor_pts)

    if median_net is not None and median_net <= 0:
        verdict = "KILL"
    elif not sample_ok:
        verdict = "NEEDS-MORE-DAYS"
    elif dominance_fail or stop_breach_fail or drawdown_fail:
        verdict = "KILL"
    elif median_net is not None and median_net > 0:
        verdict = "PROCEED"
    else:
        verdict = "NEEDS-MORE-DAYS"

    research_decision = _t1b_research_decision(
        median_net=median_net,
        sample_ok=sample_ok,
        n_events=n_events,
        min_events=min_events,
        audited_days=len(audited_unique),
        min_trading_days=min_trading_days,
        cross_contract_complete=cross_contract_complete,
        dominance_fail=dominance_fail,
        stop_breach_fail=stop_breach_fail,
        drawdown_fail=drawdown_fail,
        edge_floor_cleared=edge_floor_cleared,
    )

    return {
        "track": "T1-D: TXF Intraday Session Momentum -> TMF",
        "candidate": "t1d_txf_intraday_momentum_tmf",
        "audited_trading_days": len(audited_unique),
        "edge_floor_pts": edge_floor_pts,
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "edge_floor_cleared": edge_floor_cleared,
        "verdict": verdict,
        "research_decision": research_decision,
        "hard_gate": {
            "min_events": min_events,
            "events": n_events,
            "events_ok": bool(n_events >= min_events),
            "min_trading_days": min_trading_days,
            "trading_days_ok": bool(len(audited_unique) >= min_trading_days),
            "cross_contract_complete": bool(cross_contract_complete),
            "required_contracts": list(required_contracts),
            "median_net_positive": bool(median_net is not None and median_net > 0),
            "remove_best_1_non_collapsing": (bool(remove_best >= 0) if remove_best is not None else None),
            "p10_not_catastrophic": (bool(p10 > -3.0 * edge_floor_pts) if p10 is not None else None),
            "no_single_day_dominance": bool(day_share is None or day_share <= max_single_day_share),
            "no_single_contract_concentration": bool(contract_share is None or contract_share < 0.999),
            "stop_breach_below_h9_baseline": bool(stop_breach is None or stop_breach < h9_stop_breach_baseline),
            "drawdown_within_2x_average_monthly_net_pnl": drawdown_within_monthly_gate,
        },
        "splits": splits,
    }


def run_intraday_momentum_audit(args: argparse.Namespace) -> dict[str, object]:
    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    pairs = _matching_pairs(raw_dir, args.months.split(","))
    if args.max_date is not None:
        pairs = [(t, m) for (t, m) in pairs if _date_from_path(t) <= args.max_date]
    if args.min_date is not None:
        pairs = [(t, m) for (t, m) in pairs if _date_from_path(t) >= args.min_date]
    if args.max_pairs is not None:
        pairs = pairs[: args.max_pairs]
    audited_dates = sorted({_date_from_path(t) for t, _ in pairs})
    print(f"t1d_audit_start pairs={len(pairs)} months={args.months}", file=sys.stderr, flush=True)
    for idx, (txf_path, tmf_path) in enumerate(pairs, start=1):
        started = time_module.monotonic()
        before = len(rows)
        rows.extend(
            audit_intraday_momentum_pair(
                txf_path=txf_path,
                tmf_path=tmf_path,
                session_tz_offset_hours=args.session_tz_offset_hours,
                cost_pts=args.cost_pts,
                session_minutes=args.session_minutes,
                open_window_minutes=args.open_window_minutes,
                predict_window_minutes=args.predict_window_minutes,
                min_open_move_pts=args.min_open_move_pts,
            )
        )
        elapsed = time_module.monotonic() - started
        print(
            f"t1d_pair_done {idx}/{len(pairs)} txf={txf_path.name} events={len(rows) - before} elapsed_s={elapsed:.2f}",
            file=sys.stderr,
            flush=True,
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"{stamp}_intraday_momentum_events.csv"
    json_path = out_dir / f"{stamp}_summary.json"
    if rows:
        fieldnames = list(rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")
    summary = summarize_intraday_momentum_rows(
        rows,
        audited_dates=audited_dates,
        oos_start=args.oos_start,
        edge_floor_pts=args.edge_floor_pts,
    )
    summary["summary_path"] = str(json_path)
    summary["csv_path"] = str(csv_path)
    summary["artifact_scope"] = "validation_summary"
    summary["definition"] = {
        "l2_alpha_restriction": (
            "L2 used only for executable bid/ask and quote sanity; entry is the "
            "TXF open-window directional return predicting the last-window return."
        ),
        "session_minutes": args.session_minutes,
        "open_window_minutes": args.open_window_minutes,
        "predict_window_minutes": args.predict_window_minutes,
        "min_open_move_pts": args.min_open_move_pts,
        "cost_pts": args.cost_pts,
        "oos_start": args.oos_start,
        "min_date": args.min_date,
        "max_date": args.max_date,
        "months": args.months.split(","),
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


@dataclass(frozen=True)
class OpenGapFadeConfig:
    session_start_ns: int               # today's session start (08:45)
    prior_session_start_ns: int         # prior available session start
    session_minutes: int = 300          # 08:45-13:45 TPE day session
    prior_close_window_minutes: int = 30  # average prior-session close over last N min
    open_confirm_minutes: int = 15      # wait N min after open before entering the fade
    min_gap_pts: float = 15.0           # outsized prior-close -> today-open gap threshold
    stop_buffer_pts: float = 15.0       # gap-extension stop beyond today's open
    min_window_points: int = 5


def detect_open_gap_fade_events(
    prior_bbo: BboFrame,
    today_bbo: BboFrame,
    today_trades: TradeFrame,
    *,
    contract: str,
    date: str,
    config: OpenGapFadeConfig,
) -> list[RegimeEvent]:
    """Open-gap overreaction fade (Asian index futures overreact to overnight
    information; partial intraday reversal). The gap is constructed endogenously
    from the prior available session's close and today's open -- no external EOD
    feed. Direction = FADE (gap up -> short, gap down -> long). Backward-looking:
    the gap is measured at the open; entry is after a confirm delay.
    """
    if (
        len(prior_bbo.ts_ns) < config.min_window_points
        or len(today_bbo.ts_ns) < config.min_window_points
    ):
        return []

    prior_start = config.prior_session_start_ns
    prior_end = prior_start + config.session_minutes * NS_PER_MINUTE
    close_win = config.prior_close_window_minutes * NS_PER_MINUTE
    prior_close_mask = (prior_bbo.ts_ns >= prior_end - close_win) & (prior_bbo.ts_ns <= prior_end)
    if not np.any(prior_close_mask):
        return []
    prior_close = float(np.mean(prior_bbo.mid[prior_close_mask]))

    start = config.session_start_ns
    session_end = start + config.session_minutes * NS_PER_MINUTE
    today_open_mask = (today_bbo.ts_ns >= start) & (today_bbo.ts_ns <= session_end)
    if not np.any(today_open_mask):
        return []
    today_open = float(today_bbo.mid[np.flatnonzero(today_open_mask)[0]])

    gap = today_open - prior_close
    if abs(gap) < config.min_gap_pts:
        return []
    direction = -1 if gap > 0 else 1  # fade the gap

    entry_ns = start + config.open_confirm_minutes * NS_PER_MINUTE
    entry_idx = int(np.searchsorted(today_bbo.ts_ns, entry_ns, side="left"))
    if entry_idx >= len(today_bbo.ts_ns) or int(today_bbo.ts_ns[entry_idx]) > session_end:
        return []
    entry_ref = float(today_bbo.mid[entry_idx])
    vwap = _trade_vwap_until(today_trades, entry_ns)

    return [
        RegimeEvent(
            contract=contract,
            date=date,
            regime_type="T1-E_open_gap_overreaction_fade",
            trigger_time=_iso_from_ns(entry_ns),
            trigger_time_ns=entry_ns,
            direction=direction,
            txf_entry_ref=entry_ref,
            # Stop levels = today's open +/- gap-extension buffer (gap continues).
            opening_range_high=today_open + config.stop_buffer_pts,
            opening_range_low=today_open - config.stop_buffer_pts,
            trade_vwap=vwap,
            realized_vol_ratio=float(gap),  # reused field carries the signed gap (pts)
        )
    ]


def audit_open_gap_fade_pair(
    *,
    prior_txf_path: Path,
    today_txf_path: Path,
    today_tmf_path: Path,
    session_tz_offset_hours: int = 8,
    cost_pts: float = 8.0,
    session_minutes: int = 300,
    prior_close_window_minutes: int = 30,
    open_confirm_minutes: int = 15,
    min_gap_pts: float = 15.0,
    stop_buffer_pts: float = 15.0,
) -> list[dict[str, object]]:
    date = _date_from_path(today_txf_path)
    prior_date = _date_from_path(prior_txf_path)
    txf_contract = today_txf_path.name.split("_", 1)[0]
    tmf_contract = today_tmf_path.name.split("_", 1)[0]
    prior_bbo, _ = _load_frames(prior_txf_path)
    today_bbo, today_trades = _load_frames(today_txf_path)
    tmf_bbo, _ = _load_frames(today_tmf_path)
    config = OpenGapFadeConfig(
        session_start_ns=_session_start_ns(date, tz_offset_hours=session_tz_offset_hours),
        prior_session_start_ns=_session_start_ns(prior_date, tz_offset_hours=session_tz_offset_hours),
        session_minutes=session_minutes,
        prior_close_window_minutes=prior_close_window_minutes,
        open_confirm_minutes=open_confirm_minutes,
        min_gap_pts=min_gap_pts,
        stop_buffer_pts=stop_buffer_pts,
    )
    rows: list[dict[str, object]] = []
    for event in detect_open_gap_fade_events(
        prior_bbo,
        today_bbo,
        today_trades,
        contract=txf_contract,
        date=date,
        config=config,
    ):
        try:
            eval_row = evaluate_executable_returns(
                tmf_bbo,
                trigger_time_ns=event.trigger_time_ns,
                direction=event.direction,
            )
        except ValueError:
            continue
        after = today_bbo.ts_ns >= event.trigger_time_ns
        post_mid = today_bbo.mid[after]
        # Stop structure = gap extends past today's open by the buffer (continuation).
        if event.direction > 0:  # fade up (gap down): stop if price keeps falling
            reverted = bool(np.any(post_mid <= event.opening_range_low)) if len(post_mid) else False
        else:  # fade down (gap up): stop if price keeps rising
            reverted = bool(np.any(post_mid >= event.opening_range_high)) if len(post_mid) else False
        gross = eval_row.get("return_30m")
        net = (float(gross) - cost_pts) if gross is not None else None
        row: dict[str, object] = {
            "contract": f"{txf_contract}->{tmf_contract}",
            "date": date,
            "prior_date": prior_date,
            "regime_type": event.regime_type,
            "trigger_time": event.trigger_time,
            "direction": event.direction,
            "txf_entry_ref": event.txf_entry_ref,
            "gap_pts": event.realized_vol_ratio,
            "gap_stop_high": event.opening_range_high,
            "gap_stop_low": event.opening_range_low,
            "trade_vwap": event.trade_vwap,
            "stop_structure_breached": reverted,
            "gap_extended_past_stop": reverted,
            "cost_pts": cost_pts,
            "net_after_cost_30m": net,
            "net_30m_pts": gross,
            **eval_row,
        }
        rows.append(row)
    return rows


def _consecutive_gap_triples(raw_dir: Path, months: Iterable[str]) -> list[tuple[Path, Path, Path]]:
    """Yield (prior_txf, today_txf, today_tmf) for each consecutive available
    same-contract session pair where today's TMF leg also exists."""
    triples: list[tuple[Path, Path, Path]] = []
    for month in months:
        txf = f"TXF{month}"
        tmf = f"TMF{month}"
        txf_files = {_date_from_path(p): p for p in sorted((raw_dir / txf.lower()).glob(f"{txf}_*_l2.hftbt.npz"))}
        tmf_files = {_date_from_path(p): p for p in sorted((raw_dir / tmf.lower()).glob(f"{tmf}_*_l2.hftbt.npz"))}
        dates = sorted(txf_files)
        for i in range(1, len(dates)):
            prior_d, today_d = dates[i - 1], dates[i]
            if today_d in tmf_files:
                triples.append((txf_files[prior_d], txf_files[today_d], tmf_files[today_d]))
    return triples


def summarize_open_gap_fade_rows(
    rows: Sequence[dict[str, object]],
    *,
    audited_dates: Sequence[str] | None = None,
    oos_start: str | None = None,
    edge_floor_pts: float = 10.0,
    min_events: int = 80,
    min_trading_days: int = 20,
    required_contracts: Sequence[str] = ("TXFB6", "TXFC6", "TXFD6", "TXFE6"),
    h9_stop_breach_baseline: float = 0.50,
    max_single_day_share: float = 0.50,
) -> dict[str, object]:
    full = _subset_scorecard(rows)
    splits: dict[str, object] = {"full": full}
    if oos_start is not None:
        in_sample = [r for r in rows if str(r["date"]) < oos_start]
        out_sample = [r for r in rows if str(r["date"]) >= oos_start]
        splits["in_sample"] = _subset_scorecard(in_sample)
        splits["out_of_sample"] = _subset_scorecard(out_sample)

    def _f(value: object) -> float | None:
        return None if value is None else float(cast(float, value))

    audited_unique = sorted(set(audited_dates or [str(r["date"]) for r in rows]))
    contracts_present = {str(c) for c in cast("list[str]", full["contracts"])}
    cross_contract_complete = set(required_contracts).issubset(contracts_present)

    n_events = cast(int, full["events"])
    mean_net_edge = _f(full["mean_net_edge_pts_per_trade"])
    median_net = _f(full["median_net_after_cost_30m"])
    remove_best = _f(full["remove_best_1_median_net"])
    p10 = _f(full["p10_net_after_cost_30m"])
    stop_breach = _f(full["stop_breach_rate"])
    day_share = _f(full["max_single_day_net_share_of_positive"])
    contract_share = _f(full["max_single_contract_net_share_of_positive"])
    drawdown_within_monthly_gate = cast(
        bool | None,
        full["drawdown_within_2x_average_monthly_net_pnl"],
    )

    sample_ok = (
        n_events >= min_events
        and len(audited_unique) >= min_trading_days
        and cross_contract_complete
    )
    dominance_fail = (day_share is not None and day_share > max_single_day_share) or (
        contract_share is not None and contract_share >= 0.999
    )
    stop_breach_fail = stop_breach is not None and stop_breach >= h9_stop_breach_baseline
    drawdown_fail = drawdown_within_monthly_gate is False
    edge_floor_cleared = bool(mean_net_edge is not None and mean_net_edge > edge_floor_pts)

    if median_net is not None and median_net <= 0:
        verdict = "KILL"
    elif not sample_ok:
        verdict = "NEEDS-MORE-DAYS"
    elif dominance_fail or stop_breach_fail or drawdown_fail:
        verdict = "KILL"
    elif median_net is not None and median_net > 0:
        verdict = "PROCEED"
    else:
        verdict = "NEEDS-MORE-DAYS"

    research_decision = _t1b_research_decision(
        median_net=median_net,
        sample_ok=sample_ok,
        n_events=n_events,
        min_events=min_events,
        audited_days=len(audited_unique),
        min_trading_days=min_trading_days,
        cross_contract_complete=cross_contract_complete,
        dominance_fail=dominance_fail,
        stop_breach_fail=stop_breach_fail,
        drawdown_fail=drawdown_fail,
        edge_floor_cleared=edge_floor_cleared,
    )

    return {
        "track": "T1-E: TXF Open-Gap Overreaction Fade -> TMF",
        "candidate": "t1e_txf_opengap_fade_tmf",
        "audited_trading_days": len(audited_unique),
        "edge_floor_pts": edge_floor_pts,
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "edge_floor_cleared": edge_floor_cleared,
        "verdict": verdict,
        "research_decision": research_decision,
        "hard_gate": {
            "min_events": min_events,
            "events": n_events,
            "events_ok": bool(n_events >= min_events),
            "min_trading_days": min_trading_days,
            "trading_days_ok": bool(len(audited_unique) >= min_trading_days),
            "cross_contract_complete": bool(cross_contract_complete),
            "required_contracts": list(required_contracts),
            "median_net_positive": bool(median_net is not None and median_net > 0),
            "remove_best_1_non_collapsing": (bool(remove_best >= 0) if remove_best is not None else None),
            "p10_not_catastrophic": (bool(p10 > -3.0 * edge_floor_pts) if p10 is not None else None),
            "no_single_day_dominance": bool(day_share is None or day_share <= max_single_day_share),
            "no_single_contract_concentration": bool(contract_share is None or contract_share < 0.999),
            "stop_breach_below_h9_baseline": bool(stop_breach is None or stop_breach < h9_stop_breach_baseline),
            "drawdown_within_2x_average_monthly_net_pnl": drawdown_within_monthly_gate,
        },
        "splits": splits,
    }


def run_open_gap_fade_audit(args: argparse.Namespace) -> dict[str, object]:
    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    triples = _consecutive_gap_triples(raw_dir, args.months.split(","))
    if args.max_date is not None:
        triples = [t for t in triples if _date_from_path(t[1]) <= args.max_date]
    if args.min_date is not None:
        triples = [t for t in triples if _date_from_path(t[1]) >= args.min_date]
    if args.max_pairs is not None:
        triples = triples[: args.max_pairs]
    audited_dates = sorted({_date_from_path(t[1]) for t in triples})
    print(f"t1e_audit_start triples={len(triples)} months={args.months}", file=sys.stderr, flush=True)
    for idx, (prior_txf, today_txf, today_tmf) in enumerate(triples, start=1):
        started = time_module.monotonic()
        before = len(rows)
        rows.extend(
            audit_open_gap_fade_pair(
                prior_txf_path=prior_txf,
                today_txf_path=today_txf,
                today_tmf_path=today_tmf,
                session_tz_offset_hours=args.session_tz_offset_hours,
                cost_pts=args.cost_pts,
                session_minutes=args.session_minutes,
                prior_close_window_minutes=args.prior_close_window_minutes,
                open_confirm_minutes=args.open_confirm_minutes,
                min_gap_pts=args.min_gap_pts,
                stop_buffer_pts=args.stop_buffer_pts,
            )
        )
        elapsed = time_module.monotonic() - started
        print(
            f"t1e_triple_done {idx}/{len(triples)} today={today_txf.name} "
            f"events={len(rows) - before} elapsed_s={elapsed:.2f}",
            file=sys.stderr,
            flush=True,
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"{stamp}_open_gap_fade_events.csv"
    json_path = out_dir / f"{stamp}_summary.json"
    if rows:
        fieldnames = list(rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")
    summary = summarize_open_gap_fade_rows(
        rows,
        audited_dates=audited_dates,
        oos_start=args.oos_start,
        edge_floor_pts=args.edge_floor_pts,
    )
    summary["summary_path"] = str(json_path)
    summary["csv_path"] = str(csv_path)
    summary["artifact_scope"] = "validation_summary"
    summary["definition"] = {
        "l2_alpha_restriction": (
            "L2 used only for executable bid/ask and quote sanity; entry is the "
            "endogenous prior-close -> today-open gap, faded after a confirm delay."
        ),
        "session_minutes": args.session_minutes,
        "prior_close_window_minutes": args.prior_close_window_minutes,
        "open_confirm_minutes": args.open_confirm_minutes,
        "min_gap_pts": args.min_gap_pts,
        "stop_buffer_pts": args.stop_buffer_pts,
        "cost_pts": args.cost_pts,
        "oos_start": args.oos_start,
        "min_date": args.min_date,
        "max_date": args.max_date,
        "months": args.months.split(","),
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------
# T1-F: expiration V-reversal (H3 from the 2026-06-03 paper x data menu)
#
# Mechanism (frozen V0, see research/alphas/t1f_txf_expiration_vreversal_tmf/README.md):
# on a contract's FINAL SETTLEMENT day (3rd Wednesday), index-futures basis
# convergence and arbitrage unwind tend to produce a directional thrust early in
# the session followed by a partial mean-reversion (a "V" / inverted-V) into the
# settlement.  FADE the early thrust (thrust up -> short, thrust down -> long).
# The signal is the endogenous open->thrust-window displacement on the settlement
# day only; L2 is used solely for executable TMF bid/ask + quote sanity.
#
# NOTE ON SAMPLE: this signal fires once per contract per month (one settlement
# day each).  The V0 hard gate's >=20-trading-day / >=80-event floor is therefore
# structurally bounded by how many monthly settlements the paired dataset spans.
# It is the gate's job -- not the detector's -- to render NEEDS-MORE-DAYS when the
# floor is unmet; the floor is NOT relaxed for this candidate.
# ---------------------------------------------------------------------------

# TAIFEX delivery-month letter codes: A=Jan, B=Feb, ... L=Dec.
_TAIFEX_MONTH_CODE = "ABCDEFGHIJKL"


def _third_wednesday(year: int, month: int) -> str:
    """ISO date of the 3rd Wednesday of (year, month) -- TAIFEX equity-index
    futures final settlement day."""
    first = datetime(year, month, 1)
    first_wed_offset = (2 - first.weekday()) % 7  # Mon=0 .. Wed=2
    day = 1 + first_wed_offset + 14
    return f"{year:04d}-{month:02d}-{day:02d}"


def _settlement_date_for_month_code(month_code: str) -> str | None:
    """Map a delivery-month code (e.g. ``D6``) to its final settlement date.

    Letter -> calendar month (A=Jan), single trailing digit -> year-of-decade
    (``6`` -> 2026).  Returns ``None`` for codes that do not parse so callers
    can skip rather than guess.
    """
    if len(month_code) != 2:
        return None
    letter, year_digit = month_code[0].upper(), month_code[1]
    month_idx = _TAIFEX_MONTH_CODE.find(letter)
    if month_idx < 0 or not year_digit.isdigit():
        return None
    return _third_wednesday(2020 + int(year_digit), month_idx + 1)


@dataclass(frozen=True)
class ExpirationVReversalConfig:
    session_start_ns: int                # settlement-day session start (08:45)
    session_minutes: int = 285           # 08:45-13:30 TPE settlement-day session
    thrust_window_minutes: int = 90      # measure the early directional thrust over N min
    min_thrust_pts: float = 20.0         # outsized open -> thrust-window displacement
    stop_buffer_pts: float = 15.0        # thrust-continuation stop beyond the thrust extreme
    min_window_points: int = 5


def detect_expiration_v_reversal_events(
    today_bbo: BboFrame,
    today_trades: TradeFrame,
    *,
    contract: str,
    date: str,
    config: ExpirationVReversalConfig,
) -> list[RegimeEvent]:
    """Settlement-day V-reversal fade.  Measure the open -> thrust-window
    displacement; if it is outsized, fade it (bet the thrust partially reverts
    into settlement).  Direction = -sign(displacement).  Backward-looking: the
    thrust is measured over a closed early window and entry is at the window end.

    This detector is generic thrust-fade logic; the *expiration* semantics come
    from only invoking it on settlement-day data (see ``_settlement_day_pairs``).
    """
    if len(today_bbo.ts_ns) < config.min_window_points:
        return []

    start = config.session_start_ns
    session_end = start + config.session_minutes * NS_PER_MINUTE
    open_mask = (today_bbo.ts_ns >= start) & (today_bbo.ts_ns <= session_end)
    if not np.any(open_mask):
        return []
    today_open = float(today_bbo.mid[np.flatnonzero(open_mask)[0]])

    thrust_end_ns = start + config.thrust_window_minutes * NS_PER_MINUTE
    window_mask = (today_bbo.ts_ns >= start) & (today_bbo.ts_ns <= thrust_end_ns)
    if not np.any(window_mask):
        return []
    window_mid = today_bbo.mid[window_mask]

    entry_idx = int(np.searchsorted(today_bbo.ts_ns, thrust_end_ns, side="left"))
    if entry_idx >= len(today_bbo.ts_ns) or int(today_bbo.ts_ns[entry_idx]) > session_end:
        return []
    thrust_ref = float(today_bbo.mid[entry_idx])
    displacement = thrust_ref - today_open
    if abs(displacement) < config.min_thrust_pts:
        return []
    direction = -1 if displacement > 0 else 1  # fade the thrust

    thrust_high = float(np.max(window_mid))
    thrust_low = float(np.min(window_mid))
    vwap = _trade_vwap_until(today_trades, thrust_end_ns)

    return [
        RegimeEvent(
            contract=contract,
            date=date,
            regime_type="T1-F_expiration_v_reversal",
            trigger_time=_iso_from_ns(thrust_end_ns),
            trigger_time_ns=thrust_end_ns,
            direction=direction,
            txf_entry_ref=thrust_ref,
            # Stop = thrust CONTINUES past its extreme by the buffer (no reversal).
            opening_range_high=thrust_high + config.stop_buffer_pts,
            opening_range_low=thrust_low - config.stop_buffer_pts,
            trade_vwap=vwap,
            realized_vol_ratio=float(displacement),  # signed thrust (pts)
        )
    ]


def audit_expiration_v_reversal_pair(
    *,
    settlement_txf_path: Path,
    settlement_tmf_path: Path,
    session_tz_offset_hours: int = 8,
    cost_pts: float = 8.0,
    session_minutes: int = 285,
    thrust_window_minutes: int = 90,
    min_thrust_pts: float = 20.0,
    stop_buffer_pts: float = 15.0,
) -> list[dict[str, object]]:
    date_str = _date_from_path(settlement_txf_path)
    txf_contract = settlement_txf_path.name.split("_", 1)[0]
    tmf_contract = settlement_tmf_path.name.split("_", 1)[0]
    today_bbo, today_trades = _load_frames(settlement_txf_path)
    tmf_bbo, _ = _load_frames(settlement_tmf_path)
    config = ExpirationVReversalConfig(
        session_start_ns=_session_start_ns(date_str, tz_offset_hours=session_tz_offset_hours),
        session_minutes=session_minutes,
        thrust_window_minutes=thrust_window_minutes,
        min_thrust_pts=min_thrust_pts,
        stop_buffer_pts=stop_buffer_pts,
    )
    rows: list[dict[str, object]] = []
    for event in detect_expiration_v_reversal_events(
        today_bbo,
        today_trades,
        contract=txf_contract,
        date=date_str,
        config=config,
    ):
        try:
            eval_row = evaluate_executable_returns(
                tmf_bbo,
                trigger_time_ns=event.trigger_time_ns,
                direction=event.direction,
            )
        except ValueError:
            continue
        active_end_ns = event.trigger_time_ns + 30 * NS_PER_MINUTE
        after = today_bbo.ts_ns >= event.trigger_time_ns
        active = after & (today_bbo.ts_ns <= active_end_ns)
        post_mid = today_bbo.mid[after]
        active_mid = today_bbo.mid[active]
        active_ts = today_bbo.ts_ns[active]
        # Stop structure = thrust continues past its extreme (fade refuted).
        if event.direction > 0:  # faded a down-thrust (long): stop if price keeps falling
            full_session_breached = (
                bool(np.any(post_mid <= event.opening_range_low)) if len(post_mid) else False
            )
            active_crossings = np.flatnonzero(active_mid <= event.opening_range_low)
        else:  # faded an up-thrust (short): stop if price keeps rising
            full_session_breached = (
                bool(np.any(post_mid >= event.opening_range_high)) if len(post_mid) else False
            )
            active_crossings = np.flatnonzero(active_mid >= event.opening_range_high)
        active_stop_breached = bool(len(active_crossings))
        stop_trigger_time_ns = (
            int(active_ts[int(active_crossings[0])]) if active_stop_breached else None
        )
        gross = eval_row.get("return_30m")
        time_exit_net = (float(gross) - cost_pts) if gross is not None else None
        stop_exit_gross = gross
        stop_exit_net = time_exit_net
        stop_exit_reason = "time_30m"
        stop_exit_time_ns = None
        stop_exit_price = None
        if stop_trigger_time_ns is not None:
            stop_exit_idx = int(np.searchsorted(tmf_bbo.ts_ns, stop_trigger_time_ns, side="left"))
            if stop_exit_idx < len(tmf_bbo.ts_ns) and int(tmf_bbo.ts_ns[stop_exit_idx]) <= active_end_ns:
                stop_exit_time_ns = int(tmf_bbo.ts_ns[stop_exit_idx])
                stop_exit_price = float(
                    tmf_bbo.bid[stop_exit_idx] if event.direction > 0 else tmf_bbo.ask[stop_exit_idx]
                )
                entry = float(cast(float, eval_row["tmf_executable_entry"]))
                stop_exit_gross = (stop_exit_price - entry) * event.direction
                stop_exit_net = stop_exit_gross - cost_pts
                stop_exit_reason = "thrust_continuation_stop"
            else:
                stop_exit_gross = None
                stop_exit_net = None
                stop_exit_reason = "stop_no_executable_quote"
        row: dict[str, object] = {
            "contract": f"{txf_contract}->{tmf_contract}",
            "date": date_str,
            "settlement_date": date_str,
            "regime_type": event.regime_type,
            "trigger_time": event.trigger_time,
            "direction": event.direction,
            "txf_entry_ref": event.txf_entry_ref,
            "thrust_pts": event.realized_vol_ratio,
            "thrust_stop_high": event.opening_range_high,
            "thrust_stop_low": event.opening_range_low,
            "trade_vwap": event.trade_vwap,
            "stop_structure_breached": active_stop_breached,
            "active_30m_stop_breached": active_stop_breached,
            "full_session_stop_structure_breached": full_session_breached,
            "thrust_continued_past_stop": active_stop_breached,
            "stop_trigger_time": (
                _iso_from_ns(stop_trigger_time_ns) if stop_trigger_time_ns is not None else None
            ),
            "stop_trigger_time_ns": stop_trigger_time_ns,
            "stop_exit_time": _iso_from_ns(stop_exit_time_ns) if stop_exit_time_ns is not None else None,
            "stop_exit_time_ns": stop_exit_time_ns,
            "stop_exit_price": stop_exit_price,
            "stop_exit_reason": stop_exit_reason,
            "stop_exit_gross_30m": stop_exit_gross,
            "stop_exit_net_after_cost_30m": stop_exit_net,
            "cost_pts": cost_pts,
            "net_after_cost_30m": time_exit_net,
            "time_exit_net_after_cost_30m": time_exit_net,
            "net_30m_pts": gross,
            **eval_row,
        }
        rows.append(row)
    return rows


def _settlement_day_pairs(raw_dir: Path, months: Iterable[str]) -> list[tuple[Path, Path]]:
    """Yield (settlement_txf, settlement_tmf) for each delivery-month code whose
    final settlement day (3rd Wednesday) is present in BOTH the TXF and TMF L2
    archives.  Months whose settlement falls outside the exported data simply
    do not appear -- the sample floor is enforced downstream, not faked here."""
    pairs: list[tuple[Path, Path]] = []
    for month in months:
        settle = _settlement_date_for_month_code(month)
        if settle is None:
            continue
        txf = f"TXF{month}"
        tmf = f"TMF{month}"
        txf_files = {_date_from_path(p): p for p in sorted((raw_dir / txf.lower()).glob(f"{txf}_*_l2.hftbt.npz"))}
        tmf_files = {_date_from_path(p): p for p in sorted((raw_dir / tmf.lower()).glob(f"{tmf}_*_l2.hftbt.npz"))}
        if settle in txf_files and settle in tmf_files:
            pairs.append((txf_files[settle], tmf_files[settle]))
    return pairs


def summarize_expiration_v_reversal_rows(
    rows: Sequence[dict[str, object]],
    *,
    audited_dates: Sequence[str] | None = None,
    oos_start: str | None = None,
    edge_floor_pts: float = 10.0,
    min_events: int = 80,
    min_trading_days: int = 20,
    required_contracts: Sequence[str] = ("TXFB6", "TXFC6", "TXFD6", "TXFE6"),
    h9_stop_breach_baseline: float = 0.50,
    max_single_day_share: float = 0.50,
) -> dict[str, object]:
    full = _subset_scorecard(rows)
    splits: dict[str, object] = {"full": full}
    if oos_start is not None:
        in_sample = [r for r in rows if str(r["date"]) < oos_start]
        out_sample = [r for r in rows if str(r["date"]) >= oos_start]
        splits["in_sample"] = _subset_scorecard(in_sample)
        splits["out_of_sample"] = _subset_scorecard(out_sample)

    def _f(value: object) -> float | None:
        return None if value is None else float(cast(float, value))

    audited_unique = sorted(set(audited_dates or [str(r["date"]) for r in rows]))
    contracts_present = {str(c) for c in cast("list[str]", full["contracts"])}
    cross_contract_complete = set(required_contracts).issubset(contracts_present)

    n_events = cast(int, full["events"])
    mean_net_edge = _f(full["mean_net_edge_pts_per_trade"])
    mean_stop_exit_edge = _f(full["mean_stop_exit_net_after_cost_30m"])
    median_net = _f(full["median_net_after_cost_30m"])
    remove_best = _f(full["remove_best_1_median_net"])
    p10 = _f(full["p10_net_after_cost_30m"])
    stop_breach = _f(full["stop_breach_rate"])
    day_share = _f(full["max_single_day_net_share_of_positive"])
    contract_share = _f(full["max_single_contract_net_share_of_positive"])
    drawdown_within_monthly_gate = cast(
        bool | None,
        full["drawdown_within_2x_average_monthly_net_pnl"],
    )

    sample_ok = (
        n_events >= min_events
        and len(audited_unique) >= min_trading_days
        and cross_contract_complete
    )
    dominance_fail = (day_share is not None and day_share > max_single_day_share) or (
        contract_share is not None and contract_share >= 0.999
    )
    stop_breach_fail = stop_breach is not None and stop_breach >= h9_stop_breach_baseline
    drawdown_fail = drawdown_within_monthly_gate is False
    edge_floor_cleared = bool(mean_net_edge is not None and mean_net_edge > edge_floor_pts)
    risk_controlled_edge_floor_cleared = bool(
        mean_stop_exit_edge is not None and mean_stop_exit_edge > edge_floor_pts
    )
    # The canonical metric for this track is the risk-controlled (stop-exit)
    # edge.  A computable stop-exit edge below the floor is an outright failure;
    # an uncomputable one cannot support a PROCEED claim (falls through to
    # NEEDS-MORE-DAYS).  This stops a candidate from proceeding on the legacy
    # time-exit edge alone while failing its declared canonical metric.
    risk_controlled_edge_floor_below = (
        mean_stop_exit_edge is not None and mean_stop_exit_edge <= edge_floor_pts
    )

    if median_net is not None and median_net <= 0:
        verdict = "KILL"
    elif not sample_ok:
        verdict = "NEEDS-MORE-DAYS"
    elif dominance_fail or stop_breach_fail or drawdown_fail or risk_controlled_edge_floor_below:
        verdict = "KILL"
    elif median_net is not None and median_net > 0 and risk_controlled_edge_floor_cleared:
        verdict = "PROCEED"
    else:
        verdict = "NEEDS-MORE-DAYS"

    research_decision = _t1b_research_decision(
        median_net=median_net,
        sample_ok=sample_ok,
        n_events=n_events,
        min_events=min_events,
        audited_days=len(audited_unique),
        min_trading_days=min_trading_days,
        cross_contract_complete=cross_contract_complete,
        dominance_fail=dominance_fail,
        stop_breach_fail=stop_breach_fail,
        drawdown_fail=drawdown_fail,
        edge_floor_cleared=edge_floor_cleared,
        risk_controlled_edge_floor_cleared=risk_controlled_edge_floor_cleared,
    )

    return {
        "track": "T1-F: TXF Expiration V-Reversal -> TMF",
        "candidate": "t1f_txf_expiration_vreversal_tmf",
        "audited_trading_days": len(audited_unique),
        "settlement_days_available": sorted({str(r["date"]) for r in rows}),
        "edge_floor_pts": edge_floor_pts,
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "edge_floor_cleared": edge_floor_cleared,
        "risk_controlled_edge_floor_cleared": risk_controlled_edge_floor_cleared,
        "metric_contract": {
            "legacy_time_exit_metric": "net_after_cost_30m",
            "legacy_time_exit_alias": "time_exit_net_after_cost_30m",
            "canonical_risk_controlled_metric": "stop_exit_net_after_cost_30m",
            "verdict_basis": "risk_controlled_canonical_with_legacy_compatibility",
            "promotion_interpretation": (
                "The risk-controlled (stop-exit) edge is the canonical gate for the verdict and "
                "research eligibility; the legacy time-exit median is retained only for historical "
                "artifact compatibility and cannot promote a candidate on its own."
            ),
        },
        "verdict": verdict,
        "research_decision": research_decision,
        "hard_gate": {
            "min_events": min_events,
            "events": n_events,
            "events_ok": bool(n_events >= min_events),
            "min_trading_days": min_trading_days,
            "trading_days_ok": bool(len(audited_unique) >= min_trading_days),
            "cross_contract_complete": bool(cross_contract_complete),
            "required_contracts": list(required_contracts),
            "median_net_positive": bool(median_net is not None and median_net > 0),
            "risk_controlled_mean_edge_above_floor": risk_controlled_edge_floor_cleared,
            "remove_best_1_non_collapsing": (bool(remove_best >= 0) if remove_best is not None else None),
            "p10_not_catastrophic": (bool(p10 > -3.0 * edge_floor_pts) if p10 is not None else None),
            "no_single_day_dominance": bool(day_share is None or day_share <= max_single_day_share),
            "no_single_contract_concentration": bool(contract_share is None or contract_share < 0.999),
            "stop_breach_below_h9_baseline": bool(stop_breach is None or stop_breach < h9_stop_breach_baseline),
            "drawdown_within_2x_average_monthly_net_pnl": drawdown_within_monthly_gate,
        },
        "splits": splits,
    }


def run_expiration_v_reversal_audit(args: argparse.Namespace) -> dict[str, object]:
    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    pairs = _settlement_day_pairs(raw_dir, args.months.split(","))
    if args.max_date is not None:
        pairs = [p for p in pairs if _date_from_path(p[0]) <= args.max_date]
    if args.min_date is not None:
        pairs = [p for p in pairs if _date_from_path(p[0]) >= args.min_date]
    if args.max_pairs is not None:
        pairs = pairs[: args.max_pairs]
    audited_dates = sorted({_date_from_path(txf) for txf, _ in pairs})
    print(f"t1f_audit_start settlement_pairs={len(pairs)} months={args.months}", file=sys.stderr, flush=True)
    for idx, (settlement_txf, settlement_tmf) in enumerate(pairs, start=1):
        started = time_module.monotonic()
        before = len(rows)
        rows.extend(
            audit_expiration_v_reversal_pair(
                settlement_txf_path=settlement_txf,
                settlement_tmf_path=settlement_tmf,
                session_tz_offset_hours=args.session_tz_offset_hours,
                cost_pts=args.cost_pts,
                thrust_window_minutes=args.thrust_window_minutes,
                min_thrust_pts=args.min_thrust_pts,
                stop_buffer_pts=args.stop_buffer_pts,
            )
        )
        elapsed = time_module.monotonic() - started
        print(
            f"t1f_settlement_done {idx}/{len(pairs)} day={settlement_txf.name} "
            f"events={len(rows) - before} elapsed_s={elapsed:.2f}",
            file=sys.stderr,
            flush=True,
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"{stamp}_expiration_v_reversal_events.csv"
    json_path = out_dir / f"{stamp}_summary.json"
    if rows:
        fieldnames = list(rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")
    summary = summarize_expiration_v_reversal_rows(
        rows,
        audited_dates=audited_dates,
        oos_start=args.oos_start,
        edge_floor_pts=args.edge_floor_pts,
    )
    summary["summary_path"] = str(json_path)
    summary["csv_path"] = str(csv_path)
    summary["artifact_scope"] = "validation_summary"
    summary["definition"] = {
        "l2_alpha_restriction": (
            "L2 used only for executable bid/ask and quote sanity; entry is the "
            "endogenous open -> thrust-window displacement on the settlement day, "
            "faded at the thrust-window end."
        ),
        "settlement_day_only": True,
        "session_minutes": 285,
        "thrust_window_minutes": args.thrust_window_minutes,
        "min_thrust_pts": args.min_thrust_pts,
        "stop_buffer_pts": args.stop_buffer_pts,
        "active_stop_horizon_minutes": 30,
        "time_exit_metric": "time_exit_net_after_cost_30m",
        "risk_controlled_metric": "stop_exit_net_after_cost_30m",
        "cost_pts": args.cost_pts,
        "oos_start": args.oos_start,
        "min_date": args.min_date,
        "max_date": args.max_date,
        "months": args.months.split(","),
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------
# T1-C: VWAP-trend / session-imbalance failed-reclaim continuation
#
# Mechanism (frozen V0, see research/alphas/t1c_txf_vwaptrend_tmf/README.md):
# the third frozen Track-T1 candidate (see track_t1_opened_2026_05_13).  Within
# the day session, slide an anchor; at each anchor take the cumulative session
# trade VWAP as the fair-value reference.  A directional *session imbalance*
# exists when the TXF mid has stayed predominantly on one side of VWAP across a
# trailing window AND sits >= ``min_trend_pts`` away from it.  A *failed VWAP
# reclaim* is a pullback that approached VWAP (within ``reclaim_tolerance_pts``)
# but did NOT cross to the other side.  Enter in the TREND direction
# (continuation): mid above VWAP -> long, below -> short.  Stop structure = VWAP
# reclaim (price crosses back through VWAP by ``stop_buffer_pts``).  L2 is NOT an
# entry input -- only TXF mid + trade VWAP drive the signal and TMF bid/ask
# provides executable fills.
#
# NB: ``RegimeEvent.opening_range_high``/``opening_range_low`` are reused to
# carry the VWAP-reclaim stop band (vwap +/- buffer); ``trade_vwap`` carries the
# anchor VWAP level; ``realized_vol_ratio`` carries the signed displacement from
# VWAP (the imbalance magnitude, pts).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VwapTrendConfig:
    session_start_ns: int
    session_minutes: int = 300           # 08:45-13:45 TPE day session
    trend_window_minutes: int = 60       # trailing window establishing the imbalance
    headline_horizon_minutes: int = 30   # last anchor leaves room for the 30m hold
    min_trend_pts: float = 15.0          # mid must sit >= this far from VWAP
    min_side_fraction: float = 0.80      # >= this fraction of the window on the trend side
    reclaim_tolerance_pts: float = 5.0   # pullback must reach within this of VWAP, no cross
    stop_buffer_pts: float = 15.0        # VWAP-reclaim stop band beyond VWAP
    step_minutes: int = 5                # anchor slide granularity
    cooldown_minutes: int = 60           # no overlapping entries (max hold horizon)
    min_window_points: int = 5


def detect_vwap_trend_events(
    bbo: BboFrame,
    trades: TradeFrame,
    *,
    contract: str,
    date: str,
    config: VwapTrendConfig,
) -> list[RegimeEvent]:
    """Session-imbalance failed-VWAP-reclaim continuation.  Backward-looking:
    the imbalance and the failed reclaim are measured over a trailing window
    that closes at the anchor, and entry is at the anchor in the trend
    direction.  Edge source is VWAP-trend persistence, not microstructure.
    """
    if len(bbo.ts_ns) < config.min_window_points:
        return []

    start = config.session_start_ns
    session_end = start + config.session_minutes * NS_PER_MINUTE
    trend_ns = config.trend_window_minutes * NS_PER_MINUTE
    step_ns = config.step_minutes * NS_PER_MINUTE
    cooldown_ns = config.cooldown_minutes * NS_PER_MINUTE
    last_anchor = session_end - config.headline_horizon_minutes * NS_PER_MINUTE

    ts = bbo.ts_ns
    mid = bbo.mid
    events: list[RegimeEvent] = []
    cooldown_until = start
    anchor = start + trend_ns

    while anchor <= last_anchor:
        if anchor < cooldown_until:
            anchor += step_ns
            continue

        vwap = _trade_vwap_until(trades, anchor)
        if vwap is None:
            anchor += step_ns
            continue

        entry_idx = int(np.searchsorted(ts, anchor, side="left"))
        if entry_idx >= len(ts):
            break
        entry_ref = float(mid[entry_idx])
        displacement = entry_ref - vwap
        if abs(displacement) < config.min_trend_pts:
            anchor += step_ns
            continue
        direction = 1 if displacement > 0 else -1

        window_mask = (ts >= anchor - trend_ns) & (ts <= anchor)
        if int(np.count_nonzero(window_mask)) < config.min_window_points:
            anchor += step_ns
            continue
        window_mid = mid[window_mask]

        signed = direction * (window_mid - vwap)  # > 0 == on the trend side of VWAP
        side_fraction = float(np.count_nonzero(signed > 0)) / float(len(window_mid))
        if side_fraction < config.min_side_fraction:
            anchor += step_ns
            continue

        # Failed VWAP reclaim: a pullback that touched within tolerance of VWAP
        # but never crossed past it onto the counter-trend side.
        approached = bool(np.min(np.abs(window_mid - vwap)) <= config.reclaim_tolerance_pts)
        not_crossed = bool(float(np.min(signed)) >= -config.reclaim_tolerance_pts)
        if not (approached and not_crossed):
            anchor += step_ns
            continue

        events.append(
            RegimeEvent(
                contract=contract,
                date=date,
                regime_type="T1-C_vwap_trend_continuation",
                trigger_time=_iso_from_ns(anchor),
                trigger_time_ns=anchor,
                direction=direction,
                txf_entry_ref=entry_ref,
                # Stop = price reclaims VWAP (crosses back through it by the buffer).
                opening_range_high=vwap + config.stop_buffer_pts,
                opening_range_low=vwap - config.stop_buffer_pts,
                trade_vwap=vwap,
                realized_vol_ratio=float(displacement),  # signed VWAP displacement (pts)
            )
        )
        cooldown_until = anchor + cooldown_ns
        anchor += step_ns

    return events


def audit_vwap_trend_pair(
    *,
    txf_path: Path,
    tmf_path: Path,
    session_tz_offset_hours: int = 8,
    cost_pts: float = 8.0,
    session_minutes: int = 300,
    trend_window_minutes: int = 60,
    min_trend_pts: float = 15.0,
    min_side_fraction: float = 0.80,
    reclaim_tolerance_pts: float = 5.0,
    stop_buffer_pts: float = 15.0,
    step_minutes: int = 5,
    cooldown_minutes: int = 60,
) -> list[dict[str, object]]:
    date = _date_from_path(txf_path)
    txf_contract = txf_path.name.split("_", 1)[0]
    tmf_contract = tmf_path.name.split("_", 1)[0]
    txf_bbo, txf_trades = _load_frames(txf_path)
    tmf_bbo, _ = _load_frames(tmf_path)
    config = VwapTrendConfig(
        session_start_ns=_session_start_ns(date, tz_offset_hours=session_tz_offset_hours),
        session_minutes=session_minutes,
        trend_window_minutes=trend_window_minutes,
        min_trend_pts=min_trend_pts,
        min_side_fraction=min_side_fraction,
        reclaim_tolerance_pts=reclaim_tolerance_pts,
        stop_buffer_pts=stop_buffer_pts,
        step_minutes=step_minutes,
        cooldown_minutes=cooldown_minutes,
    )
    rows: list[dict[str, object]] = []
    for event in detect_vwap_trend_events(
        txf_bbo,
        txf_trades,
        contract=txf_contract,
        date=date,
        config=config,
    ):
        try:
            eval_row = evaluate_executable_returns(
                tmf_bbo,
                trigger_time_ns=event.trigger_time_ns,
                direction=event.direction,
            )
        except ValueError:
            # No TMF executable quote at/after the trigger -> not tradeable; skip.
            continue
        after = txf_bbo.ts_ns >= event.trigger_time_ns
        post_mid = txf_bbo.mid[after]
        # Stop structure = VWAP reclaim (price crosses back through VWAP by the buffer).
        if event.direction > 0:  # long (mid above VWAP): stop if price reclaims downward
            reverted = bool(np.any(post_mid <= event.opening_range_low)) if len(post_mid) else False
        else:  # short (mid below VWAP): stop if price reclaims upward
            reverted = bool(np.any(post_mid >= event.opening_range_high)) if len(post_mid) else False
        gross = eval_row.get("return_30m")
        net = (float(gross) - cost_pts) if gross is not None else None
        row: dict[str, object] = {
            "contract": f"{txf_contract}->{tmf_contract}",
            "date": date,
            "regime_type": event.regime_type,
            "trigger_time": event.trigger_time,
            "direction": event.direction,
            "txf_entry_ref": event.txf_entry_ref,
            "vwap_displacement_pts": event.realized_vol_ratio,
            "trade_vwap": event.trade_vwap,
            "vwap_stop_high": event.opening_range_high,
            "vwap_stop_low": event.opening_range_low,
            # Entry-time setup is by construction a FAILED reclaim; post-entry the
            # stop fires iff price later RECLAIMS VWAP (continuation refuted).
            "vwap_reclaim_failed_or_passed": "failed",
            "stop_structure_breached": reverted,
            "vwap_reclaimed_post_entry": reverted,
            "cost_pts": cost_pts,
            "net_after_cost_30m": net,
            "net_30m_pts": gross,
            **eval_row,
        }
        rows.append(row)
    return rows


def summarize_vwap_trend_rows(
    rows: Sequence[dict[str, object]],
    *,
    audited_dates: Sequence[str] | None = None,
    oos_start: str | None = None,
    edge_floor_pts: float = 10.0,
    min_events: int = 80,
    min_trading_days: int = 20,
    required_contracts: Sequence[str] = ("TXFB6", "TXFC6", "TXFD6", "TXFE6"),
    h9_stop_breach_baseline: float = 0.50,
    max_single_day_share: float = 0.50,
) -> dict[str, object]:
    full = _subset_scorecard(rows)
    splits: dict[str, object] = {"full": full}
    if oos_start is not None:
        in_sample = [r for r in rows if str(r["date"]) < oos_start]
        out_sample = [r for r in rows if str(r["date"]) >= oos_start]
        splits["in_sample"] = _subset_scorecard(in_sample)
        splits["out_of_sample"] = _subset_scorecard(out_sample)

    def _f(value: object) -> float | None:
        return None if value is None else float(cast(float, value))

    audited_unique = sorted(set(audited_dates or [str(r["date"]) for r in rows]))
    contracts_present = {str(c) for c in cast("list[str]", full["contracts"])}
    cross_contract_complete = set(required_contracts).issubset(contracts_present)

    n_events = cast(int, full["events"])
    mean_net_edge = _f(full["mean_net_edge_pts_per_trade"])
    median_net = _f(full["median_net_after_cost_30m"])
    remove_best = _f(full["remove_best_1_median_net"])
    p10 = _f(full["p10_net_after_cost_30m"])
    stop_breach = _f(full["stop_breach_rate"])
    day_share = _f(full["max_single_day_net_share_of_positive"])
    contract_share = _f(full["max_single_contract_net_share_of_positive"])
    drawdown_within_monthly_gate = cast(
        bool | None,
        full["drawdown_within_2x_average_monthly_net_pnl"],
    )

    sample_ok = (
        n_events >= min_events
        and len(audited_unique) >= min_trading_days
        and cross_contract_complete
    )
    dominance_fail = (day_share is not None and day_share > max_single_day_share) or (
        contract_share is not None and contract_share >= 0.999
    )
    stop_breach_fail = stop_breach is not None and stop_breach >= h9_stop_breach_baseline
    drawdown_fail = drawdown_within_monthly_gate is False
    edge_floor_cleared = bool(mean_net_edge is not None and mean_net_edge > edge_floor_pts)

    if median_net is not None and median_net <= 0:
        verdict = "KILL"
    elif not sample_ok:
        verdict = "NEEDS-MORE-DAYS"
    elif dominance_fail or stop_breach_fail or drawdown_fail:
        verdict = "KILL"
    elif median_net is not None and median_net > 0:
        verdict = "PROCEED"
    else:
        verdict = "NEEDS-MORE-DAYS"

    research_decision = _t1b_research_decision(
        median_net=median_net,
        sample_ok=sample_ok,
        n_events=n_events,
        min_events=min_events,
        audited_days=len(audited_unique),
        min_trading_days=min_trading_days,
        cross_contract_complete=cross_contract_complete,
        dominance_fail=dominance_fail,
        stop_breach_fail=stop_breach_fail,
        drawdown_fail=drawdown_fail,
        edge_floor_cleared=edge_floor_cleared,
    )

    return {
        "track": "T1-C: TXF VWAP-Trend Session Imbalance -> TMF",
        "candidate": "t1c_txf_vwaptrend_tmf",
        "audited_trading_days": len(audited_unique),
        "edge_floor_pts": edge_floor_pts,
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "edge_floor_cleared": edge_floor_cleared,
        "verdict": verdict,
        "research_decision": research_decision,
        "hard_gate": {
            "min_events": min_events,
            "events": n_events,
            "events_ok": bool(n_events >= min_events),
            "min_trading_days": min_trading_days,
            "trading_days_ok": bool(len(audited_unique) >= min_trading_days),
            "cross_contract_complete": bool(cross_contract_complete),
            "required_contracts": list(required_contracts),
            "median_net_positive": bool(median_net is not None and median_net > 0),
            "remove_best_1_non_collapsing": (bool(remove_best >= 0) if remove_best is not None else None),
            "p10_not_catastrophic": (bool(p10 > -3.0 * edge_floor_pts) if p10 is not None else None),
            "no_single_day_dominance": bool(day_share is None or day_share <= max_single_day_share),
            "no_single_contract_concentration": bool(contract_share is None or contract_share < 0.999),
            "stop_breach_below_h9_baseline": bool(stop_breach is None or stop_breach < h9_stop_breach_baseline),
            "drawdown_within_2x_average_monthly_net_pnl": drawdown_within_monthly_gate,
        },
        "splits": splits,
    }


def run_vwap_trend_audit(args: argparse.Namespace) -> dict[str, object]:
    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    pairs = _matching_pairs(raw_dir, args.months.split(","))
    if args.max_date is not None:
        pairs = [(t, m) for (t, m) in pairs if _date_from_path(t) <= args.max_date]
    if args.min_date is not None:
        pairs = [(t, m) for (t, m) in pairs if _date_from_path(t) >= args.min_date]
    if args.max_pairs is not None:
        pairs = pairs[: args.max_pairs]
    audited_dates = sorted({_date_from_path(t) for t, _ in pairs})
    print(f"t1c_audit_start pairs={len(pairs)} months={args.months}", file=sys.stderr, flush=True)
    for idx, (txf_path, tmf_path) in enumerate(pairs, start=1):
        started = time_module.monotonic()
        before = len(rows)
        rows.extend(
            audit_vwap_trend_pair(
                txf_path=txf_path,
                tmf_path=tmf_path,
                session_tz_offset_hours=args.session_tz_offset_hours,
                cost_pts=args.cost_pts,
                session_minutes=args.session_minutes,
                trend_window_minutes=args.trend_window_minutes,
                min_trend_pts=args.min_trend_pts,
                min_side_fraction=args.min_side_fraction,
                reclaim_tolerance_pts=args.reclaim_tolerance_pts,
                stop_buffer_pts=args.stop_buffer_pts,
                step_minutes=args.step_minutes,
                cooldown_minutes=args.cooldown_minutes,
            )
        )
        elapsed = time_module.monotonic() - started
        print(
            f"t1c_pair_done {idx}/{len(pairs)} txf={txf_path.name} events={len(rows) - before} elapsed_s={elapsed:.2f}",
            file=sys.stderr,
            flush=True,
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"{stamp}_vwap_trend_events.csv"
    json_path = out_dir / f"{stamp}_summary.json"
    if rows:
        fieldnames = list(rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")
    summary = summarize_vwap_trend_rows(
        rows,
        audited_dates=audited_dates,
        oos_start=args.oos_start,
        edge_floor_pts=args.edge_floor_pts,
    )
    summary["summary_path"] = str(json_path)
    summary["csv_path"] = str(csv_path)
    summary["artifact_scope"] = "validation_summary"
    summary["definition"] = {
        "l2_alpha_restriction": (
            "L2 used only for executable bid/ask and quote sanity; entry is a TXF "
            "session VWAP-trend imbalance with a failed VWAP reclaim, traded in the "
            "trend direction (continuation)."
        ),
        "session_minutes": args.session_minutes,
        "trend_window_minutes": args.trend_window_minutes,
        "min_trend_pts": args.min_trend_pts,
        "min_side_fraction": args.min_side_fraction,
        "reclaim_tolerance_pts": args.reclaim_tolerance_pts,
        "stop_buffer_pts": args.stop_buffer_pts,
        "step_minutes": args.step_minutes,
        "cooldown_minutes": args.cooldown_minutes,
        "cost_pts": args.cost_pts,
        "oos_start": args.oos_start,
        "min_date": args.min_date,
        "max_date": args.max_date,
        "months": args.months.split(","),
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


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
    parser.add_argument(
        "--mode",
        choices=(
            "viability",
            "coverage",
            "vol_compression",
            "intraday_momentum",
            "open_gap_fade",
            "expiration_v_reversal",
            "vwap_trend",
        ),
        default="viability",
    )
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
    # T1-B vol-compression mode (frozen V0 defaults).
    parser.add_argument("--session-minutes", type=int, default=300)
    parser.add_argument("--baseline-minutes", type=int, default=30)
    parser.add_argument("--compression-minutes", type=int, default=30)
    parser.add_argument("--break-window-minutes", type=int, default=30)
    parser.add_argument("--step-minutes", type=int, default=5)
    parser.add_argument("--max-compression-ratio", type=float, default=0.70)
    parser.add_argument("--cooldown-minutes", type=int, default=60)
    # T1-D intraday-session-momentum mode (frozen V0 defaults).
    parser.add_argument("--open-window-minutes", type=int, default=30)
    parser.add_argument("--predict-window-minutes", type=int, default=30)
    parser.add_argument("--min-open-move-pts", type=float, default=10.0)
    # T1-E open-gap-fade mode (frozen V0 defaults).
    parser.add_argument("--prior-close-window-minutes", type=int, default=30)
    parser.add_argument("--open-confirm-minutes", type=int, default=15)
    parser.add_argument("--min-gap-pts", type=float, default=15.0)
    parser.add_argument("--stop-buffer-pts", type=float, default=15.0)
    # T1-F expiration-V-reversal mode (frozen V0 defaults).
    parser.add_argument("--thrust-window-minutes", type=int, default=90)
    parser.add_argument("--min-thrust-pts", type=float, default=20.0)
    # T1-C VWAP-trend mode (frozen V0 defaults).
    parser.add_argument("--trend-window-minutes", type=int, default=60)
    parser.add_argument("--min-trend-pts", type=float, default=15.0)
    parser.add_argument("--min-side-fraction", type=float, default=0.80)
    parser.add_argument("--reclaim-tolerance-pts", type=float, default=5.0)
    parser.add_argument("--cost-pts", type=float, default=8.0)
    parser.add_argument("--edge-floor-pts", type=float, default=10.0)
    parser.add_argument("--oos-start", default=None, help="ISO date; events on/after are out-of-sample")
    parser.add_argument("--min-date", default=None, help="ISO date; include trading days >= this")
    parser.add_argument("--max-date", default=None, help="ISO date; include trading days <= this")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "vol_compression":
        summary = run_vol_compression_audit(args)
    elif args.mode == "intraday_momentum":
        summary = run_intraday_momentum_audit(args)
    elif args.mode == "open_gap_fade":
        summary = run_open_gap_fade_audit(args)
    elif args.mode == "expiration_v_reversal":
        summary = run_expiration_v_reversal_audit(args)
    elif args.mode == "vwap_trend":
        summary = run_vwap_trend_audit(args)
    elif args.mode == "coverage":
        summary = run_coverage_audit(args)
    else:
        summary = run_audit(args)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
