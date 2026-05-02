"""Shared Shioaji quote-subscription limits.

Two limits live here, both in **codes** (not broker topics):

1. ``DEFAULT_MAX_SUBSCRIPTIONS_PER_CONN`` (120) — per quote connection.
   Each subscribed code triggers 2 broker topics (``QuoteType.Tick`` +
   ``QuoteType.BidAsk`` — see ``subscription_manager._subscribe_symbol``).
   Solace per-session topic budget is ~250–256 in practice for SinoPac
   retail accounts (empirically measured 2026-04-26: conn 0 with 163
   codes → 326 topics → broker accepted ~127 codes (254 topics) and
   rejected the rest with "Max Num Subscriptions Exceeded" on session
   ``(c1,s1)_sinopac``). 120 codes × 2 = 240 topics keeps a small
   headroom under that ceiling. Used by ``QuoteConnectionPool`` to shard
   the universe across N connections.

2. ``DEFAULT_MAX_SUBSCRIPTIONS_PER_CLIENT`` (600, env-overridable via
   ``HFT_MAX_SUBSCRIPTIONS``) — per ``ShioajiClient`` preflight ceiling.
   Used by ``ShioajiClient._load_config`` to bound the total universe
   the client will accept. The actual per-conn topic budget is enforced
   by the pool sharding layer above; this is the **upper bound** for
   the entire universe (post-2026-04-27 = 588 contracts).

Override per-client preflight via ``HFT_MAX_SUBSCRIPTIONS`` env var when
the universe grows. Set ``HFT_STRICT_SUBSCRIPTION_LIMIT=1`` to make
``_load_config`` raise instead of truncate-with-warn (truncate-with-warn
is the new default after RC-1 2026-04-27).
"""

from __future__ import annotations

import os

# Per-quote-connection cap (broker Solace topic budget). Do NOT raise
# without re-measuring the broker cap — see module docstring.
DEFAULT_MAX_SUBSCRIPTIONS_PER_CONN: int = 120


def _resolve_default_max_subscriptions_per_client(default: int = 600) -> int:
    """Resolve ``HFT_MAX_SUBSCRIPTIONS`` env var with a safe fallback.

    Returns the integer value of ``HFT_MAX_SUBSCRIPTIONS`` if set and
    parseable as ``>= 1``; otherwise returns ``default`` (600 codes).
    """
    raw = os.getenv("HFT_MAX_SUBSCRIPTIONS")
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


# Per-ShioajiClient preflight ceiling (universe bound, env-overridable).
DEFAULT_MAX_SUBSCRIPTIONS_PER_CLIENT: int = _resolve_default_max_subscriptions_per_client()
