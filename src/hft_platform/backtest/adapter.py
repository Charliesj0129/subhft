"""HftBacktestAdapter — runs a BaseStrategy inside HftBacktest engine.

Decomposed (WU-01): run loops extracted to ``_feed_loop`` and ``_elapse_loop``,
shared helpers to ``_hbt_utils``.

Constitution fixes:
  - WU-02: Fill log uses pre-allocated SoA numpy arrays (Allocator + Cache Law)
  - WU-03: Equity sampling uses pre-allocated numpy arrays (Allocator Law)
  - WU-04: mid_price_x2 integer throughout (Precision Law)
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
from structlog import get_logger

try:
    from hftbacktest import (
        BacktestAsset,
        HashMapMarketDepthBacktest,
    )
    from hftbacktest.order import GTC, IOC, LIMIT

    HFTBACKTEST_AVAILABLE = True
except ImportError:
    HFTBACKTEST_AVAILABLE = False

from hft_platform.backtest._elapse_loop import run_elapse
from hft_platform.backtest._feed_loop import run_feed
from hft_platform.backtest._hbt_utils import (
    _MODERN_DEFAULT_WAIT_TIMEOUT,
    call_if_exists,
    detect_wait_status_mode,
    infer_tick_size_from_data,
    resolve_qty,
)
from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.core.pricing import FixedPriceScaleProvider, PriceCodec
from hft_platform.events import BidAskEvent, MetaData
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.strategy.base import BaseStrategy, StrategyContext

logger = get_logger("hbt_adapter")

_FILL_CAPACITY = 10_000
_EQUITY_CAPACITY = 100_000

# Backward-compat aliases for test monkeypatching
_detect_wait_status_mode = detect_wait_status_mode
_resolve_qty = resolve_qty
_call_if_exists = call_if_exists
_infer_tick_size_from_data = infer_tick_size_from_data


class HftBacktestAdapter:
    """Runs a BaseStrategy instance inside HftBacktest engine."""

    def __init__(
        self,
        strategy: BaseStrategy,
        asset_symbol: str,
        data_path: str,
        latency_us: int = 100,
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
        risk_config: BacktestRiskConfig | None = None,
    ):
        if not HFTBACKTEST_AVAILABLE:
            raise ImportError("hftbacktest not installed")

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
        self.positions: dict[str, int] = {self.symbol: 0}
        self._prev_position = 0
        self._total_buy_fills = 0
        self._total_sell_fills = 0

        # WU-02: SoA fill log
        self._fill_ts_ns = np.zeros(_FILL_CAPACITY, dtype=np.int64)
        self._fill_delta = np.zeros(_FILL_CAPACITY, dtype=np.int32)
        self._fill_position_after = np.zeros(_FILL_CAPACITY, dtype=np.int32)
        self._fill_mid_price_x2 = np.zeros(_FILL_CAPACITY, dtype=np.int64)
        self._fill_count: int = 0

        self.equity_sample_ns = int(equity_sample_ns)
        self._next_equity_sample_ns = 0
        self._last_known_balance = float(initial_balance)

        # WU-03: Pre-allocated equity buffers
        self._equity_ts_buf = np.zeros(_EQUITY_CAPACITY, dtype=np.int64)
        self._equity_val_buf = np.zeros(_EQUITY_CAPACITY, dtype=np.float64)
        self._equity_count: int = 0

        # Risk evaluator (opt-in)
        _REJECT_CAPACITY = 256
        self._reject_ts_ns = np.zeros(_REJECT_CAPACITY, dtype=np.int64)
        self._reject_reasons: list[str] = []
        self._reject_count: int = 0
        if risk_config is not None and risk_config.enabled:
            self._risk_evaluator: BacktestRiskEvaluator | None = BacktestRiskEvaluator(
                risk_config,
                position_provider=lambda sym, sid: self.positions.get(sym, 0),
                price_scale_provider=None,
            )
        else:
            self._risk_evaluator = None

        self._wait_status_mode = detect_wait_status_mode()
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
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                pass

        resolved_tick_size = float(tick_size) if tick_size is not None else infer_tick_size_from_data(data_path)
        resolved_lot_size = float(lot_size) if lot_size is not None else 1.0

        asset_builder = BacktestAsset().data([data_path]).linear_asset(1.0)
        latency_model_lower = str(latency_model).strip().lower()
        if latency_model_lower == "intporderlatency" and latency_data_path:
            asset_builder = call_if_exists(asset_builder, "intp_order_latency", latency_data_path)
        else:
            # hftbacktest's constant_order_latency does not support per-action-type
            # latencies (place vs modify vs cancel). When modify_latency_us or
            # cancel_latency_us are provided, we use the maximum of all three as a
            # conservative approximation so that no action type underestimates its
            # real round-trip time.  A warning is emitted so researchers are aware
            # of this approximation and can account for it in their analysis.
            _mod = self.modify_latency_us
            _can = self.cancel_latency_us
            if _mod > 0 or _can > 0:
                effective_latency_us = max(latency_us, _mod, _can)
                if effective_latency_us != latency_us:
                    logger.warning(
                        "backtest_latency_approximation",
                        place_latency_us=latency_us,
                        modify_latency_us=_mod,
                        cancel_latency_us=_can,
                        effective_latency_us=effective_latency_us,
                        reason=(
                            "hftbacktest constant_order_latency does not support "
                            "per-action-type latencies; using max(place, modify, cancel) "
                            "as a conservative approximation for all order actions"
                        ),
                    )
            else:
                effective_latency_us = latency_us
            lat_ns = effective_latency_us * 1000
            asset_builder = call_if_exists(asset_builder, "constant_order_latency", lat_ns, lat_ns)

        queue_model_lower = str(queue_model).strip().lower()
        if "riskadverse" in queue_model_lower or "risk_adverse" in queue_model_lower:
            asset_builder = call_if_exists(asset_builder, "risk_adverse_queue_model")
        elif "logprob" in queue_model_lower:
            asset_builder = call_if_exists(asset_builder, "log_prob_queue_model")
        elif "l3fifo" in queue_model_lower:
            asset_builder = call_if_exists(asset_builder, "l3_fifo_queue_model")
        else:
            import re

            m = re.search(r"[\d.]+", str(queue_model))
            exponent = float(m.group()) if m else 3.0
            asset_builder = call_if_exists(asset_builder, "power_prob_queue_model", exponent)

        asset_builder = call_if_exists(asset_builder, "tick_size", resolved_tick_size)
        asset_builder = call_if_exists(asset_builder, "lot_size", resolved_lot_size)
        if maker_fee or taker_fee:
            asset_builder = call_if_exists(asset_builder, "trading_value_fee_model", float(maker_fee), float(taker_fee))

        exchange_model_lower = str(exchange_model).strip().lower()
        if "partialfill" in exchange_model_lower and "no" not in exchange_model_lower:
            asset_builder = call_if_exists(asset_builder, "partial_fill_exchange")
        elif partial_fill and "nopartialfill" not in exchange_model_lower:
            asset_builder = call_if_exists(asset_builder, "partial_fill_exchange")
        else:
            asset_builder = call_if_exists(asset_builder, "no_partial_fill_exchange")
        asset_builder = call_if_exists(asset_builder, "int_order_id_converter")

        self.hbt = HashMapMarketDepthBacktest([asset_builder])

        self._feature_array_lookup = None
        if self._feature_array_source is not None:
            ts_arr, feat_arr = self._feature_array_source
            self._feature_array_lookup = self._make_feature_lookup(ts_arr, feat_arr)

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

    def run(self) -> object:
        if self.tick_mode == "elapse":
            return run_elapse(self)
        return run_feed(self)

    def _record_fill(self, ts_ns: int, delta: int, position_after: int, mid_price_x2: int) -> None:
        idx = self._fill_count
        if idx >= self._fill_ts_ns.size:
            new_cap = self._fill_ts_ns.size * 2
            self._fill_ts_ns = np.resize(self._fill_ts_ns, new_cap)
            self._fill_delta = np.resize(self._fill_delta, new_cap)
            self._fill_position_after = np.resize(self._fill_position_after, new_cap)
            self._fill_mid_price_x2 = np.resize(self._fill_mid_price_x2, new_cap)
        self._fill_ts_ns[idx] = ts_ns
        self._fill_delta[idx] = delta
        self._fill_position_after[idx] = position_after
        self._fill_mid_price_x2[idx] = mid_price_x2
        self._fill_count = idx + 1

    def _record_rejection(self, intent: OrderIntent, reason: str) -> None:
        """Record a risk rejection in SoA buffers."""
        from hft_platform.core import timebase

        if self._reject_count >= len(self._reject_ts_ns):
            new_cap = len(self._reject_ts_ns) * 2
            new_buf = np.zeros(new_cap, dtype=np.int64)
            new_buf[: len(self._reject_ts_ns)] = self._reject_ts_ns
            self._reject_ts_ns = new_buf
        self._reject_ts_ns[self._reject_count] = timebase.now_ns()
        self._reject_reasons.append(reason)
        self._reject_count += 1

    def _reset_equity_buffers(self) -> None:
        self._equity_count = 0
        self._next_equity_sample_ns = 0

    def _maybe_record_equity_point(self, ts_ns: int, best_bid: int, best_ask: int) -> None:
        if self.equity_sample_ns <= 0:
            return
        if ts_ns < self._next_equity_sample_ns:
            return
        self._next_equity_sample_ns = ts_ns + self.equity_sample_ns
        mid_price_x2 = best_bid + best_ask
        position = self.positions.get(self.symbol, 0)
        balance = self._read_balance(0)
        equity = balance + (position * mid_price_x2 / 2.0)
        idx = self._equity_count
        if idx >= self._equity_ts_buf.size:
            new_cap = self._equity_ts_buf.size * 2
            self._equity_ts_buf = np.resize(self._equity_ts_buf, new_cap)
            self._equity_val_buf = np.resize(self._equity_val_buf, new_cap)
        self._equity_ts_buf[idx] = ts_ns
        self._equity_val_buf[idx] = equity
        self._equity_count = idx + 1

    @property
    def fill_stats(self) -> dict:
        n = self._fill_count
        total_fills = self._total_buy_fills + self._total_sell_fills
        adverse_selections: np.ndarray | None = None
        if n >= 2:
            mid_x2 = self._fill_mid_price_x2[:n]
            deltas = self._fill_delta[:n]
            mid_change = np.diff(mid_x2).astype(np.float64) / 2.0
            signs = np.where(deltas[:-1] > 0, -1.0, 1.0)
            adverse_selections = signs * mid_change
        if n >= 2:
            duration_ns = int(self._fill_ts_ns[n - 1]) - int(self._fill_ts_ns[0])
            duration_hours = duration_ns / 3.6e12
        elif self._equity_count >= 2:
            duration_ns = int(self._equity_ts_buf[self._equity_count - 1]) - int(self._equity_ts_buf[0])
            duration_hours = duration_ns / 3.6e12
        else:
            duration_hours = 0.0
        return {
            "buy_fills": self._total_buy_fills,
            "sell_fills": self._total_sell_fills,
            "total_fills": total_fills,
            "fill_rate_per_hour": total_fills / duration_hours if duration_hours > 0 else 0.0,
            "adverse_selection_mean": float(np.mean(adverse_selections)) if adverse_selections is not None else 0.0,
            "adverse_selection_median": float(np.median(adverse_selections)) if adverse_selections is not None else 0.0,
            "n_fill_events": n,
        }

    @property
    def equity_timestamps_ns(self) -> np.ndarray:
        return self._equity_ts_buf[: self._equity_count]

    @property
    def equity_values(self) -> np.ndarray:
        return self._equity_val_buf[: self._equity_count]

    def get_mid_price(self) -> float:
        dp = self.hbt.depth(0)
        bid, ask = dp.best_bid, dp.best_ask
        if bid == 0 or ask == 2147483647:
            return float("nan")
        return (bid + ask) / 2.0

    def get_mid_price_x2(self) -> int:
        dp = self.hbt.depth(0)
        bid_scaled = int(round(float(dp.best_bid) * self.price_scale))
        ask_scaled = int(round(float(dp.best_ask) * self.price_scale))
        return bid_scaled + ask_scaled

    def get_spread(self) -> float:
        dp = self.hbt.depth(0)
        return dp.best_ask - dp.best_bid

    def _make_feature_lookup(self, timestamps: np.ndarray, features: np.ndarray):
        idx = [0]

        def lookup(symbol: str) -> tuple:
            ts = int(self.hbt.current_timestamp)
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
                raise TimeoutError(f"hftbacktest wait_next_feed timed out: timeout={self.timeout}")
            raise RuntimeError(f"Unexpected hftbacktest wait_next_feed status: {status}")
        if status == 0:
            return True
        raise RuntimeError(f"Unexpected legacy hftbacktest wait_next_feed status: {status}")

    def _build_l1_bidask_event(self, depth_obj: object, ts_ns: int) -> BidAskEvent:
        self._hbt_seq += 1
        raw_bid = float(getattr(depth_obj, "best_bid", 0) or 0)
        raw_ask = float(getattr(depth_obj, "best_ask", 0) or 0)
        best_bid = int(round(raw_bid * self.price_scale))
        best_ask = int(round(raw_ask * self.price_scale))
        bid_qty = resolve_qty(depth_obj, "best_bid_qty", "bid_qty", "bid_volume")
        ask_qty = resolve_qty(depth_obj, "best_ask_qty", "ask_qty", "ask_volume")
        bids = np.asarray([[best_bid, bid_qty]], dtype=np.int64)
        asks = np.asarray([[best_ask, ask_qty]], dtype=np.int64)
        return BidAskEvent(
            meta=MetaData(seq=self._hbt_seq, source_ts=int(ts_ns), local_ts=int(ts_ns), topic="hbt_bidask"),
            symbol=self.symbol,
            bids=bids,
            asks=asks,
            is_snapshot=False,
        )

    def execute_intent(self, intent: OrderIntent) -> None:
        price = self.price_codec.descale(intent.symbol, intent.price)
        tif = GTC if intent.tif == TIF.LIMIT else IOC
        if intent.intent_type == IntentType.NEW:
            if intent.side == Side.BUY:
                self.hbt.submit_buy_order(0, intent.intent_id, price, intent.qty, tif, LIMIT, False)
            else:
                self.hbt.submit_sell_order(0, intent.intent_id, price, intent.qty, tif, LIMIT, False)
        elif intent.intent_type == IntentType.CANCEL:
            if intent.target_order_id is not None:
                self.hbt.cancel(0, int(intent.target_order_id), False)

    def _intent_factory(self, strategy_id, symbol, side, price, qty, tif, intent_type, target_order_id=None, **_kw):
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

    def _sync_positions(self) -> None:
        try:
            self.positions[self.symbol] = self.hbt.position(0)
        except Exception as e:
            logger.error("Failed to sync position", symbol=self.symbol, error=str(e), error_type=type(e).__name__)

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
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    continue
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                continue
            if isinstance(raw, (int, float, np.integer, np.floating)):
                self._last_known_balance = float(raw)
                return self._last_known_balance
        return self._last_known_balance

    @property
    def _fill_log(self) -> list[dict]:
        n = self._fill_count
        return [
            {
                "ts_ns": int(self._fill_ts_ns[i]),
                "delta": int(self._fill_delta[i]),
                "position_after": int(self._fill_position_after[i]),
                "mid_price": float(self._fill_mid_price_x2[i]) / 2.0,
                "mid_price_x2": int(self._fill_mid_price_x2[i]),
            }
            for i in range(n)
        ]

    @property
    def _equity_timestamps_ns(self) -> list[int]:
        return self._equity_ts_buf[: self._equity_count].tolist()

    @property
    def _equity_values(self) -> list[float]:
        return self._equity_val_buf[: self._equity_count].tolist()


class StrategyHbtAdapter:
    """Dynamic strategy loader for HftBacktest. Prefer HftBacktestAdapter directly."""

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
        tick_mode: str = "feed",
        elapse_ns: int = 100_000_000,
        feature_array_source: tuple[np.ndarray, np.ndarray] | None = None,
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
            tick_mode=tick_mode,
            elapse_ns=elapse_ns,
            feature_array_source=feature_array_source,
        )

    def run(self):
        return self.adapter.run()
