"""Fubon Securities broker adapter. Selected via HFT_BROKER=fubon."""

try:
    from hft_platform.feed_adapter.broker_registry import register_broker
    from hft_platform.feed_adapter.fubon.factory import FubonBrokerFactory

    register_broker("fubon", FubonBrokerFactory())
except ImportError:
    pass
