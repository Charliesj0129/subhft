"""Shared broker abstractions for the feed_adapter module.

Provides base classes and protocols that capture common patterns across
broker implementations (Shioaji, Fubon, etc.) without modifying the
existing concrete classes.
"""

from hft_platform.feed_adapter._base.quote_runtime import (
    BaseQuoteWatchdog,
    QuoteRuntimeProtocol,
)
from hft_platform.feed_adapter._base.session_runtime import BaseBrokerSessionRuntime
from hft_platform.feed_adapter._base.subscription_manager import (
    CooldownManager,
    SubscriptionManagerProtocol,
)

__all__ = [
    "BaseBrokerSessionRuntime",
    "BaseQuoteWatchdog",
    "CooldownManager",
    "QuoteRuntimeProtocol",
    "SubscriptionManagerProtocol",
]
