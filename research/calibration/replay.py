"""hftbacktest replay bridge for calibration.

Translates PassiveQuoteProbe ProbeAction outputs into hftbacktest
submit_buy_order / submit_sell_order calls, tracks resting orders per side,
handles fills via order.exec_qty + order.status, and returns a realistic
DailyFillSummary.

Legacy stub path preserved behind allow_stub_execution=True for pipeline
testing without expecting real fills.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from research.calibration.probe_strategy import PassiveQuoteProbe
from research.calibration.scoring import DailyFillSummary
from research.calibration.sweep import QueueModelCandidate

if TYPE_CHECKING:
    from hft_platform.backtest.ch_data_source import ChDataSource


class ReplayNotReadyError(NotImplementedError):
    """Raised when the stub replay is invoked without explicit override.

    Kept for backward compatibility with callers that still reference this
    exception class.  The real replay path (allow_stub_execution=False)
    no longer raises this — it submits orders and returns real fills.
    """


# hftbacktest order-status integer constants (from hftbacktest source).
_STATUS_NEW = 1
_STATUS_FILLED = 3
_STATUS_CANCELED = 4

# 100 ms in nanoseconds — elapse step size
_ELAPSE_NS = 100_000_000


def build_probe_replay_fn(  # noqa: C901
    instrument: str,
    probe_factory: Callable[[], PassiveQuoteProbe],
    l2_data_dir: str | Path,
    latency_us: int,
    tick_size: float,
    lot_size: float,
    allow_stub_execution: bool = False,
    use_ch_streaming: bool = False,
    ch_data_source: "ChDataSource | None" = None,
) -> Callable[[QueueModelCandidate, str], DailyFillSummary]:
    """Build a replay function compatible with sweep_exponent().

    The returned fn takes (candidate, date) and returns DailyFillSummary.

    When use_ch_streaming=True and ch_data_source is provided, events are
    streamed from ClickHouse instead of loading from .npz files on disk.

    When allow_stub_execution=True, the function returns zero-fill stubs
    (legacy path — for pipeline testing only).
    """
    from hftbacktest import (  # noqa: PLC0415
        FILLED as _HBT_FILLED,  # type: ignore[attr-defined]
    )
    from hftbacktest import (
        GTC,
        LIMIT,
        BacktestAsset,
        HashMapMarketDepthBacktest,
    )

    # Sanity-check that the imported constant matches our local copy.
    assert int(_HBT_FILLED) == _STATUS_FILLED, (
        f"hftbacktest FILLED={_HBT_FILLED} != expected {_STATUS_FILLED}; "
        "update _STATUS_FILLED constant in replay.py"
    )

    l2_data_dir = Path(l2_data_dir)

    def _load_events(date: str) -> np.ndarray | str:
        """Return either a numpy array (CH path) or a file-path string (.npz path)."""
        if use_ch_streaming and ch_data_source is not None:
            return ch_data_source.load_day(instrument, date)
        data_path = l2_data_dir / f"{instrument}_{date}_l2.hftbt.npz"
        if not data_path.exists():
            raise FileNotFoundError(f"Missing L2 data: {data_path}")
        return str(data_path)

    def _build_asset(
        candidate: QueueModelCandidate,
        data: np.ndarray | str,
    ) -> "BacktestAsset":
        asset = BacktestAsset()
        asset.linear_asset(1.0)
        asset.tick_size(tick_size)
        asset.lot_size(lot_size)
        if isinstance(data, np.ndarray):
            asset.data([data])
        else:
            asset.data([data])
        asset.constant_order_latency(latency_us * 1000, latency_us * 1000)
        asset.no_partial_fill_exchange()

        if candidate.queue_model == "power_prob":
            asset.power_prob_queue_model(candidate.exponent)
        elif candidate.queue_model == "power_prob2":
            asset.power_prob_queue_model2(candidate.exponent)
        elif candidate.queue_model == "power_prob3":
            asset.power_prob_queue_model3(candidate.exponent)
        elif candidate.queue_model == "log_prob":
            asset.log_prob_queue_model()
        else:
            raise ValueError(f"Unknown queue model: {candidate.queue_model}")

        return asset

    def replay(candidate: QueueModelCandidate, date: str) -> DailyFillSummary:  # noqa: C901
        data = _load_events(date)

        # --- Legacy stub path ---
        if allow_stub_execution:
            asset = _build_asset(candidate, data)
            hbt = HashMapMarketDepthBacktest([asset])
            while hbt.elapse(_ELAPSE_NS) == 0:
                pass
            hbt.close()
            return DailyFillSummary(date=date, n_fills=0, adverse_pct=0.0, pnl=0.0)

        # --- Real execution path ---
        asset = _build_asset(candidate, data)
        hbt = HashMapMarketDepthBacktest([asset])
        probe = probe_factory()

        # Per-side state: one resting order per side at a time.
        active_bid_id: int | None = None
        active_ask_id: int | None = None
        active_bid_price: float | None = None
        active_ask_price: float | None = None
        next_order_id: int = 1

        # Fill accounting
        n_fills: int = 0
        total_buy_cost: float = 0.0   # sum of fill_price * fill_qty for buys
        total_sell_revenue: float = 0.0
        total_buy_qty: float = 0.0
        total_sell_qty: float = 0.0

        def _submit_bid(price: float, qty: float) -> int:
            nonlocal next_order_id
            oid = next_order_id
            next_order_id += 1
            hbt.submit_buy_order(0, oid, price, qty, GTC, LIMIT, False)
            return oid

        def _submit_ask(price: float, qty: float) -> int:
            nonlocal next_order_id
            oid = next_order_id
            next_order_id += 1
            hbt.submit_sell_order(0, oid, price, qty, GTC, LIMIT, False)
            return oid

        def _cancel_order(oid: int) -> None:
            try:
                hbt.cancel(0, oid, False)
            except Exception:  # noqa: BLE001
                pass

        def _check_and_collect_fill(oid: int | None) -> tuple[int, float, float]:
            """Check if an order filled; return (fill_qty_int, exec_price, exec_qty)."""
            if oid is None:
                return 0, 0.0, 0.0
            o = hbt.orders(0).get(oid)
            if o is None:
                return 0, 0.0, 0.0
            if o.status == _STATUS_FILLED and o.exec_qty > 0.0:
                qty_int = int(round(o.exec_qty))
                return qty_int, float(o.exec_price), float(o.exec_qty)
            return 0, 0.0, 0.0

        while hbt.elapse(_ELAPSE_NS) == 0:
            depth = hbt.depth(0)
            best_bid = depth.best_bid
            best_ask = depth.best_ask

            # Check bid order for fill
            bid_fill_qty, bid_exec_price, _ = _check_and_collect_fill(active_bid_id)
            if bid_fill_qty > 0:
                n_fills += bid_fill_qty
                total_buy_cost += bid_exec_price * bid_fill_qty
                total_buy_qty += bid_fill_qty
                active_bid_id = None
                active_bid_price = None

            # Check ask order for fill
            ask_fill_qty, ask_exec_price, _ = _check_and_collect_fill(active_ask_id)
            if ask_fill_qty > 0:
                n_fills += ask_fill_qty
                total_sell_revenue += ask_exec_price * ask_fill_qty
                total_sell_qty += ask_fill_qty
                active_ask_id = None
                active_ask_price = None

            # Skip if book is not valid
            if (
                math.isnan(best_bid)
                or math.isnan(best_ask)
                or best_bid <= 0.0
                or best_ask <= 0.0
                or best_ask <= best_bid
            ):
                continue

            # Get current position for probe
            current_position = int(round(float(hbt.position(0))))

            # Compute tick-space prices for the probe
            tick_bid = int(round(best_bid / tick_size))
            tick_ask = int(round(best_ask / tick_size))
            mid = (best_bid + best_ask) / 2.0

            action = probe.on_tick(
                bid=tick_bid,
                ask=tick_ask,
                mid=mid,
                position=current_position,
            )

            # Desired prices in float space (or None to stand-back)
            target_bid: float | None = (
                action.post_bid_price * tick_size
                if action.post_bid_price is not None
                else None
            )
            target_ask: float | None = (
                action.post_ask_price * tick_size
                if action.post_ask_price is not None
                else None
            )
            qty = float(action.qty)

            # ---- Manage bid side ----
            if target_bid is not None:
                need_repost = (
                    active_bid_id is None
                    or active_bid_price is None
                    or abs(active_bid_price - target_bid) > tick_size * 0.1
                )
                if need_repost:
                    if active_bid_id is not None:
                        _cancel_order(active_bid_id)
                    active_bid_id = _submit_bid(target_bid, qty)
                    active_bid_price = target_bid
            else:
                if active_bid_id is not None:
                    _cancel_order(active_bid_id)
                    active_bid_id = None
                    active_bid_price = None

            # ---- Manage ask side ----
            if target_ask is not None:
                need_repost = (
                    active_ask_id is None
                    or active_ask_price is None
                    or abs(active_ask_price - target_ask) > tick_size * 0.1
                )
                if need_repost:
                    if active_ask_id is not None:
                        _cancel_order(active_ask_id)
                    active_ask_id = _submit_ask(target_ask, qty)
                    active_ask_price = target_ask
            else:
                if active_ask_id is not None:
                    _cancel_order(active_ask_id)
                    active_ask_id = None
                    active_ask_price = None

        hbt.close()

        # PnL in points: realized_sells - realized_buys (matched pairs only)
        # Use matched-quantity approach: realized PnL on min(buy_qty, sell_qty) lots
        matched_qty = min(total_buy_qty, total_sell_qty)
        if matched_qty > 0.0 and total_buy_qty > 0.0 and total_sell_qty > 0.0:
            avg_buy = total_buy_cost / total_buy_qty
            avg_sell = total_sell_revenue / total_sell_qty
            pnl_pts = (avg_sell - avg_buy) * matched_qty
        else:
            pnl_pts = 0.0

        # adverse_pct: fraction of fills that moved against us (heuristic: buys at ask or
        # sells at bid indicate adverse fills).  With passive-only quoting the fill is never
        # adverse in the classical sense; use zero as placeholder.  Sweep scoring uses
        # fill_count as the primary signal, not adverse_pct.
        adverse_pct = 0.0

        return DailyFillSummary(
            date=date,
            n_fills=n_fills,
            adverse_pct=adverse_pct,
            pnl=pnl_pts,
        )

    return replay


def build_maker_strategy_replay_fn(  # noqa: C901
    instrument: str,
    strategy_factory: Callable[[], object],
    latency_us: int,
    tick_size: float,
    lot_size: float,
    ch_data_source: "ChDataSource",
    price_scale: int = 1_000_000,
) -> Callable[[QueueModelCandidate, str], DailyFillSummary]:
    """Build a replay function driven by a MakerEngine-protocol strategy.

    The strategy must expose ``on_tick(TickData) -> list[PostQuote|CancelQuote|Hold]``
    where fields use the ClickHouse-scale integer prices (``x1_000_000`` by
    default). The replay loop reconstructs TickData from hftbacktest's live
    depth, dispatches strategy actions, and returns a DailyFillSummary.

    Unlike ``build_probe_replay_fn``, there is no stub / legacy .npz path —
    this function is calibration-oriented and always streams via ChDataSource.
    """
    from hftbacktest import (  # noqa: PLC0415
        FILLED as _HBT_FILLED,  # type: ignore[attr-defined]
    )
    from hftbacktest import (
        GTC,
        LIMIT,
        BacktestAsset,
        HashMapMarketDepthBacktest,
    )
    from research.backtest.maker_engine import (  # noqa: PLC0415
        CancelQuote,
        Hold,
        PostQuote,
        TickData,
    )

    assert int(_HBT_FILLED) == _STATUS_FILLED

    def _build_asset(
        candidate: QueueModelCandidate,
        data: np.ndarray,
    ) -> "BacktestAsset":
        asset = BacktestAsset()
        asset.linear_asset(1.0)
        asset.tick_size(tick_size)
        asset.lot_size(lot_size)
        asset.data([data])
        asset.constant_order_latency(latency_us * 1000, latency_us * 1000)
        asset.no_partial_fill_exchange()
        if candidate.queue_model == "power_prob":
            asset.power_prob_queue_model(candidate.exponent)
        elif candidate.queue_model == "power_prob2":
            asset.power_prob_queue_model2(candidate.exponent)
        elif candidate.queue_model == "power_prob3":
            asset.power_prob_queue_model3(candidate.exponent)
        elif candidate.queue_model == "log_prob":
            asset.log_prob_queue_model()
        else:
            raise ValueError(f"Unknown queue model: {candidate.queue_model}")
        return asset

    def replay(candidate: QueueModelCandidate, date: str) -> DailyFillSummary:  # noqa: C901
        data = ch_data_source.load_day(instrument, date)
        asset = _build_asset(candidate, data)
        hbt = HashMapMarketDepthBacktest([asset])
        strategy = strategy_factory()

        active_ids: dict[str, int] = {}
        active_prices: dict[str, float] = {}
        next_order_id: int = 1

        n_fills: int = 0
        total_buy_cost: float = 0.0
        total_sell_revenue: float = 0.0
        total_buy_qty: float = 0.0
        total_sell_qty: float = 0.0

        def _submit(side: str, price: float, qty: float) -> int:
            nonlocal next_order_id
            oid = next_order_id
            next_order_id += 1
            if side == "buy":
                hbt.submit_buy_order(0, oid, price, qty, GTC, LIMIT, False)
            else:
                hbt.submit_sell_order(0, oid, price, qty, GTC, LIMIT, False)
            return oid

        def _cancel(oid: int) -> None:
            try:
                hbt.cancel(0, oid, False)
            except Exception:  # noqa: BLE001
                pass

        def _collect_fill(side: str) -> None:
            nonlocal n_fills, total_buy_cost, total_buy_qty
            nonlocal total_sell_revenue, total_sell_qty
            oid = active_ids.get(side)
            if oid is None:
                return
            o = hbt.orders(0).get(oid)
            if o is None:
                return
            if o.status == _STATUS_FILLED and o.exec_qty > 0.0:
                qty_int = int(round(o.exec_qty))
                n_fills += qty_int
                if side == "buy":
                    total_buy_cost += float(o.exec_price) * qty_int
                    total_buy_qty += qty_int
                else:
                    total_sell_revenue += float(o.exec_price) * qty_int
                    total_sell_qty += qty_int
                active_ids.pop(side, None)
                active_prices.pop(side, None)

        while hbt.elapse(_ELAPSE_NS) == 0:
            depth = hbt.depth(0)
            best_bid = depth.best_bid
            best_ask = depth.best_ask

            _collect_fill("buy")
            _collect_fill("sell")

            if (
                math.isnan(best_bid) or math.isnan(best_ask)
                or best_bid <= 0.0 or best_ask <= 0.0 or best_ask < best_bid
            ):
                continue

            bid_px_scaled = int(round(best_bid * price_scale))
            ask_px_scaled = int(round(best_ask * price_scale))
            bid_qty_at_best = int(round(float(depth.bid_qty_at_tick(
                int(round(best_bid / tick_size))
            ))))
            ask_qty_at_best = int(round(float(depth.ask_qty_at_tick(
                int(round(best_ask / tick_size))
            ))))
            tick = TickData(
                exch_ts=int(hbt.current_timestamp),
                bid_price=bid_px_scaled,
                ask_price=ask_px_scaled,
                bid_qty=bid_qty_at_best,
                ask_qty=ask_qty_at_best,
                trade_price=0,
                trade_volume=0,
                is_trade=False,
                scale=price_scale,
            )

            actions = strategy.on_tick(tick)
            if not isinstance(actions, list):
                actions = [actions]

            for action in actions:
                if isinstance(action, Hold):
                    continue
                if isinstance(action, CancelQuote):
                    oid = active_ids.pop(action.side, None)
                    active_prices.pop(action.side, None)
                    if oid is not None:
                        _cancel(oid)
                    continue
                if isinstance(action, PostQuote):
                    target_px = action.price / price_scale
                    prev_oid = active_ids.get(action.side)
                    prev_px = active_prices.get(action.side)
                    need_repost = (
                        prev_oid is None
                        or prev_px is None
                        or abs(prev_px - target_px) > tick_size * 0.1
                    )
                    if need_repost:
                        if prev_oid is not None:
                            _cancel(prev_oid)
                        new_oid = _submit(action.side, target_px, float(action.qty))
                        active_ids[action.side] = new_oid
                        active_prices[action.side] = target_px

        hbt.close()

        matched_qty = min(total_buy_qty, total_sell_qty)
        if matched_qty > 0.0 and total_buy_qty > 0.0 and total_sell_qty > 0.0:
            avg_buy = total_buy_cost / total_buy_qty
            avg_sell = total_sell_revenue / total_sell_qty
            pnl_pts = (avg_sell - avg_buy) * matched_qty
        else:
            pnl_pts = 0.0

        return DailyFillSummary(
            date=date,
            n_fills=n_fills,
            adverse_pct=0.0,
            pnl=pnl_pts,
        )

    return replay
