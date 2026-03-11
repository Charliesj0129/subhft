"""Shioaji adapter submodules.

Import concrete classes from their dedicated modules, e.g.:
`from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade`.
"""

__all__: list[str] = []

# Auto-register Shioaji as a broker factory
try:
    from hft_platform.feed_adapter.broker_registry import register_broker
    from hft_platform.feed_adapter.shioaji.factory import ShioajiBrokerFactory

    register_broker("shioaji", ShioajiBrokerFactory())
except ImportError:
    pass  # broker_registry not yet available
