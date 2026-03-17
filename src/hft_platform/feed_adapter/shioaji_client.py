# DEPRECATED: Use hft_platform.feed_adapter.shioaji.client instead.
# This module is kept for backward compatibility and will be removed in a future release.
#
# All public symbols are re-exported from the canonical location so that
# existing ``from hft_platform.feed_adapter.shioaji_client import X`` and
# ``patch("hft_platform.feed_adapter.shioaji_client.sj")`` continue to work.

from hft_platform.feed_adapter.shioaji.client import (  # noqa: F401
    _ROUTE_MISS_COUNT,
    _ROUTE_MISS_FALLBACK_MODE,
    _ROUTE_MISS_LOG_EVERY,
    _ROUTE_MISS_STRICT,
    CLIENT_DISPATCH_BY_CODE_SNAPSHOT,
    CLIENT_DISPATCH_SNAPSHOT,
    CLIENT_DISPATCH_WILDCARD_SNAPSHOT,
    CLIENT_REGISTRY,
    CLIENT_REGISTRY_BY_CODE,
    CLIENT_REGISTRY_BY_CODE_SNAPSHOT,
    CLIENT_REGISTRY_LOCK,
    CLIENT_REGISTRY_SNAPSHOT,
    CLIENT_REGISTRY_WILDCARD_SNAPSHOT,
    TOPIC_CODE_CACHE,
    ShioajiClient,
    _extract_code_from_topic,
    _record_route_metric,
    _registry_rebind_codes,
    _registry_register,
    _registry_snapshot,
    _registry_unregister,
    _sync_router_route_globals,
    dispatch_tick_cb,
    fcntl,
    logger,
    sj,
)

__all__ = ["ShioajiClient", "dispatch_tick_cb"]
