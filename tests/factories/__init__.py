"""Shared test factories for HFT platform events, order objects, and runtime components."""

from tests.factories.components import (
    make_normalizer,
    make_position_store,
    make_risk_engine,
    make_storm_guard,
)
from tests.factories.events import (
    make_bidask_event,
    make_fill_event,
    make_lob_stats_event,
    make_tick_event,
)
from tests.factories.intents import (
    make_order_command,
    make_order_intent,
    make_risk_config,
)

__all__ = [
    "make_bidask_event",
    "make_fill_event",
    "make_lob_stats_event",
    "make_normalizer",
    "make_order_command",
    "make_order_intent",
    "make_position_store",
    "make_risk_config",
    "make_risk_engine",
    "make_storm_guard",
    "make_tick_event",
]
