import numpy as np
from structlog import get_logger

try:
    from hftbacktest import (
        BacktestAsset,
        ConstantLatency,
        HashMapMarketDepthBacktest,
        LinearAsset,
        PowerProbQueueModel,
    )
    from hftbacktest.order import IOC, ROD, Limit

    HFTBACKTEST_AVAILABLE = True
except ImportError:
    HFTBACKTEST_AVAILABLE = False

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.core.pricing import FixedPriceScaleProvider, PriceCodec
from hft_platform.events import LOBStatsEvent
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
        self.positions = {self.symbol: 0}

        # Setup HftBacktest
        # 1. Asset
        self.asset = LinearAsset(1.0)  # Tick size 1.0 or whatever

        # 2. Latency Model
        self.latency = ConstantLatency(latency_us * 1000)  # ns

        # 3. Queue Model
        self.queue_model = PowerProbQueueModel(3.0)  # Standard assumption

        # 4. Engine
        self.hbt = HashMapMarketDepthBacktest(
            [
                BacktestAsset()
                .data([data_path])
                .linear_asset(1.0)
                .constant_latency(latency_us * 1000, latency_us * 1000)
                .power_prob_queue_model(3.0)
                .int_order_id_converter()
            ]
        )

        # Context Mapping
        self.ctx = StrategyContext(
            positions=self.positions,
            strategy_id=self.strategy.strategy_id,
            intent_factory=self._intent_factory,
            price_scaler=self._scale_price,
            lob_source=None,
        )

    def run(self):
        logger.info("Starting HftBacktest simulation...")

        # Initialize Strategy
        # Strategy expects on_book(ctx, event)
        # We need to bridge the loop.

        # HftBacktest loop
        while self.hbt.run():
            if not self.hbt.elapse(1):  # Granularity?
                continue

            # Current LOB State
            # We construct a mock event for the strategy
            # Efficiently accessing hbt depth

            dp = self.hbt.depth(0)
            best_bid = dp.best_bid
            best_ask = dp.best_ask
            if best_bid == 0 or best_ask == 2147483647:
                continue

            # LOBStatsEvent auto-computes mid_price_x2/spread from best_bid/best_ask
            event = LOBStatsEvent(
                symbol=self.symbol,
                ts=int(self.hbt.current_timestamp),
                imbalance=0.0,
                best_bid=int(best_bid),
                best_ask=int(best_ask),
                bid_depth=0,
                ask_depth=0,
            )

            # Update Context State
            self._sync_positions()

            # Call Strategy
            intents = self.strategy.handle_event(self.ctx, event)

            # Execute Intents
            for intent in intents:
                self.execute_intent(intent)

        return self.hbt.close()

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

    def execute_intent(self, intent):
        # Convert Intent -> HftBacktest Order
        # hbt.submit_buy_order(asset_id, order_id, price, qty, time_in_force, exec_type)

        asset_id = 0
        order_id = intent.intent_id
        price = self.price_codec.descale(intent.symbol, intent.price)
        qty = intent.qty
        tif = ROD if intent.tif == TIF.LIMIT else IOC  # Mapping

        if intent.intent_type == IntentType.NEW:
            if intent.side == Side.BUY:
                self.hbt.submit_buy_order(asset_id, order_id, price, qty, tif, Limit)
            else:
                self.hbt.submit_sell_order(asset_id, order_id, price, qty, tif, Limit)

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
        price_scale: int = 10_000,
        timeout: int = 0,
        seed: int = 42,
    ):
        import importlib

        mod = importlib.import_module(strategy_module)
        cls = getattr(mod, strategy_class)
        self.strategy = cls(strategy_id=strategy_id)
        self.adapter = HftBacktestAdapter(
            strategy=self.strategy,
            asset_symbol=symbol,
            data_path=data_path,
            seed=seed,
            price_scale=price_scale,
        )

    def run(self):
        return self.adapter.run()
