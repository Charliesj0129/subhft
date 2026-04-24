import unittest

from hft_platform.contracts.execution import OrderEvent, OrderStatus
from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy, StrategyContext


# Mock Strategy implementation specifically for test
class MockStrategy(BaseStrategy):
    def on_stats(self, event: LOBStatsEvent):
        # Logic: If spread > 1, place Buy/Sell
        # Use mid_price_scaled for integer price (avoid float)
        if event.spread > 1:
            mid = event.mid_price_scaled
            self.buy(event.symbol, mid - 1, 1)
            self.sell(event.symbol, mid + 1, 1)


class MockCancelStrategy(BaseStrategy):
    def on_stats(self, event: LOBStatsEvent):
        self.cancel(event.symbol, "OID1")
        self.cancel(event.symbol, "OID1")


class TestStrategySDK(unittest.TestCase):
    def test_simple_strategy_logic(self):
        # 1. Setup
        strat = MockStrategy("test-strat", symbols=["2330"])

        # Mock Context
        # We need a fully functional context because BaseStrategy uses it to create intents
        # But constructing intents requires the factory.

        # Real Intent Factory logic for testing
        def mock_intent_factory(strategy_id, symbol, side, price, qty, tif, intent_type, target_order_id=None, **_kw):
            return OrderIntent(
                intent_id=1,
                strategy_id=strategy_id,
                symbol=symbol,
                side=side,
                price=price,
                qty=qty,
                tif=tif,
                intent_type=intent_type,
                target_order_id=target_order_id,
                timestamp_ns=0,
            )

        ctx = StrategyContext(
            positions={"2330": 0},
            strategy_id="test-strat",
            intent_factory=mock_intent_factory,
            price_scaler=lambda sym, p: int(p),  # No scale for test
            lob_source=None,
        )

        # 2. Simulate Event (LOB Stats)
        # With backward-compatible interface: provide best_bid/best_ask, mid_price/spread are auto-computed
        event = LOBStatsEvent(
            symbol="2330",
            ts=1000,
            imbalance=0.0,
            best_bid=99,
            best_ask=101,
            bid_depth=10,
            ask_depth=10,
        )

        # 3. Execution
        generated_intents = strat.handle_event(ctx, event)

        # 4. Verification
        self.assertEqual(len(generated_intents), 2)

        # Check Buy
        buy_intent = next(i for i in generated_intents if i.side == Side.BUY)
        self.assertEqual(buy_intent.symbol, "2330")
        self.assertEqual(buy_intent.price, 99.0)  # 100.0 - 1

        # Check Sell
        sell_intent = next(i for i in generated_intents if i.side == Side.SELL)
        self.assertEqual(sell_intent.price, 101.0)  # 100.0 + 1

        print("SDK Test Passed: Generated correct intent from high-level API.")

    def test_cancel_for_same_target_is_suppressed_until_terminal(self):
        strat = MockCancelStrategy("test-strat", symbols=["2330"])

        next_intent_id = {"value": 0}

        def mock_intent_factory(strategy_id, symbol, side, price, qty, tif, intent_type, target_order_id=None, **_kw):
            next_intent_id["value"] += 1
            return OrderIntent(
                intent_id=next_intent_id["value"],
                strategy_id=strategy_id,
                symbol=symbol,
                side=side,
                price=price,
                qty=qty,
                tif=tif,
                intent_type=intent_type,
                target_order_id=target_order_id,
                timestamp_ns=0,
            )

        ctx = StrategyContext(
            positions={"2330": 0},
            strategy_id="test-strat",
            intent_factory=mock_intent_factory,
            price_scaler=lambda sym, p: int(p),
            lob_source=None,
        )
        event = LOBStatsEvent(
            symbol="2330",
            ts=1000,
            imbalance=0.0,
            best_bid=99,
            best_ask=101,
            bid_depth=10,
            ask_depth=10,
        )

        first = strat.handle_event(ctx, event)
        second = strat.handle_event(ctx, event)

        self.assertEqual([i.intent_type for i in first], [IntentType.CANCEL])
        self.assertEqual(first[0].target_order_id, "OID1")
        self.assertEqual(second, [])

        terminal = OrderEvent(
            order_id="OID1",
            strategy_id="test-strat",
            symbol="2330",
            status=OrderStatus.CANCELLED,
            submitted_qty=1,
            filled_qty=0,
            remaining_qty=0,
            price=99,
            side=Side.BUY,
            ingest_ts_ns=0,
            broker_ts_ns=0,
        )
        strat.handle_event(ctx, terminal)

        after_terminal = strat.handle_event(ctx, event)
        self.assertEqual([i.intent_type for i in after_terminal], [IntentType.CANCEL])


if __name__ == "__main__":
    unittest.main()
