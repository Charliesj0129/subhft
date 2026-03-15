from decimal import Decimal
from importlib import metadata as importlib_metadata

import numpy as np
from structlog import get_logger

try:
    from hftbacktest import (
        BacktestAsset,
        HashMapMarketDepthBacktest,
    )

    # v2.x uses integer constants; GTC replaces ROD, LIMIT replaces Limit class
    from hftbacktest.order import GTC, IOC, LIMIT

    HFTBACKTEST_AVAILABLE = True
except ImportError:
    HFTBACKTEST_AVAILABLE = False

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.core.pricing import FixedPriceScaleProvider, PriceCodec
from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.strategy.base import BaseStrategy, StrategyContext

logger = get_logger("hbt_adapter")
_MODERN_DEFAULT_WAIT_TIMEOUT = 10**18


class HftBacktestAdapter:
    """Runs a BaseStrategy instance inside HftBacktest engine.

    Note: ``modify_latency_us`` and ``cancel_latency_us`` are stored for future
    hftbacktest versions that support per-operation latency.  The current
    ``constant_order_latency`` builder applies a single ``latency_us`` to all
    order types.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        asset_symbol: str,
        data_path: str,
        latency_us=100,
        seed: int = 42,
        price_scale: int = 10_000,
        equity_sample_ns: int = 1_000_000,
        initial_balance: float = 1_000_000.0,
        tick_size: float | None = None,
        lot_size: float | None = None,
        maker_fee: float = 0.0,
        taker_fee: float = 0.0,
        partial_fill: bool = True,
        feature_mode: str = "stats_only",
        dispatch_feature_events: bool = False,
        queue_model: str = "PowerProbQueueModel(3.0)",
        latency_model: str = "ConstantLatency",
        exchange_model: str = "NoPartialFillExchange",
        latency_data_path: str | None = None,
        modify_latency_us: int = 0,
        cancel_latency_us: int = 0,
        timeout: int = 0,
        tick_mode: str = "feed",
        elapse_ns: int = 100_000_000,
        feature_array_source: tuple[np.ndarray, np.ndarray] | None = None,
    ):
        if not HFTBACKTEST_AVAILABLE:
            raise ImportError("hftbacktest not installed")

        # 0. Seeding for Determinism
        import random

        random.seed(seed)
        np.random.seed(seed)

        self.strategy = strategy
        self.symbol = asset_symbol
        self.data_path = data_path
        self.modify_latency_us = int(modify_latency_us)
        self.cancel_latency_us = int(cancel_latency_us)
        self.timeout = int(timeout)
        self.price_scale = price_scale
        self.price_codec = PriceCodec(FixedPriceScaleProvider(price_scale))
        self._intent_seq = 0
        self._hbt_seq = 0
        self.positions = {self.symbol: 0}
        self._prev_position = 0
        self._total_buy_fills = 0
        self._total_sell_fills = 0
        self._fill_log: list[dict] = []
        self.equity_sample_ns = int(equity_sample_ns)
        self._next_equity_sample_ns = 0
        self._last_known_balance = float(initial_balance)
        self._equity_timestamps_ns: list[int] = []
        self._equity_values: list[float] = []
        self._wait_status_mode = _detect_wait_status_mode()
        self.feature_mode = str(feature_mode or "stats_only").strip().lower()
        self.dispatch_feature_events = bool(dispatch_feature_events)
        self._lob_engine: LOBEngine | None = None
        self._feature_engine: FeatureEngine | None = None
        self.tick_mode = str(tick_mode).strip().lower()
        if self.tick_mode not in ("feed", "elapse"):
            raise ValueError(f"tick_mode must be 'feed' or 'elapse', got {self.tick_mode!r}")
        self.elapse_ns = int(elapse_ns)
        self._feature_array_source = feature_array_source

        if self.feature_mode == "lob_feature":
            self._lob_engine = LOBEngine()
            self._feature_engine = FeatureEngine()
            try:
                setattr(self._lob_engine, "feature_engine", self._feature_engine)
            except Exception:
                pass

        resolved_tick_size = float(tick_size) if tick_size is not None else _infer_tick_size_from_data(data_path)
        resolved_lot_size = float(lot_size) if lot_size is not None else 1.0

        # Setup HftBacktest (v2.x builder pattern)
        asset_builder = BacktestAsset().data([data_path]).linear_asset(1.0)

        # -- Latency model selection --
        latency_model_lower = str(latency_model).strip().lower()
        if latency_model_lower == "intporderlatency" and latency_data_path:
            asset_builder = _call_if_exists(asset_builder, "intp_order_latency", latency_data_path)
        elif latency_model_lower == "feedlatency":
            asset_builder = _call_if_exists(
                asset_builder, "constant_order_latency", latency_us * 1000, latency_us * 1000
            )
        else:
            # Default: ConstantLatency (renamed constant_order_latency in v2.x)
            asset_builder = _call_if_exists(
                asset_builder, "constant_order_latency", latency_us * 1000, latency_us * 1000
            )

        # -- Queue model selection --
        queue_model_lower = str(queue_model).strip().lower()
        if "riskadverse" in queue_model_lower or "risk_adverse" in queue_model_lower:
            asset_builder = _call_if_exists(asset_builder, "risk_adverse_queue_model")
        elif "logprob" in queue_model_lower:
            asset_builder = _call_if_exists(asset_builder, "log_prob_queue_model")
        elif "l3fifo" in queue_model_lower:
            asset_builder = _call_if_exists(asset_builder, "l3_fifo_queue_model")
        else:
            # Default: PowerProbQueueModel — extract exponent if specified
            import re

            m = re.search(r"[\d.]+", str(queue_model))
            exponent = float(m.group()) if m else 3.0
            asset_builder = _call_if_exists(asset_builder, "power_prob_queue_model", exponent)

        asset_builder = _call_if_exists(asset_builder, "tick_size", resolved_tick_size)
        asset_builder = _call_if_exists(asset_builder, "lot_size", resolved_lot_size)
        if maker_fee or taker_fee:
            asset_builder = _call_if_exists(
                asset_builder,
                "trading_value_fee_model",
                float(maker_fee),
                float(taker_fee),
            )

        # -- Exchange model selection --
        exchange_model_lower = str(exchange_model).strip().lower()
        if "partialfill" in exchange_model_lower and "no" not in exchange_model_lower:
            asset_builder = _call_if_exists(asset_builder, "partial_fill_exchange")
        elif partial_fill and "nopartialfill" not in exchange_model_lower:
            asset_builder = _call_if_exists(asset_builder, "partial_fill_exchange")
        else:
            asset_builder = _call_if_exists(asset_builder, "no_partial_fill_exchange")
        asset_builder = _call_if_exists(asset_builder, "int_order_id_converter")

        self.hbt = HashMapMarketDepthBacktest([asset_builder])

        # Feature array lookup (for elapse mode with precomputed features)
        self._feature_array_lookup = None
        if self._feature_array_source is not None:
            ts_arr, feat_arr = self._feature_array_source
            self._feature_array_lookup = self._make_feature_lookup(ts_arr, feat_arr)

        # Context Mapping
        feature_tuple_src = self._feature_engine.get_feature_tuple if self._feature_engine else None
        if self._feature_array_lookup is not None:
            feature_tuple_src = self._feature_array_lookup

        self.ctx = StrategyContext(
            positions=self.positions,
            strategy_id=self.strategy.strategy_id,
            intent_factory=self._intent_factory,
            price_scaler=self._scale_price,
            lob_source=None,
            lob_l1_source=(self._lob_engine.get_l1_scaled if self._lob_engine else None),
            feature_source=(self._feature_engine.get_feature if self._feature_engine else None),
            feature_view_source=(self._feature_engine.get_feature_view if self._feature_engine else None),
            feature_set_source=(self._feature_engine.feature_set_id if self._feature_engine else None),
            feature_tuple_source=feature_tuple_src,
        )

    def run(self):
        """Dispatch to feed or elapse run loop based on tick_mode."""
        if self.tick_mode == "elapse":
            return self._run_elapse()
        return self._run_feed()

    def _run_feed(self):
        logger.info("Starting HftBacktest simulation (feed mode)...")
        self._equity_timestamps_ns.clear()
        self._equity_values.clear()
        self._next_equity_sample_ns = 0

        # Initialize Strategy
        # Strategy expects on_book(ctx, event)
        # We need to bridge the loop.

        # HftBacktest v2.x loop: advance to each feed event
        while self._wait_for_next_feed():
            # Current LOB State
            # We construct a mock event for the strategy
            # Efficiently accessing hbt depth

            dp = self.hbt.depth(0)
            best_bid = dp.best_bid
            best_ask = dp.best_ask
            if (
                not (best_bid == best_bid)
                or not (best_ask == best_ask)
                or best_bid <= 0
                or best_ask >= 2147483647
                or best_bid >= best_ask
            ):
                continue

            ts_ns = int(self.hbt.current_timestamp)
            event = None
            feature_event = None
            if self.feature_mode == "lob_feature" and self._lob_engine is not None:
                bidask_event = self._build_l1_bidask_event(dp, ts_ns)
                stats = self._lob_engine.process_event(bidask_event)
                if isinstance(stats, LOBStatsEvent):
                    event = stats
                    if self._feature_engine is not None:
                        process_lob_update = getattr(self._feature_engine, "process_lob_update", None)
                        if callable(process_lob_update):
                            feature_event = process_lob_update(bidask_event, stats, local_ts_ns=ts_ns)
                        else:
                            feature_event = self._feature_engine.process_lob_stats(stats, local_ts_ns=ts_ns)
                else:
                    bid_qty = _resolve_qty(dp, "best_bid_qty", "bid_qty", "bid_volume")
                    ask_qty = _resolve_qty(dp, "best_ask_qty", "ask_qty", "ask_volume")
                    total_qty = bid_qty + ask_qty
                    imb = (bid_qty - ask_qty) / total_qty if total_qty > 0 else 0.0
                    event = LOBStatsEvent(
                        symbol=self.symbol,
                        ts=ts_ns,
                        imbalance=imb,
                        best_bid=int(best_bid),
                        best_ask=int(best_ask),
                        bid_depth=bid_qty,
                        ask_depth=ask_qty,
                    )
            else:
                bid_qty = _resolve_qty(dp, "best_bid_qty", "bid_qty", "bid_volume")
                ask_qty = _resolve_qty(dp, "best_ask_qty", "ask_qty", "ask_volume")
                total_qty = bid_qty + ask_qty
                imb = (bid_qty - ask_qty) / total_qty if total_qty > 0 else 0.0
                event = LOBStatsEvent(
                    symbol=self.symbol,
                    ts=ts_ns,
                    imbalance=imb,
                    best_bid=int(best_bid),
                    best_ask=int(best_ask),
                    bid_depth=bid_qty,
                    ask_depth=ask_qty,
                )

            # Update Context State + fill detection
            old_pos = self._prev_position
            self._sync_positions()
            new_pos = self.positions.get(self.symbol, 0)
            delta = new_pos - old_pos
            if delta != 0:
                if delta > 0:
                    self._total_buy_fills += abs(delta)
                else:
                    self._total_sell_fills += abs(delta)
                self._fill_log.append({
                    "ts_ns": ts_ns,
                    "delta": delta,
                    "position_after": new_pos,
                    "mid_price": (int(best_bid) + int(best_ask)) / 2.0,
                })
            self._prev_position = new_pos
            self._maybe_record_equity_point(int(self.hbt.current_timestamp), int(best_bid), int(best_ask))

            # Call Strategy
            intents = self.strategy.handle_event(self.ctx, event)
            if feature_event is not None and self.dispatch_feature_events:
                more = self.strategy.handle_event(self.ctx, feature_event)
                if more:
                    intents.extend(more)

            # Execute Intents
            for intent in intents:
                self.execute_intent(intent)

        return self.hbt.close()

    def _run_elapse(self):
        """Run simulation using elapse-based stepping.

        Instead of calling back on every LOB event, ``hbt.elapse(ns)``
        advances the internal engine by ``elapse_ns`` nanoseconds at a time.
        All intermediate LOB updates are processed internally by hftbacktest
        (queue position stays accurate), but Python only gets called at each
        elapse boundary — dramatically reducing Python overhead for long
        backtests.
        """
        logger.info(
            "Starting HftBacktest simulation (elapse mode)...",
            elapse_ns=self.elapse_ns,
        )
        self._equity_timestamps_ns.clear()
        self._equity_values.clear()
        self._next_equity_sample_ns = 0

        while self.hbt.elapse(self.elapse_ns) == 0:
            dp = self.hbt.depth(0)
            best_bid = dp.best_bid
            best_ask = dp.best_ask
            if (
                not (best_bid == best_bid)
                or not (best_ask == best_ask)
                or best_bid <= 0
                or best_ask >= 2147483647
                or best_bid >= best_ask
            ):
                continue

            ts_ns = int(self.hbt.current_timestamp)

            # Access trades that occurred during this elapse interval
            last_trades = None
            try:
                last_trades = self.hbt.last_trades(0)
                self.hbt.clear_last_trades(0)
            except (AttributeError, TypeError):
                pass

            # Build LOB event
            event = None
            feature_event = None
            if self.feature_mode == "lob_feature" and self._lob_engine is not None:
                bidask_event = self._build_l1_bidask_event(dp, ts_ns)
                stats = self._lob_engine.process_event(bidask_event)
                if isinstance(stats, LOBStatsEvent):
                    event = stats
                    if self._feature_engine is not None:
                        process_lob_update = getattr(self._feature_engine, "process_lob_update", None)
                        if callable(process_lob_update):
                            feature_event = process_lob_update(bidask_event, stats, local_ts_ns=ts_ns)
                        else:
                            feature_event = self._feature_engine.process_lob_stats(stats, local_ts_ns=ts_ns)
                else:
                    bid_qty = _resolve_qty(dp, "best_bid_qty", "bid_qty", "bid_volume")
                    ask_qty = _resolve_qty(dp, "best_ask_qty", "ask_qty", "ask_volume")
                    total_qty = bid_qty + ask_qty
                    imb = (bid_qty - ask_qty) / total_qty if total_qty > 0 else 0.0
                    event = LOBStatsEvent(
                        symbol=self.symbol,
                        ts=ts_ns,
                        imbalance=imb,
                        best_bid=int(best_bid),
                        best_ask=int(best_ask),
                        bid_depth=bid_qty,
                        ask_depth=ask_qty,
                    )
            else:
                bid_qty = _resolve_qty(dp, "best_bid_qty", "bid_qty", "bid_volume")
                ask_qty = _resolve_qty(dp, "best_ask_qty", "ask_qty", "ask_volume")
                total_qty = bid_qty + ask_qty
                imb = (bid_qty - ask_qty) / total_qty if total_qty > 0 else 0.0
                event = LOBStatsEvent(
                    symbol=self.symbol,
                    ts=ts_ns,
                    imbalance=imb,
                    best_bid=int(best_bid),
                    best_ask=int(best_ask),
                    bid_depth=bid_qty,
                    ask_depth=ask_qty,
                )

            # Attach last_trades to event for MM strategies (best-effort)
            if last_trades is not None:
                try:
                    event.last_trades = last_trades  # type: ignore[attr-defined]
                except AttributeError:
                    pass  # __slots__ dataclass — skip

            # Update Context State + fill detection
            old_pos = self._prev_position
            self._sync_positions()
            new_pos = self.positions.get(self.symbol, 0)
            delta = new_pos - old_pos
            if delta != 0:
                if delta > 0:
                    self._total_buy_fills += abs(delta)
                else:
                    self._total_sell_fills += abs(delta)
                self._fill_log.append({
                    "ts_ns": ts_ns,
                    "delta": delta,
                    "position_after": new_pos,
                    "mid_price": (int(best_bid) + int(best_ask)) / 2.0,
                })
            self._prev_position = new_pos
            self._maybe_record_equity_point(ts_ns, int(best_bid), int(best_ask))

            # Call Strategy
            intents = self.strategy.handle_event(self.ctx, event)
            if feature_event is not None and self.dispatch_feature_events:
                more = self.strategy.handle_event(self.ctx, feature_event)
                if more:
                    intents.extend(more)

            # Execute Intents
            for intent in intents:
                self.execute_intent(intent)

        return self.hbt.close()

    def _make_feature_lookup(
        self, timestamps: np.ndarray, features: np.ndarray
    ):
        """Returns a callable(symbol: str) -> tuple that looks up features by current hbt timestamp."""
        idx = [0]  # mutable container for closure state

        def lookup(symbol: str) -> tuple:
            ts = int(self.hbt.current_timestamp)
            # Advance index monotonically (timestamps are sorted)
            while idx[0] < len(timestamps) - 1 and timestamps[idx[0] + 1] <= ts:
                idx[0] += 1
            return tuple(features[idx[0]])

        return lookup

    def _wait_for_next_feed(self) -> bool:
        if self._wait_status_mode == "modern":
            wait_timeout = self.timeout if self.timeout > 0 else _MODERN_DEFAULT_WAIT_TIMEOUT
        else:
            wait_timeout = self.timeout if self.timeout > 0 else -1
        status = int(self.hbt.wait_next_feed(True, wait_timeout))

        if status == 1:
            return False

        if self._wait_status_mode == "modern":
            if status in (2, 3):
                return True
            if status == 0:
                raise TimeoutError(
                    "hftbacktest wait_next_feed timed out before the next event; "
                    f"rerun with a larger --timeout or verify the input data stream: timeout={self.timeout}"
                )
            raise RuntimeError(f"Unexpected hftbacktest wait_next_feed status: {status}")

        if status == 0:
            return True

        raise RuntimeError(f"Unexpected legacy hftbacktest wait_next_feed status: {status}")

    @property
    def fill_stats(self) -> dict:
        """Return fill statistics from backtest run."""
        total_fills = self._total_buy_fills + self._total_sell_fills
        # Compute adverse selection: price move against fill direction
        adverse_selections: list[float] = []
        for i, fill in enumerate(self._fill_log):
            # Look ahead to next fill or end of log for mid price change
            if i + 1 < len(self._fill_log):
                next_mid = self._fill_log[i + 1]["mid_price"]
                mid_change = next_mid - fill["mid_price"]
                # Adverse = price moved against us (bought and price fell, or sold and price rose)
                if fill["delta"] > 0:  # buy fill
                    adverse_selections.append(-mid_change)
                else:  # sell fill
                    adverse_selections.append(mid_change)

        # Duration for fill rate calculation
        if len(self._fill_log) >= 2:
            duration_ns = self._fill_log[-1]["ts_ns"] - self._fill_log[0]["ts_ns"]
            duration_hours = duration_ns / 3.6e12
        elif len(self._equity_timestamps_ns) >= 2:
            duration_ns = self._equity_timestamps_ns[-1] - self._equity_timestamps_ns[0]
            duration_hours = duration_ns / 3.6e12
        else:
            duration_hours = 0.0

        return {
            "buy_fills": self._total_buy_fills,
            "sell_fills": self._total_sell_fills,
            "total_fills": total_fills,
            "fill_rate_per_hour": total_fills / duration_hours if duration_hours > 0 else 0.0,
            "adverse_selection_mean": float(np.mean(adverse_selections)) if adverse_selections else 0.0,
            "adverse_selection_median": float(np.median(adverse_selections)) if adverse_selections else 0.0,
            "n_fill_events": len(self._fill_log),
        }

    @property
    def equity_timestamps_ns(self) -> np.ndarray:
        return np.asarray(self._equity_timestamps_ns, dtype=np.int64)

    @property
    def equity_values(self) -> np.ndarray:
        return np.asarray(self._equity_values, dtype=np.float64)

    def get_mid_price(self):
        # Access hbt LOB
        dp = self.hbt.depth(0)  # asset 0
        bid = dp.best_bid
        ask = dp.best_ask
        if bid == 0 or ask == 2147483647:  # Max int check
            return float("nan")
        return (bid + ask) / 2.0

    def get_spread(self):
        dp = self.hbt.depth(0)
        return dp.best_ask - dp.best_bid

    def _build_l1_bidask_event(self, depth_obj, ts_ns: int) -> BidAskEvent:
        self._hbt_seq += 1
        best_bid = int(getattr(depth_obj, "best_bid", 0) or 0)
        best_ask = int(getattr(depth_obj, "best_ask", 0) or 0)
        bid_qty = _resolve_qty(depth_obj, "best_bid_qty", "bid_qty", "bid_volume")
        ask_qty = _resolve_qty(depth_obj, "best_ask_qty", "ask_qty", "ask_volume")
        bids = np.asarray([[best_bid, bid_qty]], dtype=np.int64)
        asks = np.asarray([[best_ask, ask_qty]], dtype=np.int64)
        return BidAskEvent(
            meta=MetaData(seq=self._hbt_seq, source_ts=int(ts_ns), local_ts=int(ts_ns), topic="hbt_bidask"),
            symbol=self.symbol,
            bids=bids,
            asks=asks,
            is_snapshot=False,
        )

    def execute_intent(self, intent):
        # Convert Intent -> HftBacktest Order
        # hbt.submit_buy_order(asset_id, order_id, price, qty, time_in_force, exec_type)

        asset_id = 0
        order_id = intent.intent_id
        price = self.price_codec.descale(intent.symbol, intent.price)
        qty = intent.qty
        tif = GTC if intent.tif == TIF.LIMIT else IOC  # GTC = Rest-of-Day (v2.x)

        if intent.intent_type == IntentType.NEW:
            if intent.side == Side.BUY:
                self.hbt.submit_buy_order(asset_id, order_id, price, qty, tif, LIMIT, False)
            else:
                self.hbt.submit_sell_order(asset_id, order_id, price, qty, tif, LIMIT, False)

        elif intent.intent_type == IntentType.CANCEL:
            self.hbt.cancel(asset_id, int(intent.target_order_id), False)

    def _intent_factory(self, strategy_id, symbol, side, price, qty, tif, intent_type, target_order_id=None):
        self._intent_seq += 1
        return OrderIntent(
            intent_id=self._intent_seq,
            strategy_id=strategy_id,
            symbol=symbol,
            intent_type=intent_type,
            side=side,
            price=price,
            qty=qty,
            tif=tif,
            target_order_id=target_order_id,
        )

    def _scale_price(self, symbol: str, price: int | float | Decimal) -> int:
        return self.price_codec.scale(symbol, price)

    def _sync_positions(self):
        try:
            self.positions[self.symbol] = self.hbt.position(0)
        except Exception as e:
            logger.error(
                "Failed to sync position from hftbacktest",
                symbol=self.symbol,
                error=str(e),
                error_type=type(e).__name__,
            )
            # Keep stale position rather than silently failing - strategy should be notified

    def _maybe_record_equity_point(self, ts_ns: int, best_bid: int, best_ask: int) -> None:
        if self.equity_sample_ns <= 0:
            return
        if ts_ns < self._next_equity_sample_ns:
            return
        self._next_equity_sample_ns = ts_ns + self.equity_sample_ns

        mid_price = (best_bid + best_ask) / 2.0
        position = float(self.positions.get(self.symbol, 0))
        balance = self._read_balance(0)
        equity = balance + (position * mid_price)

        self._equity_timestamps_ns.append(ts_ns)
        self._equity_values.append(float(equity))

    def _read_balance(self, asset_id: int) -> float:
        for method_name in ("balance", "cash", "asset_balance"):
            fn = getattr(self.hbt, method_name, None)
            if not callable(fn):
                continue
            try:
                raw = fn(asset_id)
            except TypeError:
                try:
                    raw = fn()
                except Exception:
                    continue
            except Exception:
                continue
            if isinstance(raw, (int, float, np.integer, np.floating)):
                self._last_known_balance = float(raw)
                return self._last_known_balance
        return self._last_known_balance


class StrategyHbtAdapter:
    def __init__(
        self,
        data_path: str,
        strategy_module: str,
        strategy_class: str,
        strategy_id: str,
        symbol: str,
        tick_size: float | None = None,
        lot_size: float | None = None,
        maker_fee: float = 0.0,
        taker_fee: float = 0.0,
        partial_fill: bool = True,
        price_scale: int = 10_000,
        timeout: int = 0,
        seed: int = 42,
        feature_mode: str = "stats_only",
        dispatch_feature_events: bool = False,
    ):
        import importlib

        mod = importlib.import_module(strategy_module)
        cls = getattr(mod, strategy_class)
        self.strategy = cls(strategy_id=strategy_id)
        self.adapter = HftBacktestAdapter(
            strategy=self.strategy,
            asset_symbol=symbol,
            data_path=data_path,
            tick_size=tick_size,
            lot_size=lot_size,
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            partial_fill=partial_fill,
            seed=seed,
            price_scale=price_scale,
            timeout=timeout,
            feature_mode=feature_mode,
            dispatch_feature_events=dispatch_feature_events,
        )

    def run(self):
        return self.adapter.run()


def _resolve_qty(obj: object, *attr_names: str) -> int:
    """Return the first non-None attribute from *obj*, cast to int.

    Correctly preserves legitimate zero values (``0 is not None``).
    Falls back to ``0`` when every attribute is absent.
    """
    for name in attr_names:
        val = getattr(obj, name, None)
        if val is not None:
            return int(val)
    return 0


def _call_if_exists(obj, method_name: str, *args):
    method = getattr(obj, method_name, None)
    if not callable(method):
        return obj
    try:
        return method(*args)
    except Exception:
        return obj


def _detect_wait_status_mode() -> str:
    """Detect wait_next_feed status semantics.

    hftbacktest v2.4+ returns explicit status codes:
    0=timeout, 1=end, 2=feed, 3=order response.
    Older releases returned 0 on successful advancement.
    """

    try:
        version_text = importlib_metadata.version("hftbacktest")
    except Exception:
        return "legacy"

    parts = []
    for chunk in version_text.split(".")[:2]:
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 2:
        parts.append(0)

    return "modern" if tuple(parts[:2]) >= (2, 4) else "legacy"


def _infer_tick_size_from_data(data_path: str) -> float:
    """Infer a positive tick size from research.npy or hftbt.npz price fields.

    Falls back to 1.0 when the source cannot be loaded or the price ladder
    does not expose a usable positive increment.
    """

    try:
        loaded = np.load(data_path, allow_pickle=False)
        try:
            if isinstance(loaded, np.lib.npyio.NpzFile):
                arr = np.asarray(loaded["data"])
            else:
                arr = np.asarray(loaded)
        finally:
            if hasattr(loaded, "close"):
                loaded.close()
    except Exception:
        return 1.0

    names = tuple(arr.dtype.names or ())
    price_fields = [name for name in ("px", "bid_px", "ask_px") if name in names]
    if not price_fields:
        return 1.0

    samples: list[np.ndarray] = []
    head = arr[: min(len(arr), 20_000)]
    for field in price_fields:
        col = np.asarray(head[field], dtype=np.float64)
        col = col[np.isfinite(col) & (col > 0.0)]
        if col.size:
            samples.append(col)
    if not samples:
        return 1.0

    prices = np.unique(np.concatenate(samples))
    if prices.size < 2:
        return 1.0

    diffs = np.diff(prices)
    diffs = diffs[np.isfinite(diffs) & (diffs > 0.0)]
    if diffs.size == 0:
        return 1.0

    return float(diffs.min())
