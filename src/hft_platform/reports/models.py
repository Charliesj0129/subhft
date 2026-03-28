"""Data contracts for the Market Analysis Report Service.

All price fields use ScaledPrice (int x10000) per the platform Precision Law.
All dataclasses use slots=True for memory efficiency and to prevent accidental
attribute pollution (hot-path safety).
"""

from __future__ import annotations

from dataclasses import dataclass

from hft_platform.contracts.types import ScaledPrice

__all__ = [
    "Bar5m",
    "FlowBar",
    "LargeTrade",
    "DepthBar",
    "SessionData",
    "PriceLevel",
    "SignalReport",
    "Scenario",
    "KeyLevel",
    "ScenarioReport",
    "ChannelConfig",
]


@dataclass(slots=True)
class Bar5m:
    """5-minute OHLCV bar."""

    ts: str
    open: ScaledPrice
    high: ScaledPrice
    low: ScaledPrice
    close: ScaledPrice
    volume: int
    ticks: int


@dataclass(slots=True)
class FlowBar:
    """5-minute order-flow summary bar."""

    ts: str
    ticks: int
    total_vol: int
    uptick_vol: int
    downtick_vol: int
    flat_vol: int
    ud_ratio: float
    net_flow: int


@dataclass(slots=True)
class LargeTrade:
    """A single large-print trade event.

    direction: one of "buy", "sell", "unknown".
    """

    ts: str
    price: ScaledPrice
    volume: int
    direction: str


@dataclass(slots=True)
class DepthBar:
    """Hourly depth imbalance summary."""

    hour: int
    avg_bid_vol: float
    avg_ask_vol: float
    bid_ratio: float


@dataclass(slots=True)
class SessionData:
    """Full session snapshot for one symbol on one date."""

    session: str
    symbol: str
    date: str
    open: ScaledPrice
    high: ScaledPrice
    low: ScaledPrice
    close: ScaledPrice
    volume: int
    tick_count: int
    bars_5m: list[Bar5m]
    flow_5m: list[FlowBar]
    large_trades: list[LargeTrade]
    spread_dist: dict[int, int]
    depth_imbalance: list[DepthBar]


@dataclass(slots=True)
class PriceLevel:
    """A price support or resistance level with strength score.

    strength: float in [0, 1].
    """

    price: ScaledPrice
    strength: float
    reason: str


@dataclass(slots=True)
class SignalReport:
    """Derived signal analysis built from SessionData."""

    session_data: SessionData
    total_net_flow: int
    ud_ratio_session: float
    strongest_sell: FlowBar
    strongest_buy: FlowBar
    large_buy_volume: int
    large_sell_volume: int
    large_net: int
    key_large_trades: list[LargeTrade]
    supports: list[PriceLevel]
    resistances: list[PriceLevel]
    bias: str
    bias_confidence: float
    rule_scores: dict[str, float]


@dataclass(slots=True)
class Scenario:
    """A single directional scenario with probability estimate."""

    id: str
    label: str
    probability: str
    condition: str
    target: ScaledPrice
    description: str


@dataclass(slots=True)
class KeyLevel:
    """A key price level for scenario planning.

    importance: 1 (minor) to 3 (major).
    """

    price: ScaledPrice
    label: str
    importance: int
    reason: str


@dataclass(slots=True)
class ScenarioReport:
    """Full scenario plan derived from a SignalReport."""

    signal: SignalReport
    direction: str
    confidence_pct: int
    entry_zone: tuple[ScaledPrice, ScaledPrice]
    target: ScaledPrice
    stop_loss: ScaledPrice
    scenarios: list[Scenario]
    key_levels: list[KeyLevel]


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    """Immutable configuration for a report distribution channel."""

    name: str
    chat_id: str
    tier: str
    enabled: bool
