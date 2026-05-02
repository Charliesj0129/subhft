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
    # Layer 1: Facts
    "SegmentFact",
    "ChipCluster",
    "ChipFacts",
    "FlowFacts",
    "StructureFacts",
    "VolatilityFacts",
    "DaySnapshot",
    "CrossDayFacts",
    "FactReport",
    # Layer 2: Reasoning
    "Evidence",
    "BiasJudgment",
    "EnrichedLevel",
    "NarrativeReport",
    "ReasoningReport",
    # Layer 3: Composition
    "MessagePart",
    "ComposedReport",
]


@dataclass(frozen=True, slots=True)
class Bar5m:
    """5-minute OHLCV bar."""

    ts: str
    open: ScaledPrice
    high: ScaledPrice
    low: ScaledPrice
    close: ScaledPrice
    volume: int
    ticks: int


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class LargeTrade:
    """A single large-print trade event.

    direction: one of "buy", "sell", "unknown".
    """

    ts: str
    price: ScaledPrice
    volume: int
    direction: str


@dataclass(frozen=True, slots=True)
class DepthBar:
    """Hourly depth imbalance summary."""

    hour: int
    avg_bid_vol: float
    avg_ask_vol: float
    bid_ratio: float


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class PriceLevel:
    """A price support or resistance level with strength score.

    strength: float in [0, 1].
    """

    price: ScaledPrice
    strength: float
    reason: str


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class Scenario:
    """A single directional scenario with probability estimate."""

    id: str
    label: str
    probability: str
    condition: str
    target: ScaledPrice
    description: str


@dataclass(frozen=True, slots=True)
class KeyLevel:
    """A key price level for scenario planning.

    importance: 1 (minor) to 3 (major).
    """

    price: ScaledPrice
    label: str
    importance: int
    reason: str


@dataclass(frozen=True, slots=True)
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


# ---------------------------------------------------------------------------
# Three-Layer Architecture Models (Layer 1: Facts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SegmentFact:
    """Facts about a single time segment within a session."""

    name: str  # "pre_open" / "opening" / "midday" / "closing"
    time_range: str
    ud_ratio: float
    net_flow: int
    volume: int
    volume_pct: float
    large_buy_count: int
    large_sell_count: int
    high: int
    low: int
    dominant_side: str  # "bull" / "bear" / "neutral"


@dataclass(frozen=True, slots=True)
class ChipCluster:
    """A cluster of large trades near a price level."""

    price_center: int
    price_range: tuple[int, int]
    buy_volume: int
    sell_volume: int
    trade_count: int
    dominant_side: str
    first_ts: str
    last_ts: str
    time_range: str


@dataclass(frozen=True, slots=True)
class ChipFacts:
    """Aggregated chip structure from large trades + volume-at-price."""

    clusters: list[ChipCluster]
    vap_peaks: list[PriceLevel]
    buy_zone: tuple[int, int] | None
    sell_zone: tuple[int, int] | None
    total_buy_volume: int
    total_sell_volume: int
    net_ratio: float


@dataclass(frozen=True, slots=True)
class FlowFacts:
    """Session-level order flow facts."""

    session_ud: float
    session_net_flow: int
    strongest_buy_bar: FlowBar
    strongest_sell_bar: FlowBar
    sustained_runs: list[tuple[str, int, str]]
    volume_spikes: list[tuple[FlowBar, float]]
    eod_ud: float
    eod_drift: float


@dataclass(frozen=True, slots=True)
class StructureFacts:
    """Price structure facts."""

    double_bottoms: list[PriceLevel]
    double_tops: list[PriceLevel]
    failed_breakouts: list[PriceLevel]
    round_numbers: list[PriceLevel]
    session_high: PriceLevel
    session_low: PriceLevel


@dataclass(frozen=True, slots=True)
class VolatilityFacts:
    """Volatility metrics derived from 5m bars."""

    atr_5m: int
    session_range: int
    range_atr_ratio: float
    atr_session: int


@dataclass(frozen=True, slots=True)
class DaySnapshot:
    """Summary of a single previous trading day/session."""

    date: str
    session: str
    open: int
    high: int
    low: int
    close: int
    volume: int
    ud_ratio: float
    net_flow: int


@dataclass(frozen=True, slots=True)
class CrossDayFacts:
    """Cross-day comparison facts."""

    prev_days: list[DaySnapshot]
    volume_change_pct: float
    price_position: str
    trend_direction: str
    flow_reversal: bool


@dataclass(frozen=True, slots=True)
class FactReport:
    """Complete Layer 1 output."""

    session_data: SessionData
    segments: list[SegmentFact]
    chips: ChipFacts
    flow: FlowFacts
    structure: StructureFacts
    volatility: VolatilityFacts
    cross_day: CrossDayFacts


# ---------------------------------------------------------------------------
# Three-Layer Architecture Models (Layer 2: Reasoning)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Evidence:
    """A single piece of evidence for bias determination."""

    source: str
    fact_value: str
    direction: str
    weight: float


@dataclass(frozen=True, slots=True)
class BiasJudgment:
    """Overall market bias with evidence chain."""

    bias: str
    confidence: float
    evidences: list[Evidence]
    summary: str


@dataclass(frozen=True, slots=True)
class EnrichedLevel:
    """Support/resistance level with confluence information."""

    price: int
    side: str
    strength: float
    sources: list[str]
    confluence_count: int


@dataclass(frozen=True, slots=True)
class NarrativeReport:
    """Time-segment narrative output."""

    storyline: list[str]
    turning_points: list[tuple[str, str]]
    conclusion: str


@dataclass(frozen=True, slots=True)
class ReasoningReport:
    """Complete Layer 2 output."""

    bias: BiasJudgment
    levels: list[EnrichedLevel]
    scenarios: list[Scenario]
    narrative: NarrativeReport


# ---------------------------------------------------------------------------
# Three-Layer Architecture Models (Layer 3: Composition)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MessagePart:
    """A single part of the composed report."""

    kind: str
    content: str
    image: bytes | None = None
    caption: str = ""
    min_tier: str = "free"


@dataclass(frozen=True, slots=True)
class ComposedReport:
    """Complete Layer 3 output."""

    messages: list[MessagePart]
