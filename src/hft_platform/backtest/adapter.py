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


class HftBacktestAdapter:
    """
    Runs a BaseStrategy instance inside HftBacktest engine.
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
        depth_levels: int = 1,
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
        self.price_scale = price_scale
        self.price_codec = PriceCodec(FixedPriceScaleProvider(price_scale))
        self._intent_seq = 0
        self._hbt_seq = 0
        self.positions = {self.symbol: 0}
        self.equity_sample_ns = int(equity_sample_ns)
        self._next_equity_sample_ns = 0
        self._last_known_balance = float(initial_balance)
        self._equity_timestamps_ns: list[int] = []
        self._equity_values: list[float] = []
        self.feature_mode = str(feature_mode or "stats_only").strip().lower()
        self.dispatch_feature_events = bool(dispatch_feature_events)
        self._lob_engine: LOBEngine | None = None
        self._feature_engine: FeatureEngine | None = None
        if self.feature_mode == "lob_feature":
            self._lob_engine = LOBEngine()
            self._feature_engine = FeatureEngine()
            try:
                setattr(self._lob_engine, "feature_engine", self._feature_engine)
            except Exception:
                pass

        # L2 depth configuration + pre-allocated buffers (Allocator Law)
        self._depth_levels = max(int(depth_levels), 1)
        self._tick_size = float(tick_size) if tick_size is not None else None
        self._bid_buf = np.zeros((self._depth_levels, 2), dtype=np.int64)
        self._ask_buf = np.zeros((self._depth_levels, 2), dtype=np.int64)

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

        if tick_size is not None:
            asset_builder = _call_if_exists(asset_builder, "tick_size", float(tick_size))
        if lot_size is not None:
            asset_builder = _call_if_exists(asset_builder, "lot_size", float(lot_size))
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

        # Context Mapping
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
            feature_tuple_source=(self._feature_engine.get_feature_tuple if self._feature_engine else None),
        )

    def run(self):
        logger.info("Starting HftBacktest simulation...")
        self._equity_timestamps_ns.clear()
        self._equity_values.clear()
        self._next_equity_sample_ns = 0

        # Initialize Strategy
        # Strategy expects on_book(ctx, event)
        # We need to bridge the loop.

        # HftBacktest v2.x loop: advance to each feed event
        while self.hbt.wait_next_feed(True, -1) == 0:
            # Current LOB State
            # We construct a mock event for the strategy
            # Efficiently accessing hbt depth

            dp = self.hbt.depth(0)
            best_bid = dp.best_bid
            best_ask = dp.best_ask
            if not (best_bid == best_bid) or not (best_ask == best_ask) or best_bid <= 0 or best_ask >= 2147483647:
                continue

            ts_ns = int(self.hbt.current_timestamp)
            event = None
            feature_event = None
            if self.feature_mode == "lob_feature" and self._lob_engine is not None:
                if self._depth_levels > 1:
                    bidask_event = self._build_l2_bidask_event(dp, ts_ns)
                else:
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
                    event = LOBStatsEvent(
                        symbol=self.symbol,
                        ts=ts_ns,
                        imbalance=0.0,
                        best_bid=int(best_bid),
                        best_ask=int(best_ask),
                        bid_depth=0,
                        ask_depth=0,
                    )
            else:
                # LOBStatsEvent auto-computes mid_price_x2/spread from best_bid/best_ask
                event = LOBStatsEvent(
                    symbol=self.symbol,
                    ts=ts_ns,
                    imbalance=0.0,
                    best_bid=int(best_bid),
                    best_ask=int(best_ask),
                    bid_depth=0,
                    ask_depth=0,
                )

            # Update Context State
            self._sync_positions()
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

    @staticmethod
    def _read_side_qty(depth_obj, side: str) -> int:
        """Extract best-level quantity for *side* ('bid' or 'ask') from a depth object."""
        return int(
            getattr(depth_obj, f"best_{side}_qty", None)
            or getattr(depth_obj, f"{side}_qty", None)
            or getattr(depth_obj, f"{side}_volume", 0)
            or 0
        )

    def _build_l1_bidask_event(self, depth_obj, ts_ns: int) -> BidAskEvent:
        self._hbt_seq += 1
        best_bid = int(getattr(depth_obj, "best_bid", 0) or 0)
        best_ask = int(getattr(depth_obj, "best_ask", 0) or 0)
        bid_qty = self._read_side_qty(depth_obj, "bid")
        ask_qty = self._read_side_qty(depth_obj, "ask")
        # Write into pre-allocated buffer (Allocator Law: no per-tick heap alloc)
        self._bid_buf[0, 0] = best_bid
        self._bid_buf[0, 1] = bid_qty
        self._ask_buf[0, 0] = best_ask
        self._ask_buf[0, 1] = ask_qty
        # Copy slice — buffer is reused next tick
        bids = self._bid_buf[:1].copy()
        asks = self._ask_buf[:1].copy()
        return BidAskEvent(
            meta=MetaData(seq=self._hbt_seq, source_ts=int(ts_ns), local_ts=int(ts_ns), topic="hbt_bidask"),
            symbol=self.symbol,
            bids=bids,
            asks=asks,
            is_snapshot=False,
        )

    def _build_l2_bidask_event(self, depth_obj, ts_ns: int) -> BidAskEvent:
        """Build L2 BidAskEvent from hftbacktest depth with up to depth_levels price levels.

        Reads levels from the depth object's internal hash map. Falls back to L1-only
        if the depth object does not expose multi-level access.
        Prices are scaled to platform int (x10000) via self.price_scale.
        """
        self._hbt_seq += 1
        best_bid_raw = float(getattr(depth_obj, "best_bid", 0) or 0)
        best_ask_raw = float(getattr(depth_obj, "best_ask", 0) or 0)

        # Zero the buffers for this tick
        self._bid_buf[:] = 0
        self._ask_buf[:] = 0

        bid_depth = getattr(depth_obj, "bid_depth", None)
        ask_depth = getattr(depth_obj, "ask_depth", None)
        tick_size = self._tick_size

        filled_bids = 0
        filled_asks = 0

        if bid_depth is not None and hasattr(bid_depth, "__getitem__"):
            # Depth object exposes a dict/mapping: bid_depth[price] -> qty
            filled_bids = self._fill_from_mapping(
                bid_depth, best_bid_raw, tick_size, self._bid_buf, descending=True
            )
        elif tick_size is not None and tick_size > 0:
            # Step from best price by tick_size, probing depth object
            filled_bids = self._fill_by_stepping(
                depth_obj, best_bid_raw, tick_size, self._bid_buf, side="bid"
            )

        if ask_depth is not None and hasattr(ask_depth, "__getitem__"):
            filled_asks = self._fill_from_mapping(
                ask_depth, best_ask_raw, tick_size, self._ask_buf, descending=False
            )
        elif tick_size is not None and tick_size > 0:
            filled_asks = self._fill_by_stepping(
                depth_obj, best_ask_raw, tick_size, self._ask_buf, side="ask"
            )

        # Fallback: at least populate L1 from best_bid/best_ask attrs
        if filled_bids == 0:
            self._bid_buf[0, 0] = int(best_bid_raw * self.price_scale)
            self._bid_buf[0, 1] = self._read_side_qty(depth_obj, "bid")
            filled_bids = 1

        if filled_asks == 0:
            self._ask_buf[0, 0] = int(best_ask_raw * self.price_scale)
            self._ask_buf[0, 1] = self._read_side_qty(depth_obj, "ask")
            filled_asks = 1

        # Copy used portion — buffer is reused next tick
        bids = self._bid_buf[: max(filled_bids, 1)].copy()
        asks = self._ask_buf[: max(filled_asks, 1)].copy()
        return BidAskEvent(
            meta=MetaData(seq=self._hbt_seq, source_ts=int(ts_ns), local_ts=int(ts_ns), topic="hbt_bidask"),
            symbol=self.symbol,
            bids=bids,
            asks=asks,
            is_snapshot=False,
        )

    def _fill_from_mapping(
        self,
        depth_map,
        best_price: float,
        tick_size: float | None,
        buf: np.ndarray,
        *,
        descending: bool,
    ) -> int:
        """Fill buf from a price->qty mapping, starting at best_price, stepping by tick_size."""
        levels = buf.shape[0]
        filled = 0
        if tick_size is None or tick_size <= 0:
            # Try to read just best level
            try:
                qty = int(depth_map[best_price])
                if qty > 0:
                    buf[0, 0] = int(best_price * self.price_scale)
                    buf[0, 1] = qty
                    filled = 1
            except (KeyError, TypeError, IndexError):
                pass
            return filled

        price = best_price
        step = -tick_size if descending else tick_size
        for _ in range(levels):
            try:
                qty = int(depth_map[price])
            except (KeyError, TypeError, IndexError):
                qty = 0
            if qty > 0:
                buf[filled, 0] = int(round(price * self.price_scale))
                buf[filled, 1] = qty
                filled += 1
            price = price + step
        return filled

    def _fill_by_stepping(
        self,
        depth_obj,
        best_price: float,
        tick_size: float,
        buf: np.ndarray,
        *,
        side: str,
    ) -> int:
        """Fill buf by stepping from best_price and probing depth object for qty at each level."""
        levels = buf.shape[0]
        filled = 0
        step = -tick_size if side == "bid" else tick_size
        price = best_price
        qty_fn = getattr(depth_obj, f"{side}_qty_at", None)
        for _ in range(levels):
            qty = 0
            if qty_fn is not None:
                try:
                    qty = int(qty_fn(price))
                except (TypeError, AttributeError):
                    qty = 0
            if qty > 0:
                buf[filled, 0] = int(round(price * self.price_scale))
                buf[filled, 1] = qty
                filled += 1
            price = price + step
        return filled

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
                self.hbt.submit_buy_order(asset_id, order_id, price, qty, tif, LIMIT)
            else:
                self.hbt.submit_sell_order(asset_id, order_id, price, qty, tif, LIMIT)

        elif intent.intent_type == IntentType.CANCEL:
            self.hbt.cancel(asset_id, int(intent.target_order_id))

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

    def _scale_price(self, symbol: str, price: float) -> int:
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
            feature_mode=feature_mode,
            dispatch_feature_events=dispatch_feature_events,
        )

    def run(self):
        return self.adapter.run()


def _call_if_exists(obj, method_name: str, *args):
    method = getattr(obj, method_name, None)
    if not callable(method):
        return obj
    try:
        return method(*args)
    except Exception:
        return obj
