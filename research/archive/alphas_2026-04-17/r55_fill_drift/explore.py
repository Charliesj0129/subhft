#!/usr/bin/env python3
"""R55 Fill Drift — Post-Fill Price Path Exploration.

Measures post-fill price paths for R47 maker fills to identify conditioning
features that predict adverse drift (trend-through) vs V-shape recovery.

T4 exploration only — measures the signal, does NOT build a classifier.

Usage:
    uv run python research/alphas/r55_fill_drift/explore.py --password changeme
    uv run python research/alphas/r55_fill_drift/explore.py --password changeme --symbol TXFD6
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

os.environ.setdefault("HFT_STRICT_PRICE_MODE", "0")
_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "src"))

import structlog  # noqa: E402

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging  # noqa: E402

logging.disable(logging.WARNING)

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus  # noqa: E402
from hft_platform.contracts.strategy import IntentType, OrderIntent, Side  # noqa: E402
from hft_platform.events import (  # noqa: E402
    FeatureUpdateEvent,
    LOBStatsEvent,
    MetaData,
    TickEvent,
)
from hft_platform.strategies.r47_maker import R47MakerStrategy  # noqa: E402
from hft_platform.strategy.base import StrategyContext  # noqa: E402

# ── Constants ───────────────────────────────────────────────────────────

CK_SCALE = 1_000_000  # ClickHouse golden data scale (x1e6)
PLATFORM_SCALE = 10_000  # Platform price scale (x10000)
SCALE_RATIO = CK_SCALE // PLATFORM_SCALE  # 100

POINT_VALUE_NTD = 10
FEE_PER_SIDE_NTD = 20  # commission 13 + tax 7
FEE_RT_PTS = 2 * FEE_PER_SIDE_NTD / POINT_VALUE_NTD  # 4.0 pts

DEFAULT_LATENCY_NS = 36_000_000  # 36ms
DEFAULT_QUEUE_FRAC = 0.5
DEFAULT_SPREAD_THRESHOLD = 5
DEFAULT_QI_SKEW = 0.10
DEFAULT_QI_WIDEN = 1

# Feature indices (lob_shared_v3)
_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9
_IDX_L1_IMBALANCE_PPM = 10

# Post-fill measurement horizons (in ticks)
DRIFT_HORIZONS = (5, 10, 20, 30, 50, 100)
MAE_HORIZON = 30  # max adverse excursion window


# ── Data Structures ─────────────────────────────────────────────────────


@dataclass
class PendingOrder:
    """Order in the latency pipeline."""

    order_id: str
    side: Side
    price: int  # platform scale (x10000)
    qty: int
    submit_ts: int
    active_ts: int  # submit_ts + latency_ns
    queue_ahead: int
    is_active: bool = False
    cancel_requested_ts: int | None = None
    cancel_complete_ts: int | None = None
    depth_at_place: int = 0


@dataclass
class FillRecord:
    """Record of a single fill with conditioning features and post-fill paths."""

    # Identity
    fill_id: int
    date: str
    fill_ts: int  # nanosecond timestamp
    side: str  # "BUY" or "SELL"
    fill_price_pts: float  # in points

    # Conditioning features at fill time
    spread_at_fill: float  # ask - bid in points
    signed_flow_10tick: int  # sum of last 10 trade directions
    book_imbalance_l1: float  # bid_qty / (bid_qty + ask_qty)

    # Mid price at fill time (points)
    mid_at_fill_pts: float

    # Post-fill drift at each horizon (signed: positive = favorable)
    drift: dict = field(default_factory=dict)  # horizon -> drift in points

    # Max adverse excursion within MAE_HORIZON ticks (positive = adverse magnitude)
    mae_30: float = 0.0


# ── Price Conversion ────────────────────────────────────────────────────


def _ck_price_to_platform(ck_price: int) -> int:
    """Convert CK golden scale (x1e6) to platform scale (x10000)."""
    return ck_price // SCALE_RATIO


def _platform_price_to_pts(platform_price: int) -> float:
    """Convert platform scale (x10000) to points."""
    return platform_price / PLATFORM_SCALE


def _ck_price_to_pts(ck_price: int) -> float:
    """Convert CK golden scale (x1e6) to points."""
    return ck_price / CK_SCALE


# ── CK Data Loading ─────────────────────────────────────────────────────


def _ck_query(url: str, password: str, sql: str) -> str:
    import requests

    resp = requests.post(
        url,
        params={"user": "default", "password": password},
        data=sql,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.text.strip()


def _ck_query_numpy(url: str, password: str, sql: str) -> dict[str, np.ndarray]:
    raw = _ck_query(url, password, sql + " FORMAT TSVWithNames")
    lines = raw.split("\n")
    if len(lines) < 2:
        return {}
    headers = lines[0].split("\t")
    rows = [line.split("\t") for line in lines[1:] if line]
    cols: dict[str, np.ndarray] = {}
    for i, h in enumerate(headers):
        col_vals = [r[i] for r in rows]
        try:
            cols[h] = np.array(col_vals, dtype=np.int64)
        except ValueError:
            try:
                cols[h] = np.array(col_vals, dtype=np.float64)
            except ValueError:
                cols[h] = np.array(col_vals)
    return cols


def get_trading_days(url: str, password: str, symbol: str) -> list[str]:
    sql = f"""
    SELECT DISTINCT toDate(fromUnixTimestamp64Nano(exch_ts)) as d
    FROM hft.market_data
    WHERE symbol = '{symbol}' AND type IN ('BidAsk', 'Tick')
    ORDER BY d
    """
    raw = _ck_query(url, password, sql + " FORMAT TSV")
    if not raw:
        return []
    return [line.strip() for line in raw.split("\n") if line.strip()]


def load_day_ck(
    url: str, password: str, symbol: str, date: str
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Load BidAsk + Tick data for one day from ClickHouse."""
    ba_sql = f"""
    SELECT
        exch_ts,
        bids_price[1] AS bid1_p, bids_vol[1] AS bid1_v,
        asks_price[1] AS ask1_p, asks_vol[1] AS ask1_v,
        bids_price[2] AS bid2_p, bids_vol[2] AS bid2_v,
        asks_price[2] AS ask2_p, asks_vol[2] AS ask2_v,
        bids_price[3] AS bid3_p, bids_vol[3] AS bid3_v,
        asks_price[3] AS ask3_p, asks_vol[3] AS ask3_v,
        bids_price[4] AS bid4_p, bids_vol[4] AS bid4_v,
        asks_price[4] AS ask4_p, asks_vol[4] AS ask4_v,
        bids_price[5] AS bid5_p, bids_vol[5] AS bid5_v,
        asks_price[5] AS ask5_p, asks_vol[5] AS ask5_v
    FROM hft.market_data
    WHERE symbol = '{symbol}'
      AND type = 'BidAsk'
      AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
      AND length(bids_price) >= 1 AND length(asks_price) >= 1
    ORDER BY exch_ts
    """
    tick_sql = f"""
    SELECT
        exch_ts,
        price_scaled AS price,
        volume
    FROM hft.market_data
    WHERE symbol = '{symbol}'
      AND type = 'Tick'
      AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
    ORDER BY exch_ts
    """
    return (
        _ck_query_numpy(url, password, ba_sql),
        _ck_query_numpy(url, password, tick_sql),
    )


# ── Trade Direction ─────────────────────────────────────────────────────


def _infer_trade_direction(
    trade_price_ck: int, best_bid_ck: int, best_ask_ck: int
) -> int:
    """Tick rule: +1 if trade at ask (buyer-initiated), -1 at bid, 0 unknown."""
    if trade_price_ck >= best_ask_ck:
        return 1
    if trade_price_ck <= best_bid_ck:
        return -1
    mid = (best_bid_ck + best_ask_ck) // 2
    if trade_price_ck > mid:
        return 1
    if trade_price_ck < mid:
        return -1
    return 0


# ── Fill-Drift Simulation Engine ────────────────────────────────────────


def run_day_fill_drift(  # noqa: C901
    ba: dict[str, np.ndarray],
    ticks: dict[str, np.ndarray],
    date: str,
    symbol: str,
    latency_ns: int = DEFAULT_LATENCY_NS,
    queue_frac: float = DEFAULT_QUEUE_FRAC,
    spread_threshold_pts: int = DEFAULT_SPREAD_THRESHOLD,
    max_pos: int = 3,
) -> list[FillRecord]:
    """Run single-day simulation, recording fill conditioning features.

    Returns a list of FillRecord with conditioning features populated.
    Post-fill drift is measured in a second pass after all mid prices are known.
    """

    # ── Initialize strategy ──────────────────────────────────────────
    strat = R47MakerStrategy(
        strategy_id="r55_bt",
        pe_danger_threshold=0.55,
        pe_window=100,
        queue_cancel_threshold=0.7,
        mfg_skew_z_threshold=2.0,
        spread_threshold_pts=spread_threshold_pts,
        toxicity_max=700,
        qi_skew_threshold=DEFAULT_QI_SKEW,
        qi_widen_ticks=DEFAULT_QI_WIDEN,
        max_pos=max_pos,
    )

    # Strategy context with intent capture
    pos_dict: dict[str, int] = {symbol: 0}
    intent_seq = [0]
    captured_intents: list[OrderIntent] = []

    def intent_factory(**kw: object) -> OrderIntent:
        intent_seq[0] += 1
        intent = OrderIntent(intent_id=intent_seq[0], **kw)
        captured_intents.append(intent)
        return intent

    def price_scaler(s: str, p: object) -> int:
        from decimal import Decimal

        if isinstance(p, int):
            return p
        return int(Decimal(str(p)) * Decimal(str(PLATFORM_SCALE)))

    ctx = StrategyContext(
        positions=pos_dict,
        strategy_id=strat.strategy_id,
        intent_factory=intent_factory,
        price_scaler=price_scaler,
    )

    # ── Order and fill tracking ──────────────────────────────────────
    pending_orders: dict[str, PendingOrder] = {}
    order_counter = 0
    fill_counter = 0

    # Current L1 state (CK scale)
    cur_bid_ck = 0
    cur_ask_ck = 0
    cur_bid_v = 0
    cur_ask_v = 0

    # Rolling trade direction buffer for signed_flow_10tick
    recent_directions: deque[int] = deque(maxlen=10)

    # Fill records (conditioning features only; drift measured later)
    fill_records: list[FillRecord] = []

    # Mid-price timeline for post-fill drift measurement
    # We record (tick_index, mid_ck) at every tick to look up post-fill paths
    tick_mid_timeline: list[int] = []  # mid_ck at each tick index
    fill_tick_indices: list[int] = []  # tick index at which each fill occurred
    fill_direction_signs: list[int] = []  # +1 for BUY, -1 for SELL

    # Global tick counter (for tick-based horizons)
    global_tick_idx = 0

    # ── Merge BidAsk + Tick by exch_ts ───────────────────────────────
    ba_ts = ba.get("exch_ts", np.array([], dtype=np.int64))
    ba_n = len(ba_ts)
    tick_ts = ticks.get("exch_ts", np.array([], dtype=np.int64))
    tick_n = len(tick_ts)

    ba_bid1_p = ba.get("bid1_p", np.array([], dtype=np.int64))
    ba_bid1_v = ba.get("bid1_v", np.array([], dtype=np.int64))
    ba_ask1_p = ba.get("ask1_p", np.array([], dtype=np.int64))
    ba_ask1_v = ba.get("ask1_v", np.array([], dtype=np.int64))
    t_price = ticks.get("price", np.array([], dtype=np.int64))
    t_vol = ticks.get("volume", np.array([], dtype=np.int64))

    ba_i = 0
    ti = 0

    def _activate_pending(current_ts: int) -> None:
        for _oid, pord in pending_orders.items():
            if not pord.is_active and current_ts >= pord.active_ts:
                if pord.side == Side.BUY:
                    depth = cur_bid_v
                elif pord.side == Side.SELL:
                    depth = cur_ask_v
                else:
                    depth = 1
                pord.queue_ahead = max(1, int(depth * queue_frac))
                pord.depth_at_place = depth
                pord.is_active = True

    def _complete_cancels(current_ts: int) -> None:
        to_remove: list[str] = []
        for oid, pord in pending_orders.items():
            if (
                pord.cancel_complete_ts is not None
                and current_ts >= pord.cancel_complete_ts
            ):
                to_remove.append(oid)
                cancel_event = OrderEvent(
                    order_id=oid,
                    strategy_id=strat.strategy_id,
                    symbol=symbol,
                    status=OrderStatus.CANCELLED,
                    submitted_qty=pord.qty,
                    filled_qty=0,
                    remaining_qty=pord.qty,
                    price=pord.price,
                    side=pord.side,
                    ingest_ts_ns=current_ts,
                    broker_ts_ns=current_ts,
                )
                strat.handle_event(ctx, cancel_event)
                captured_intents.clear()
        for oid in to_remove:
            del pending_orders[oid]

    def _check_fills(trade_price_ck: int, trade_vol: int, current_ts: int) -> None:
        nonlocal fill_counter

        to_remove: list[str] = []
        for oid, pord in pending_orders.items():
            if not pord.is_active:
                continue
            if pord.cancel_requested_ts is not None:
                continue

            filled = False
            if pord.side == Side.BUY and trade_price_ck <= pord.price * SCALE_RATIO:
                pord.queue_ahead -= trade_vol
                if pord.queue_ahead <= 0:
                    filled = True
            elif pord.side == Side.SELL and trade_price_ck >= pord.price * SCALE_RATIO:
                pord.queue_ahead -= trade_vol
                if pord.queue_ahead <= 0:
                    filled = True

            if filled:
                fill_counter += 1
                fill_price = pord.price  # platform scale

                # Update position
                if pord.side == Side.BUY:
                    pos_dict[symbol] = pos_dict.get(symbol, 0) + pord.qty
                else:
                    pos_dict[symbol] = pos_dict.get(symbol, 0) - pord.qty

                # ── Record fill with conditioning features ───────────
                spread_pts = _ck_price_to_pts(cur_ask_ck - cur_bid_ck)
                total_depth = cur_bid_v + cur_ask_v
                imbal = cur_bid_v / total_depth if total_depth > 0 else 0.5
                signed_flow = sum(recent_directions)
                mid_ck = (cur_bid_ck + cur_ask_ck) // 2
                mid_pts = _ck_price_to_pts(mid_ck)

                fill_rec = FillRecord(
                    fill_id=fill_counter,
                    date=date,
                    fill_ts=current_ts,
                    side="BUY" if pord.side == Side.BUY else "SELL",
                    fill_price_pts=_platform_price_to_pts(fill_price),
                    spread_at_fill=spread_pts,
                    signed_flow_10tick=signed_flow,
                    book_imbalance_l1=round(imbal, 4),
                    mid_at_fill_pts=mid_pts,
                )
                fill_records.append(fill_rec)
                fill_tick_indices.append(global_tick_idx)
                direction_sign = 1 if pord.side == Side.BUY else -1
                fill_direction_signs.append(direction_sign)

                # Notify strategy
                fill_event = FillEvent(
                    fill_id=f"BT_{date}_{fill_counter}",
                    account_id="BT_ACC",
                    order_id=oid,
                    strategy_id=strat.strategy_id,
                    symbol=symbol,
                    side=pord.side,
                    qty=pord.qty,
                    price=fill_price,
                    fee=0,
                    tax=0,
                    ingest_ts_ns=current_ts,
                    match_ts_ns=current_ts,
                )
                strat.handle_event(ctx, fill_event)
                captured_intents.clear()

                to_remove.append(oid)

        for oid in to_remove:
            del pending_orders[oid]

    def _process_intents(current_ts: int) -> None:
        nonlocal order_counter
        for intent in captured_intents:
            if intent.intent_type == IntentType.NEW:
                order_counter += 1
                oid = f"BT_ORD_{order_counter}"
                pending_orders[oid] = PendingOrder(
                    order_id=oid,
                    side=intent.side,
                    price=intent.price,
                    qty=intent.qty,
                    submit_ts=current_ts,
                    active_ts=current_ts + latency_ns,
                    queue_ahead=0,
                    is_active=False,
                )
            elif intent.intent_type == IntentType.CANCEL:
                target = intent.target_order_id
                if target and target in pending_orders:
                    pord = pending_orders[target]
                    if pord.cancel_requested_ts is None:
                        pord.cancel_requested_ts = current_ts
                        pord.cancel_complete_ts = current_ts + latency_ns
        captured_intents.clear()

    def _cancel_stale_orders(current_ts: int) -> None:
        cur_bid_platform = (
            _ck_price_to_platform(cur_bid_ck) if cur_bid_ck > 0 else 0
        )
        cur_ask_platform = (
            _ck_price_to_platform(cur_ask_ck) if cur_ask_ck > 0 else 0
        )
        for _oid, pord in list(pending_orders.items()):
            if pord.cancel_requested_ts is not None:
                continue
            stale = False
            if pord.side == Side.BUY and pord.price != cur_bid_platform:
                stale = True
            elif pord.side == Side.SELL and pord.price != cur_ask_platform:
                stale = True
            if stale:
                pord.cancel_requested_ts = current_ts
                pord.cancel_complete_ts = current_ts + latency_ns

    # ── Main event loop ──────────────────────────────────────────────
    INT64_MAX = np.iinfo(np.int64).max

    while ba_i < ba_n or ti < tick_n:
        ba_time = int(ba_ts[ba_i]) if ba_i < ba_n else INT64_MAX
        tk_time = int(tick_ts[ti]) if ti < tick_n else INT64_MAX

        if ba_time <= tk_time:
            # BidAsk event
            cur_ts = ba_time
            cur_bid_ck = int(ba_bid1_p[ba_i])
            cur_ask_ck = int(ba_ask1_p[ba_i])
            cur_bid_v = int(ba_bid1_v[ba_i])
            cur_ask_v = int(ba_ask1_v[ba_i])
            ba_i += 1

            if cur_bid_ck <= 0 or cur_ask_ck <= 0 or cur_bid_ck >= cur_ask_ck:
                continue

            _activate_pending(cur_ts)
            _complete_cancels(cur_ts)
            _cancel_stale_orders(cur_ts)

            best_bid = _ck_price_to_platform(cur_bid_ck)
            best_ask = _ck_price_to_platform(cur_ask_ck)
            mid_price_x2 = best_bid + best_ask
            spread_scaled = best_ask - best_bid
            bid_depth = cur_bid_v
            ask_depth = cur_ask_v
            total_qty = bid_depth + ask_depth
            imbalance = (
                (bid_depth - ask_depth) / total_qty if total_qty > 0 else 0.0
            )

            values = [0] * 27
            values[_IDX_BEST_BID] = best_bid
            values[_IDX_BEST_ASK] = best_ask
            values[_IDX_L1_BID_QTY] = bid_depth
            values[_IDX_L1_ASK_QTY] = ask_depth
            values[_IDX_L1_IMBALANCE_PPM] = (
                int((bid_depth - ask_depth) * 1_000_000 / total_qty)
                if total_qty > 0
                else 0
            )
            fids = tuple(f"f{i}" for i in range(27))
            feat = FeatureUpdateEvent(
                symbol=symbol,
                ts=cur_ts,
                local_ts=cur_ts,
                seq=0,
                feature_set_id="lob_shared_v3",
                schema_version=3,
                changed_mask=0xFFFFFFFF,
                warmup_ready_mask=0xFFFFFFFF,
                quality_flags=0,
                feature_ids=fids,
                values=tuple(values),
            )
            lob = LOBStatsEvent(
                symbol=symbol,
                ts=cur_ts,
                imbalance=imbalance,
                best_bid=best_bid,
                best_ask=best_ask,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                mid_price_x2=mid_price_x2,
                spread_scaled=spread_scaled,
            )

            captured_intents.clear()
            strat.handle_event(ctx, feat)
            captured_intents.clear()
            strat.handle_event(ctx, lob)
            _process_intents(cur_ts)

        else:
            # Tick event
            cur_ts = tk_time
            trade_price_ck = int(t_price[ti])
            trade_vol = int(t_vol[ti])
            ti += 1

            if trade_vol <= 0 or trade_price_ck <= 0:
                continue

            # Record mid at this tick for post-fill drift measurement
            if cur_bid_ck > 0 and cur_ask_ck > 0:
                tick_mid_timeline.append((cur_bid_ck + cur_ask_ck) // 2)
            else:
                # No valid quote yet — use trade price as proxy
                tick_mid_timeline.append(trade_price_ck)

            _activate_pending(cur_ts)
            _complete_cancels(cur_ts)

            # Record trade direction before fill check
            direction = _infer_trade_direction(
                trade_price_ck, cur_bid_ck, cur_ask_ck
            )
            recent_directions.append(direction)

            _check_fills(trade_price_ck, trade_vol, cur_ts)

            # Feed tick to strategy
            trade_price_platform = _ck_price_to_platform(trade_price_ck)
            meta = MetaData(seq=0, source_ts=cur_ts, local_ts=cur_ts)
            tick_event = TickEvent(
                meta=meta,
                symbol=symbol,
                price=trade_price_platform,
                volume=trade_vol,
                trade_direction=direction,
            )
            captured_intents.clear()
            strat.handle_event(ctx, tick_event)
            _process_intents(cur_ts)

            global_tick_idx += 1

    # ── Measure post-fill drift from tick_mid_timeline ───────────────
    mid_arr = np.array(tick_mid_timeline, dtype=np.int64)
    n_ticks_total = len(mid_arr)

    for i, rec in enumerate(fill_records):
        t0 = fill_tick_indices[i]
        dir_sign = fill_direction_signs[i]
        mid_at_fill_ck = mid_arr[t0] if t0 < n_ticks_total else 0

        # Post-fill drift at each horizon
        for h in DRIFT_HORIZONS:
            t_h = t0 + h
            if t_h < n_ticks_total:
                post_mid_ck = mid_arr[t_h]
                drift_ck = int(post_mid_ck) - int(mid_at_fill_ck)
                # Signed: positive = favorable for the fill side
                drift_pts = _ck_price_to_pts(drift_ck * dir_sign)
                rec.drift[h] = round(drift_pts, 4)
            else:
                rec.drift[h] = None  # not enough data

        # Max adverse excursion within MAE_HORIZON ticks
        mae_end = min(t0 + MAE_HORIZON, n_ticks_total)
        if t0 < n_ticks_total and mae_end > t0:
            window = mid_arr[t0:mae_end].astype(np.int64)
            excursions = (window - int(mid_at_fill_ck)) * (-dir_sign)
            # Adverse = price moved against us (positive excursion value = bad)
            max_adverse = int(np.max(excursions)) if len(excursions) > 0 else 0
            rec.mae_30 = round(_ck_price_to_pts(max(0, max_adverse)), 4)
        else:
            rec.mae_30 = 0.0

    return fill_records


# ── Analysis Functions ──────────────────────────────────────────────────


def _quintile_labels(values: np.ndarray) -> np.ndarray:
    """Assign quintile labels (0-4) to an array of values."""
    if len(values) == 0:
        return np.array([], dtype=np.int64)
    # Use percentiles to define quintile boundaries
    pcts = np.percentile(values, [20, 40, 60, 80])
    labels = np.zeros(len(values), dtype=np.int64)
    for i, v in enumerate(values):
        if v <= pcts[0]:
            labels[i] = 0
        elif v <= pcts[1]:
            labels[i] = 1
        elif v <= pcts[2]:
            labels[i] = 2
        elif v <= pcts[3]:
            labels[i] = 3
        else:
            labels[i] = 4
    return labels


def analyze_fills(fills: list[FillRecord]) -> None:  # noqa: C901
    """Print comprehensive analysis of fill-level data."""
    if not fills:
        print("No fills to analyze.")
        return

    n = len(fills)
    print(f"\n{'='*80}")
    print(f"R55 FILL DRIFT ANALYSIS — {n} fills across {len(set(f.date for f in fills))} days")
    print(f"{'='*80}")

    # ── Extract arrays ───────────────────────────────────────────────
    spreads = np.array([f.spread_at_fill for f in fills])
    flows = np.array([f.signed_flow_10tick for f in fills])
    imbals = np.array([f.book_imbalance_l1 for f in fills])
    maes = np.array([f.mae_30 for f in fills])

    drift_arrays: dict[int, np.ndarray] = {}
    for h in DRIFT_HORIZONS:
        vals = []
        for f in fills:
            v = f.drift.get(h)
            vals.append(v if v is not None else np.nan)
        drift_arrays[h] = np.array(vals)

    # ── 1. Overall statistics ────────────────────────────────────────
    print("\n--- 1. Overall Fill Statistics ---")
    n_buy = sum(1 for f in fills if f.side == "BUY")
    n_sell = n - n_buy
    print(f"  Total fills: {n}  (BUY: {n_buy}, SELL: {n_sell})")
    print(f"  Mean spread at fill: {np.mean(spreads):.2f} pts")
    print(f"  Mean |signed_flow_10|: {np.mean(np.abs(flows)):.2f}")
    print(f"  Mean book_imbal_l1: {np.mean(imbals):.4f}")
    print(f"  Mean MAE(30tick): {np.mean(maes):.4f} pts")
    print()
    print(f"  {'Horizon':<10} {'Mean Drift':>12} {'Std Drift':>12} {'Median':>10} {'% Adverse':>12}")
    for h in DRIFT_HORIZONS:
        d = drift_arrays[h]
        valid = d[~np.isnan(d)]
        if len(valid) == 0:
            continue
        pct_adverse = np.sum(valid < 0) / len(valid) * 100
        print(
            f"  t+{h:<7} {np.mean(valid):>12.4f} {np.std(valid):>12.4f} "
            f"{np.median(valid):>10.4f} {pct_adverse:>11.1f}%"
        )

    # ── 2. Conditional on spread ─────────────────────────────────────
    print("\n--- 2. Conditional Analysis: by Spread at Fill ---")
    unique_spreads = sorted(set(int(s) for s in spreads))
    # Group into integer spread buckets
    print(
        f"  {'Spread':>8} {'Count':>7} {'Drift t+5':>11} {'Drift t+10':>11} "
        f"{'Drift t+30':>11} {'MAE(30)':>9}"
    )
    for sp_val in unique_spreads:
        mask = (spreads >= sp_val) & (spreads < sp_val + 1)
        cnt = int(np.sum(mask))
        if cnt < 3:
            continue
        d5 = drift_arrays[5][mask]
        d10 = drift_arrays[10][mask]
        d30 = drift_arrays[30][mask]
        mae_grp = maes[mask]
        d5_valid = d5[~np.isnan(d5)]
        d10_valid = d10[~np.isnan(d10)]
        d30_valid = d30[~np.isnan(d30)]
        print(
            f"  {sp_val:>7}pt {cnt:>7} "
            f"{np.mean(d5_valid):>11.4f} {np.mean(d10_valid):>11.4f} "
            f"{np.mean(d30_valid):>11.4f} {np.mean(mae_grp):>9.4f}"
        )

    # ── 3. Conditional on signed_flow quintile ───────────────────────
    print("\n--- 3. Conditional Analysis: by Signed Flow (10-tick) Quintile ---")
    flow_q = _quintile_labels(flows.astype(np.float64))
    print(
        f"  {'Quintile':>8} {'Flow Range':>14} {'Count':>7} {'Drift t+5':>11} "
        f"{'Drift t+30':>11} {'MAE(30)':>9}"
    )
    for q in range(5):
        mask = flow_q == q
        cnt = int(np.sum(mask))
        if cnt < 3:
            continue
        flow_min = int(np.min(flows[mask]))
        flow_max = int(np.max(flows[mask]))
        d5 = drift_arrays[5][mask]
        d30 = drift_arrays[30][mask]
        mae_grp = maes[mask]
        d5_valid = d5[~np.isnan(d5)]
        d30_valid = d30[~np.isnan(d30)]
        print(
            f"  Q{q}       [{flow_min:>4},{flow_max:>4}] {cnt:>7} "
            f"{np.mean(d5_valid):>11.4f} {np.mean(d30_valid):>11.4f} "
            f"{np.mean(mae_grp):>9.4f}"
        )

    # ── 4. Conditional on imbalance quintile ─────────────────────────
    print("\n--- 4. Conditional Analysis: by Book Imbalance L1 Quintile ---")
    imbal_q = _quintile_labels(imbals)
    print(
        f"  {'Quintile':>8} {'Imbal Range':>14} {'Count':>7} {'Drift t+5':>11} "
        f"{'Drift t+30':>11} {'MAE(30)':>9}"
    )
    for q in range(5):
        mask = imbal_q == q
        cnt = int(np.sum(mask))
        if cnt < 3:
            continue
        im_min = float(np.min(imbals[mask]))
        im_max = float(np.max(imbals[mask]))
        d5 = drift_arrays[5][mask]
        d30 = drift_arrays[30][mask]
        mae_grp = maes[mask]
        d5_valid = d5[~np.isnan(d5)]
        d30_valid = d30[~np.isnan(d30)]
        print(
            f"  Q{q}       [{im_min:.3f},{im_max:.3f}] {cnt:>7} "
            f"{np.mean(d5_valid):>11.4f} {np.mean(d30_valid):>11.4f} "
            f"{np.mean(mae_grp):>9.4f}"
        )

    # ── 5. Correlation matrix ────────────────────────────────────────
    print("\n--- 5. Correlation Matrix: Features vs Post-Fill Drift ---")
    features = np.column_stack([spreads, flows.astype(np.float64), imbals])
    feat_names = ["spread", "signed_flow", "imbalance"]

    d30 = drift_arrays[30]
    valid_mask = ~np.isnan(d30)
    if np.sum(valid_mask) > 10:
        print(f"\n  Drift at t+30 (n={int(np.sum(valid_mask))}):")
        print(f"  {'Feature':<16} {'Corr':>8} {'Abs Corr':>10}")
        for j, fname in enumerate(feat_names):
            x = features[valid_mask, j]
            y = d30[valid_mask]
            if np.std(x) > 0 and np.std(y) > 0:
                corr = float(np.corrcoef(x, y)[0, 1])
            else:
                corr = 0.0
            print(f"  {fname:<16} {corr:>8.4f} {abs(corr):>10.4f}")

    d10 = drift_arrays[10]
    valid_mask_10 = ~np.isnan(d10)
    if np.sum(valid_mask_10) > 10:
        print(f"\n  Drift at t+10 (n={int(np.sum(valid_mask_10))}):")
        print(f"  {'Feature':<16} {'Corr':>8} {'Abs Corr':>10}")
        for j, fname in enumerate(feat_names):
            x = features[valid_mask_10, j]
            y = d10[valid_mask_10]
            if np.std(x) > 0 and np.std(y) > 0:
                corr = float(np.corrcoef(x, y)[0, 1])
            else:
                corr = 0.0
            print(f"  {fname:<16} {corr:>8.4f} {abs(corr):>10.4f}")

    # ── 6. Cross-conditioning: spread x flow ─────────────────────────
    print("\n--- 6. Cross-Conditioning: Spread x Signed Flow Direction ---")
    flow_pos_mask = flows > 0
    flow_neg_mask = flows < 0
    flow_zero_mask = flows == 0

    for sp_val in unique_spreads:
        sp_mask = (spreads >= sp_val) & (spreads < sp_val + 1)
        cnt_sp = int(np.sum(sp_mask))
        if cnt_sp < 10:
            continue
        print(f"\n  Spread = {sp_val} pt (n={cnt_sp}):")
        print(f"    {'Flow Dir':<12} {'Count':>7} {'Drift t+10':>11} {'Drift t+30':>11} {'MAE(30)':>9}")
        for label, fmask in [
            ("flow > 0", flow_pos_mask),
            ("flow = 0", flow_zero_mask),
            ("flow < 0", flow_neg_mask),
        ]:
            combined = sp_mask & fmask
            cnt = int(np.sum(combined))
            if cnt < 3:
                continue
            d10_g = drift_arrays[10][combined]
            d30_g = drift_arrays[30][combined]
            mae_g = maes[combined]
            d10_v = d10_g[~np.isnan(d10_g)]
            d30_v = d30_g[~np.isnan(d30_g)]
            print(
                f"    {label:<12} {cnt:>7} "
                f"{np.mean(d10_v):>11.4f} {np.mean(d30_v):>11.4f} "
                f"{np.mean(mae_g):>9.4f}"
            )

    # ── 7. Adverse drift identification ──────────────────────────────
    print("\n--- 7. Adverse Drift Regime Identification ---")
    d30_valid_mask = ~np.isnan(drift_arrays[30])
    if np.sum(d30_valid_mask) > 20:
        d30_vals = drift_arrays[30][d30_valid_mask]
        overall_mean = float(np.mean(d30_vals))
        overall_pct_neg = float(np.sum(d30_vals < 0) / len(d30_vals) * 100)
        print(f"  Overall t+30 drift: mean={overall_mean:.4f} pts, {overall_pct_neg:.1f}% adverse")
        print()

        # Find worst and best quintile combinations
        print("  Worst fill regimes (highest % adverse at t+30):")
        combos: list[tuple[str, int, float, float]] = []
        for sp_val in unique_spreads:
            sp_mask = (spreads >= sp_val) & (spreads < sp_val + 1)
            for q in range(5):
                fq_mask = flow_q == q
                combined = sp_mask & fq_mask & d30_valid_mask
                cnt = int(np.sum(combined))
                if cnt < 5:
                    continue
                d30_g = drift_arrays[30][combined]
                pct_neg = float(np.sum(d30_g < 0) / len(d30_g) * 100)
                mean_d = float(np.mean(d30_g))
                combos.append(
                    (f"spr={sp_val},flowQ{q}", cnt, mean_d, pct_neg)
                )

        combos.sort(key=lambda x: x[3], reverse=True)
        print(f"    {'Regime':<22} {'Count':>7} {'Mean Drift':>11} {'% Adverse':>10}")
        for label, cnt, mean_d, pct in combos[:5]:
            print(f"    {label:<22} {cnt:>7} {mean_d:>11.4f} {pct:>9.1f}%")

        print()
        print("  Best fill regimes (lowest % adverse at t+30):")
        combos.sort(key=lambda x: x[3])
        for label, cnt, mean_d, pct in combos[:5]:
            print(f"    {label:<22} {cnt:>7} {mean_d:>11.4f} {pct:>9.1f}%")

    print(f"\n{'='*80}")


def save_fills_csv(fills: list[FillRecord], out_path: Path) -> None:
    """Save fill records to CSV."""
    import csv

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        header = [
            "fill_id", "date", "fill_ts", "side", "fill_price_pts",
            "spread_at_fill", "signed_flow_10tick", "book_imbalance_l1",
            "mid_at_fill_pts", "mae_30",
        ]
        for h in DRIFT_HORIZONS:
            header.append(f"drift_t{h}")
        writer.writerow(header)

        for rec in fills:
            row = [
                rec.fill_id, rec.date, rec.fill_ts, rec.side,
                rec.fill_price_pts, rec.spread_at_fill,
                rec.signed_flow_10tick, rec.book_imbalance_l1,
                rec.mid_at_fill_pts, rec.mae_30,
            ]
            for h in DRIFT_HORIZONS:
                row.append(rec.drift.get(h))
            writer.writerow(row)


# ── Main ────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="R55 Fill Drift — Post-fill price path exploration"
    )
    parser.add_argument(
        "--password", required=True, help="ClickHouse password"
    )
    parser.add_argument(
        "--symbol", default="TMFD6", help="Symbol to analyze (default: TMFD6)"
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8123",
        help="ClickHouse HTTP URL (default: http://localhost:8123)",
    )
    parser.add_argument(
        "--days", type=int, default=0,
        help="Limit to N most recent days (0 = all)",
    )
    parser.add_argument(
        "--spread-threshold", type=int, default=DEFAULT_SPREAD_THRESHOLD,
        help=f"Spread threshold in points (default: {DEFAULT_SPREAD_THRESHOLD})",
    )
    parser.add_argument(
        "--max-pos", type=int, default=3,
        help="Max position (default: 3)",
    )
    args = parser.parse_args()

    print("R55 Fill Drift Exploration")
    print(f"  Symbol: {args.symbol}")
    print(f"  CK URL: {args.url}")
    print(f"  Spread threshold: {args.spread_threshold} pts")
    print(f"  Max position: {args.max_pos}")
    print()

    # Get trading days
    days = get_trading_days(args.url, args.password, args.symbol)
    if not days:
        print(f"ERROR: No trading days found for {args.symbol}")
        sys.exit(1)

    if args.days > 0:
        days = days[-args.days:]

    print(f"  Trading days available: {len(days)}")
    print(f"  Date range: {days[0]} to {days[-1]}")
    print()

    # Process each day
    all_fills: list[FillRecord] = []
    for i, date in enumerate(days):
        print(f"  [{i+1}/{len(days)}] Processing {date}...", end=" ", flush=True)
        ba, ticks = load_day_ck(args.url, args.password, args.symbol, date)

        if not ba or not ticks:
            print("SKIP (no data)")
            continue

        n_ba = len(ba.get("exch_ts", []))
        n_tick = len(ticks.get("exch_ts", []))
        print(f"({n_ba} BA, {n_tick} ticks)", end=" ", flush=True)

        day_fills = run_day_fill_drift(
            ba=ba,
            ticks=ticks,
            date=date,
            symbol=args.symbol,
            latency_ns=DEFAULT_LATENCY_NS,
            queue_frac=DEFAULT_QUEUE_FRAC,
            spread_threshold_pts=args.spread_threshold,
            max_pos=args.max_pos,
        )
        all_fills.extend(day_fills)
        print(f"-> {len(day_fills)} fills")

    print(f"\n  Total fills: {len(all_fills)}")

    # Save to CSV
    out_dir = Path(__file__).resolve().parent
    csv_path = out_dir / "fill_drift_results.csv"
    save_fills_csv(all_fills, csv_path)
    print(f"  Saved to: {csv_path}")

    # Run analysis
    analyze_fills(all_fills)


if __name__ == "__main__":
    main()
