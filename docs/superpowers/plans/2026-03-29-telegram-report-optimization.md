# Telegram Report Optimization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat 8-rule weighted report pipeline with a three-layer analysis architecture (FactExtractor → Reasoner → ReportComposer) that produces deeper, cross-referencing analysis with time-segment narrative, chip structure, cross-day context, and a flow heatmap.

**Architecture:** Layer 1 extracts 6 pure-fact groups from SessionData. Layer 2 runs 4 reasoners that cross-reference facts to produce bias, levels, scenarios, and narrative. Layer 3 composes 8 text messages + 1 heatmap image into a `ComposedReport` with tier-aware `MessagePart`s. The pipeline, distributor, and bot handlers are rewired to use the new contract.

**Tech Stack:** Python 3.12, ClickHouse, matplotlib, python-telegram-bot, structlog, dataclasses with `__slots__`

**Spec:** `docs/superpowers/specs/2026-03-29-telegram-report-optimization-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/hft_platform/reports/models.py` | MODIFY | Add 15 new dataclasses for facts, reasoning, composition |
| `src/hft_platform/reports/facts.py` | CREATE | Layer 1 — 6 fact extractors + `extract_all()` |
| `src/hft_platform/reports/reasoner.py` | CREATE | Layer 2 — 4 reasoners + `reason_all()` |
| `src/hft_platform/reports/composer.py` | CREATE | Layer 3 — ReportComposer → `ComposedReport` |
| `src/hft_platform/reports/heatmap.py` | CREATE | Flow heatmap PNG generation |
| `src/hft_platform/reports/collector.py` | MODIFY | Add Q7 cross-day query + `collect_cross_day()` |
| `src/hft_platform/reports/pipeline.py` | MODIFY | Rewire `build_report()` to three-layer pipeline |
| `src/hft_platform/reports/distributor.py` | MODIFY | `Distributor.send()` accepts `ComposedReport` |
| `src/hft_platform/bot/handlers.py` | MODIFY | Commands use new pipeline |
| `src/hft_platform/bot/scheduler.py` | MODIFY | Push uses `ComposedReport` |
| `Dockerfile` | MODIFY | Add `matplotlib` |
| `tests/unit/test_report_facts.py` | CREATE | Layer 1 tests |
| `tests/unit/test_report_reasoner.py` | CREATE | Layer 2 tests |
| `tests/unit/test_report_composer.py` | CREATE | Layer 3 tests |
| `tests/unit/test_report_heatmap.py` | CREATE | Heatmap tests |
| `tests/unit/test_report_integration.py` | CREATE | Full pipeline integration test |

Post-migration (after all tests pass):
| `src/hft_platform/reports/signals.py` | DELETE | Logic migrated to facts.py + reasoner.py |
| `src/hft_platform/reports/scenarios.py` | DELETE | Logic migrated to reasoner.py |
| `src/hft_platform/reports/renderer.py` | DELETE | Replaced by composer.py |

---

### Task 1: Add New Data Models to models.py

**Files:**
- Modify: `src/hft_platform/reports/models.py`
- Test: `tests/unit/test_report_models.py`

- [ ] **Step 1: Write test for new dataclasses**

Create `tests/unit/test_report_models.py`:

```python
"""Test new report data models for three-layer architecture."""
from __future__ import annotations

from hft_platform.reports.models import (
    BiasJudgment,
    ChipCluster,
    ChipFacts,
    ComposedReport,
    CrossDayFacts,
    DaySnapshot,
    EnrichedLevel,
    Evidence,
    FactReport,
    FlowFacts,
    MessagePart,
    NarrativeReport,
    ReasoningReport,
    SegmentFact,
    SessionData,
    VolatilityFacts,
)


def test_segment_fact_creation():
    sf = SegmentFact(
        name="opening",
        time_range="08:45-09:30",
        ud_ratio=1.15,
        net_flow=1200,
        volume=5000,
        volume_pct=0.23,
        large_buy_count=5,
        large_sell_count=3,
        high=205000000,
        low=204000000,
        dominant_side="bull",
    )
    assert sf.name == "opening"
    assert sf.dominant_side == "bull"


def test_chip_cluster_has_timestamps():
    cc = ChipCluster(
        price_center=204500000,
        price_range=(204000000, 205000000),
        buy_volume=120,
        sell_volume=80,
        trade_count=8,
        dominant_side="buy",
        first_ts="09:15:30",
        last_ts="10:30:45",
        time_range="09:15-10:30",
    )
    assert cc.first_ts == "09:15:30"
    assert cc.time_range == "09:15-10:30"


def test_chip_facts_aggregates():
    cf = ChipFacts(
        clusters=[],
        vap_peaks=[],
        buy_zone=None,
        sell_zone=None,
        total_buy_volume=500,
        total_sell_volume=300,
        net_ratio=0.625,
    )
    assert cf.net_ratio == 0.625
    assert cf.total_buy_volume == 500


def test_volatility_facts():
    vf = VolatilityFacts(
        atr_5m=50000,
        session_range=3000000,
        range_atr_ratio=0.85,
        atr_session=3500000,
    )
    assert vf.range_atr_ratio == 0.85


def test_evidence_and_bias_judgment():
    e = Evidence(source="flow.session_ud", fact_value="U/D=0.82", direction="bear", weight=0.20)
    bj = BiasJudgment(
        bias="bearish",
        confidence=0.72,
        evidences=[e],
        summary="偏空: 全場 U/D=0.82 空方主導",
    )
    assert bj.bias == "bearish"
    assert len(bj.evidences) == 1


def test_enriched_level_pivot():
    el = EnrichedLevel(
        price=205000000,
        side="pivot",
        strength=0.85,
        sources=["大單賣壓 120口", "成交量集中"],
        confluence_count=2,
    )
    assert el.side == "pivot"
    assert el.confluence_count == 2


def test_message_part_tier():
    mp_free = MessagePart(kind="text", content="Summary", min_tier="free")
    mp_paid = MessagePart(kind="text", content="Detail", min_tier="paid")
    mp_img = MessagePart(kind="image", content="", image=b"\x89PNG", caption="Heatmap", min_tier="paid")
    assert mp_free.min_tier == "free"
    assert mp_paid.min_tier == "paid"
    assert mp_img.image is not None


def test_composed_report():
    cr = ComposedReport(messages=[
        MessagePart(kind="text", content="hello", min_tier="free"),
    ])
    assert len(cr.messages) == 1


def test_narrative_report():
    nr = NarrativeReport(
        storyline=["Opening paragraph", "Midday paragraph"],
        turning_points=[("09:30", "多方突破失敗")],
        conclusion="空方尾盤接管",
    )
    assert len(nr.storyline) == 2
    assert nr.conclusion == "空方尾盤接管"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_report_models.py -v`
Expected: ImportError — new classes not yet defined

- [ ] **Step 3: Add new dataclasses to models.py**

Append after existing `ChannelConfig` class (after line 177) in `src/hft_platform/reports/models.py`:

```python
# ---------------------------------------------------------------------------
# Three-Layer Architecture Models (Layer 1: Facts)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SegmentFact:
    """Facts about a single time segment within a session."""

    name: str  # "pre_open" / "opening" / "midday" / "closing"
    time_range: str  # "08:45-09:30"
    ud_ratio: float
    net_flow: int
    volume: int
    volume_pct: float  # fraction of session total (0.0-1.0)
    large_buy_count: int
    large_sell_count: int
    high: int  # scaled int
    low: int  # scaled int
    dominant_side: str  # "bull" / "bear" / "neutral"


@dataclass(slots=True)
class ChipCluster:
    """A cluster of large trades near a price level."""

    price_center: int
    price_range: tuple[int, int]
    buy_volume: int
    sell_volume: int
    trade_count: int
    dominant_side: str  # "buy" / "sell" / "mixed"
    first_ts: str
    last_ts: str
    time_range: str  # "09:15-10:30"


@dataclass(slots=True)
class ChipFacts:
    """Aggregated chip structure from large trades + volume-at-price."""

    clusters: list[ChipCluster]
    vap_peaks: list[PriceLevel]
    buy_zone: tuple[int, int] | None
    sell_zone: tuple[int, int] | None
    total_buy_volume: int
    total_sell_volume: int
    net_ratio: float  # buy / (buy + sell), 0.5 = balanced


@dataclass(slots=True)
class FlowFacts:
    """Session-level order flow facts."""

    session_ud: float
    session_net_flow: int
    strongest_buy_bar: FlowBar
    strongest_sell_bar: FlowBar
    sustained_runs: list[tuple[str, int, str]]  # [(side, bar_count, time_range)]
    volume_spikes: list[tuple[FlowBar, float]]  # [(bar, ratio_vs_avg)]
    eod_ud: float
    eod_drift: float  # eod_ud - session_ud


@dataclass(slots=True)
class StructureFacts:
    """Price structure facts (patterns, round numbers, extremes)."""

    double_bottoms: list[PriceLevel]
    double_tops: list[PriceLevel]
    failed_breakouts: list[PriceLevel]
    round_numbers: list[PriceLevel]
    session_high: PriceLevel
    session_low: PriceLevel


@dataclass(slots=True)
class VolatilityFacts:
    """Volatility metrics derived from 5m bars."""

    atr_5m: int  # Average True Range of 5m bars (scaled int)
    session_range: int  # high - low (scaled int)
    range_atr_ratio: float  # session_range / atr_session
    atr_session: int  # session-level ATR estimate (scaled int)


@dataclass(slots=True)
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


@dataclass(slots=True)
class CrossDayFacts:
    """Cross-day comparison facts."""

    prev_days: list[DaySnapshot]
    volume_change_pct: float  # vs previous day
    price_position: str  # "above_prev_high" / "below_prev_low" / "inside_range"
    trend_direction: str  # "up" / "down" / "sideways"
    flow_reversal: bool


@dataclass(slots=True)
class FactReport:
    """Complete Layer 1 output: all extracted facts."""

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


@dataclass(slots=True)
class Evidence:
    """A single piece of evidence for bias determination."""

    source: str  # "flow.session_ud", "chips.net_ratio", etc.
    fact_value: str  # human-readable
    direction: str  # "bull" / "bear" / "neutral"
    weight: float  # 0.0-1.0


@dataclass(slots=True)
class BiasJudgment:
    """Overall market bias with evidence chain."""

    bias: str  # "bullish" / "bearish" / "neutral"
    confidence: float  # 0.0-1.0
    evidences: list[Evidence]
    summary: str


@dataclass(slots=True)
class EnrichedLevel:
    """Support/resistance level with confluence information."""

    price: int
    side: str  # "support" / "resistance" / "pivot"
    strength: float  # 0.0-1.0
    sources: list[str]
    confluence_count: int


@dataclass(slots=True)
class NarrativeReport:
    """Time-segment narrative output."""

    storyline: list[str]  # chronological paragraphs
    turning_points: list[tuple[str, str]]  # [(time, event)]
    conclusion: str


@dataclass(slots=True)
class ReasoningReport:
    """Complete Layer 2 output."""

    bias: BiasJudgment
    levels: list[EnrichedLevel]
    scenarios: list[Scenario]  # reuses existing Scenario dataclass
    narrative: NarrativeReport


# ---------------------------------------------------------------------------
# Three-Layer Architecture Models (Layer 3: Composition)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MessagePart:
    """A single part of the composed report."""

    kind: str  # "text" or "image"
    content: str  # text content (for kind="text"), empty for image
    image: bytes | None = None  # PNG bytes (for kind="image")
    caption: str = ""  # image caption (for kind="image")
    min_tier: str = "free"  # "free" or "paid"


@dataclass(slots=True)
class ComposedReport:
    """Complete Layer 3 output: ordered list of message parts."""

    messages: list[MessagePart]
```

Also update `__all__` at the top of models.py to export all new classes.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_report_models.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/models.py tests/unit/test_report_models.py
git commit -m "feat(reports): add 15 dataclasses for three-layer analysis architecture"
```

---

### Task 2: Layer 1 — FactExtractor (facts.py)

**Files:**
- Create: `src/hft_platform/reports/facts.py`
- Test: `tests/unit/test_report_facts.py`

This is the largest task. It migrates logic from `signals.py` (rules IF-01..06, SR-02..06) and adds new extractors (TimeSegment, Chip aggregates, Volatility, CrossDay).

- [ ] **Step 1: Write tests for all 6 extractors**

Create `tests/unit/test_report_facts.py`:

```python
"""Test Layer 1 fact extractors."""
from __future__ import annotations

import math

from hft_platform.reports.facts import (
    extract_all,
    extract_chip_facts,
    extract_cross_day_facts,
    extract_flow_facts,
    extract_structure_facts,
    extract_time_segments,
    extract_volatility_facts,
)
from hft_platform.reports.models import (
    Bar5m,
    ChipCluster,
    DaySnapshot,
    FlowBar,
    LargeTrade,
    PriceLevel,
    SessionData,
)


def _make_session_data(
    *,
    bars_5m: list[Bar5m] | None = None,
    flow_5m: list[FlowBar] | None = None,
    large_trades: list[LargeTrade] | None = None,
    open_p: int = 205000000,
    high: int = 206000000,
    low: int = 204000000,
    close: int = 204500000,
    volume: int = 10000,
    tick_count: int = 50000,
) -> SessionData:
    return SessionData(
        session="day",
        symbol="TXFD6",
        date="2026-03-27",
        open=open_p,
        high=high,
        low=low,
        close=close,
        volume=volume,
        tick_count=tick_count,
        bars_5m=bars_5m or [],
        flow_5m=flow_5m or [],
        large_trades=large_trades or [],
        spread_dist={},
        depth_imbalance=[],
    )


def _make_flow_bar(ts: str, uptick: int, downtick: int, total: int | None = None) -> FlowBar:
    t = total if total is not None else uptick + downtick
    flat = t - uptick - downtick
    ud = uptick / downtick if downtick > 0 else 99.0
    return FlowBar(
        ts=ts, ticks=100, total_vol=t,
        uptick_vol=uptick, downtick_vol=downtick,
        flat_vol=max(0, flat), ud_ratio=ud, net_flow=uptick - downtick,
    )


def _make_bar5m(ts: str, o: int, h: int, l: int, c: int, vol: int = 100) -> Bar5m:
    return Bar5m(ts=ts, open=o, high=h, low=l, close=c, volume=vol, ticks=50)


# ---- TimeSegmentFacts ----

class TestTimeSegments:
    def test_day_session_has_4_segments(self):
        bars = [
            _make_flow_bar("2026-03-27 07:30:00", 50, 40),
            _make_flow_bar("2026-03-27 09:00:00", 60, 30),
            _make_flow_bar("2026-03-27 10:00:00", 45, 55),
            _make_flow_bar("2026-03-27 12:30:00", 30, 70),
        ]
        sd = _make_session_data(flow_5m=bars, large_trades=[])
        segments = extract_time_segments(sd)
        names = [s.name for s in segments]
        assert "pre_open" in names
        assert "opening" in names
        assert "midday" in names
        assert "closing" in names

    def test_volume_pcts_sum_to_one(self):
        bars = [
            _make_flow_bar("2026-03-27 09:00:00", 100, 80),
            _make_flow_bar("2026-03-27 10:00:00", 90, 90),
            _make_flow_bar("2026-03-27 12:30:00", 70, 110),
        ]
        sd = _make_session_data(flow_5m=bars)
        segments = extract_time_segments(sd)
        total_pct = sum(s.volume_pct for s in segments)
        assert abs(total_pct - 1.0) < 0.01

    def test_dominant_side_classification(self):
        bars = [_make_flow_bar("2026-03-27 09:00:00", 80, 40)]
        sd = _make_session_data(flow_5m=bars)
        segments = extract_time_segments(sd)
        opening = next(s for s in segments if s.name == "opening")
        assert opening.dominant_side == "bull"


# ---- ChipFacts ----

class TestChipFacts:
    def test_cluster_with_timestamps(self):
        trades = [
            LargeTrade(ts="2026-03-27 09:15:00", price=204500000, volume=50, direction="buy"),
            LargeTrade(ts="2026-03-27 09:20:00", price=204550000, volume=40, direction="sell"),
            LargeTrade(ts="2026-03-27 09:30:00", price=204480000, volume=60, direction="buy"),
        ]
        sd = _make_session_data(large_trades=trades)
        chips = extract_chip_facts(sd)
        assert len(chips.clusters) >= 1
        cluster = chips.clusters[0]
        assert cluster.first_ts == "2026-03-27 09:15:00"
        assert cluster.trade_count == 3

    def test_aggregates_computed(self):
        trades = [
            LargeTrade(ts="09:15", price=204500000, volume=100, direction="buy"),
            LargeTrade(ts="09:20", price=205500000, volume=60, direction="sell"),
        ]
        sd = _make_session_data(large_trades=trades)
        chips = extract_chip_facts(sd)
        assert chips.total_buy_volume == 100
        assert chips.total_sell_volume == 60
        assert abs(chips.net_ratio - 100 / 160) < 0.01

    def test_empty_trades(self):
        sd = _make_session_data(large_trades=[])
        chips = extract_chip_facts(sd)
        assert chips.total_buy_volume == 0
        assert chips.net_ratio == 0.5


# ---- FlowFacts ----

class TestFlowFacts:
    def test_session_ud_calculation(self):
        bars = [
            _make_flow_bar("09:00", 80, 100),
            _make_flow_bar("09:05", 70, 90),
        ]
        sd = _make_session_data(flow_5m=bars)
        ff = extract_flow_facts(sd)
        expected_ud = (80 + 70) / (100 + 90)
        assert abs(ff.session_ud - expected_ud) < 0.001

    def test_sustained_run_detection(self):
        # 5 consecutive bearish bars (ud < 0.7)
        bars = [_make_flow_bar(f"09:{i*5:02d}", 30, 70) for i in range(5)]
        sd = _make_session_data(flow_5m=bars)
        ff = extract_flow_facts(sd)
        bear_runs = [r for r in ff.sustained_runs if r[0] == "bear"]
        assert len(bear_runs) >= 1
        assert bear_runs[0][1] >= 5

    def test_eod_drift(self):
        # First 10 bars neutral, last 6 bars bearish
        neutral = [_make_flow_bar(f"09:{i*5:02d}", 50, 50) for i in range(10)]
        bearish = [_make_flow_bar(f"12:{i*5:02d}", 30, 70) for i in range(6)]
        sd = _make_session_data(flow_5m=neutral + bearish)
        ff = extract_flow_facts(sd)
        assert ff.eod_drift < -0.1  # EOD more bearish than session


# ---- StructureFacts ----

class TestStructureFacts:
    def test_session_extremes(self):
        sd = _make_session_data(high=206000000, low=204000000)
        sf = extract_structure_facts(sd)
        assert sf.session_high.price == 206000000
        assert sf.session_low.price == 204000000

    def test_round_numbers_in_range(self):
        sd = _make_session_data(high=206000000, low=204000000)
        sf = extract_structure_facts(sd)
        prices = [rn.price for rn in sf.round_numbers]
        assert 205000000 in prices  # 20500 is a round number (x100)


# ---- VolatilityFacts ----

class TestVolatilityFacts:
    def test_atr_from_bars(self):
        bars = [
            _make_bar5m("09:00", 204500000, 205000000, 204000000, 204800000),
            _make_bar5m("09:05", 204800000, 205200000, 204300000, 204600000),
            _make_bar5m("09:10", 204600000, 205100000, 204100000, 204900000),
        ]
        sd = _make_session_data(bars_5m=bars, high=205200000, low=204000000)
        vf = extract_volatility_facts(sd)
        assert vf.atr_5m > 0
        assert vf.session_range == 205200000 - 204000000
        assert vf.atr_session > vf.atr_5m

    def test_empty_bars_returns_zero(self):
        sd = _make_session_data(bars_5m=[])
        vf = extract_volatility_facts(sd)
        assert vf.atr_5m == 0
        assert vf.range_atr_ratio == 0.0


# ---- CrossDayFacts ----

class TestCrossDayFacts:
    def test_volume_change(self):
        prev = [DaySnapshot(
            date="2026-03-26", session="day",
            open=205000000, high=206000000, low=204000000, close=205500000,
            volume=12000, ud_ratio=1.1, net_flow=500,
        )]
        sd = _make_session_data(volume=10000)
        cdf = extract_cross_day_facts(sd, prev)
        expected_pct = (10000 - 12000) / 12000 * 100
        assert abs(cdf.volume_change_pct - expected_pct) < 0.1

    def test_price_position_below_prev_low(self):
        prev = [DaySnapshot(
            date="2026-03-26", session="day",
            open=206000000, high=207000000, low=205000000, close=206500000,
            volume=12000, ud_ratio=1.1, net_flow=500,
        )]
        sd = _make_session_data(close=204500000)
        cdf = extract_cross_day_facts(sd, prev)
        assert cdf.price_position == "below_prev_low"

    def test_flow_reversal_detection(self):
        prev = [DaySnapshot(
            date="2026-03-26", session="day",
            open=205000000, high=206000000, low=204000000, close=205500000,
            volume=12000, ud_ratio=1.2, net_flow=1000,
        )]
        bars = [_make_flow_bar("09:00", 30, 70)]  # bearish today
        sd = _make_session_data(flow_5m=bars)
        cdf = extract_cross_day_facts(sd, prev)
        assert cdf.flow_reversal is True

    def test_empty_prev_days(self):
        sd = _make_session_data()
        cdf = extract_cross_day_facts(sd, [])
        assert cdf.volume_change_pct == 0.0
        assert cdf.trend_direction == "sideways"


# ---- extract_all ----

class TestExtractAll:
    def test_returns_fact_report(self):
        bars = [_make_bar5m("09:00", 204500000, 205000000, 204000000, 204800000)]
        flow = [_make_flow_bar("09:00", 60, 40)]
        sd = _make_session_data(bars_5m=bars, flow_5m=flow)
        fr = extract_all(sd, prev_days=[])
        assert fr.session_data is sd
        assert len(fr.segments) >= 1
        assert fr.flow.session_ud > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_report_facts.py -v`
Expected: ImportError — `facts.py` not yet created

- [ ] **Step 3: Implement facts.py**

Create `src/hft_platform/reports/facts.py`. This file migrates logic from:
- `rules/informed_flow.py` → FlowFacts extraction (score_session_ud, score_sustained_pressure, score_volume_spike, score_end_of_session_drift, find_large_trade_clusters)
- `rules/support_resistance.py` → StructureFacts extraction (find_double_bottoms_tops, find_failed_breakouts, find_round_numbers, find_session_extremes, find_volume_at_price)
- New: TimeSegmentFacts, ChipFacts aggregates, VolatilityFacts, CrossDayFacts

Key implementation notes:
- Reuse existing rule functions from `rules/informed_flow.py` and `rules/support_resistance.py` internally — call them and wrap results into fact dataclasses. Do NOT duplicate the actual detection logic.
- `extract_time_segments()`: Classify each FlowBar by timestamp into segments using the boundaries from spec §3.1 (day: pre_open 07:00-08:45, opening 08:45-09:30, midday 09:30-12:00, closing 12:00-13:45; night: opening 15:00-15:45, midday 15:45-03:00, closing 03:00-05:00). Parse FlowBar.ts to determine segment.
- `extract_chip_facts()`: Call `find_large_trade_clusters()` with tolerance=50000, then augment clusters with timestamps from the original trades. Compute `total_buy_volume`, `total_sell_volume`, `net_ratio` from all large trades. Call `find_volume_at_price()` for vap_peaks. Compute buy_zone/sell_zone from clusters.
- `extract_flow_facts()`: Compute session_ud, net_flow, strongest bars, sustained runs, volume spikes, eod_ud/drift from FlowBar list. Logic from IF-01, IF-02, IF-05, IF-06 fact extraction (not scoring).
- `extract_structure_facts()`: Call existing `find_double_bottoms_tops()`, `find_failed_breakouts()`, `find_round_numbers()`, `find_session_extremes()`.
- `extract_volatility_facts()`: Compute Wilder ATR from 5m bars. `atr_session = atr_5m * sqrt(bar_count)`. `range_atr_ratio = session_range / atr_session`.
- `extract_cross_day_facts()`: Compare current SessionData with `prev_days: list[DaySnapshot]`. Compute volume_change_pct, price_position, trend_direction (2+ days same direction), flow_reversal.
- `extract_all()`: Calls all 6 extractors, returns `FactReport`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_facts.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/facts.py tests/unit/test_report_facts.py
git commit -m "feat(reports): add Layer 1 FactExtractor with 6 extractors"
```

---

### Task 3: Collector — Add Q7 Cross-Day Query

**Files:**
- Modify: `src/hft_platform/reports/collector.py`
- Test: `tests/unit/test_collector_crossday.py`

- [ ] **Step 1: Write test for collect_cross_day()**

Create `tests/unit/test_collector_crossday.py`:

```python
"""Test Q7 cross-day query in DataCollector."""
from __future__ import annotations

from unittest.mock import patch

from hft_platform.reports.collector import DataCollector
from hft_platform.reports.models import DaySnapshot


def test_collect_cross_day_returns_snapshots():
    """Q7 should return up to 3 DaySnapshot objects."""
    fake_rows = [
        ("2026-03-26", 205000000, 206000000, 204000000, 205500000, 12000, 6500, 5500),
        ("2026-03-25", 204000000, 205500000, 203500000, 204800000, 11000, 5800, 5200),
    ]

    collector = DataCollector(ch_host="localhost")
    with patch.object(collector, "_execute", return_value=fake_rows):
        snapshots = collector.collect_cross_day("TXFD6", "day", "2026-03-27")

    assert len(snapshots) == 2
    assert isinstance(snapshots[0], DaySnapshot)
    assert snapshots[0].date == "2026-03-26"
    assert snapshots[0].volume == 12000
    assert snapshots[0].ud_ratio == 6500 / 5500


def test_collect_cross_day_empty():
    """Q7 with no data returns empty list."""
    collector = DataCollector(ch_host="localhost")
    with patch.object(collector, "_execute", return_value=[]):
        snapshots = collector.collect_cross_day("TXFD6", "day", "2026-03-27")

    assert snapshots == []


def test_collect_cross_day_handles_zero_downtick():
    """ud_ratio should be inf-safe when downtick_vol is 0."""
    fake_rows = [
        ("2026-03-26", 205000000, 206000000, 204000000, 205500000, 12000, 6500, 0),
    ]
    collector = DataCollector(ch_host="localhost")
    with patch.object(collector, "_execute", return_value=fake_rows):
        snapshots = collector.collect_cross_day("TXFD6", "day", "2026-03-27")

    assert snapshots[0].ud_ratio > 10  # effectively inf, clamped or large
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_collector_crossday.py -v`
Expected: AttributeError — `collect_cross_day` not defined

- [ ] **Step 3: Implement collect_cross_day()**

Add method to `DataCollector` class in `src/hft_platform/reports/collector.py` (after `_query_depth_imbalance`):

```python
def collect_cross_day(
    self,
    symbol: str,
    session: str,
    date: str,
    lookback_days: int = 3,
) -> list[DaySnapshot]:
    """Q7: Fetch OHLCV + flow summary for previous N trading days.

    Returns list of DaySnapshot sorted by date descending (most recent first).
    """
    # Build date list: previous N calendar days, filtering weekends
    from datetime import datetime, timedelta

    base = datetime.strptime(date, "%Y-%m-%d")
    dates: list[str] = []
    for offset in range(1, lookback_days * 2 + 1):
        d = base - timedelta(days=offset)
        if d.weekday() < 5:  # skip Saturday (5) and Sunday (6)
            dates.append(d.strftime("%Y-%m-%d"))
        if len(dates) >= lookback_days:
            break

    if not dates:
        return []

    date_list = ", ".join(f"'{d}'" for d in dates)
    time_filter_fn = _day_filter if session == "day" else _night_filter

    # Build UNION ALL query for each date
    subqueries = []
    for d in dates:
        tf = time_filter_fn(d)
        subqueries.append(f"""
            SELECT
                '{d}' AS date,
                argMin(price_scaled, exch_ts) AS open_p,
                max(price_scaled) AS high_p,
                min(price_scaled) AS low_p,
                argMax(price_scaled, exch_ts) AS close_p,
                sum(volume) AS total_vol,
                sumIf(volume, price_scaled > lagInFrame(price_scaled) OVER (ORDER BY exch_ts)) AS up_vol,
                sumIf(volume, price_scaled < lagInFrame(price_scaled) OVER (ORDER BY exch_ts)) AS dn_vol
            FROM hft.market_data
            WHERE symbol = '{symbol}' AND {tf}
            HAVING total_vol > 0
        """)

    query = " UNION ALL ".join(subqueries) + " ORDER BY date DESC"

    try:
        rows = self._execute(f"{_SETTINGS}\n{query}")
    except Exception:
        _log.warning("q7_cross_day_failed", symbol=symbol, session=session)
        return []

    snapshots: list[DaySnapshot] = []
    for row in rows:
        d, op, hp, lp, cp, vol, up, dn = row
        ud = up / dn if dn > 0 else 99.0
        snapshots.append(DaySnapshot(
            date=d,
            session=session,
            open=_ch_to_platform(op),
            high=_ch_to_platform(hp),
            low=_ch_to_platform(lp),
            close=_ch_to_platform(cp),
            volume=vol,
            ud_ratio=ud,
            net_flow=up - dn,
        ))
    return snapshots
```

Also add `DaySnapshot` to the imports from models.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_collector_crossday.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/collector.py tests/unit/test_collector_crossday.py
git commit -m "feat(reports): add Q7 cross-day query to DataCollector"
```

---

### Task 4: Layer 2 — Reasoner (reasoner.py)

**Files:**
- Create: `src/hft_platform/reports/reasoner.py`
- Test: `tests/unit/test_report_reasoner.py`

- [ ] **Step 1: Write tests for all 4 reasoners**

Create `tests/unit/test_report_reasoner.py`:

```python
"""Test Layer 2 reasoners."""
from __future__ import annotations

from hft_platform.reports.models import (
    BiasJudgment,
    ChipCluster,
    ChipFacts,
    CrossDayFacts,
    DaySnapshot,
    EnrichedLevel,
    FactReport,
    FlowBar,
    FlowFacts,
    NarrativeReport,
    PriceLevel,
    Scenario,
    SegmentFact,
    SessionData,
    StructureFacts,
    VolatilityFacts,
)
from hft_platform.reports.reasoner import (
    BiasReasoner,
    LevelReasoner,
    NarrativeReasoner,
    ScenarioReasoner,
    reason_all,
)


def _make_fact_report(
    *,
    session_ud: float = 0.82,
    eod_drift: float = -0.25,
    net_ratio: float = 0.35,
    closing_side: str = "bear",
    trend: str = "down",
    flow_reversal: bool = True,
    close: int = 204500000,
    supports: list[PriceLevel] | None = None,
    resistances: list[PriceLevel] | None = None,
) -> FactReport:
    """Build a FactReport with controllable parameters for testing."""
    dummy_bar = FlowBar(
        ts="09:00", ticks=100, total_vol=200,
        uptick_vol=80, downtick_vol=120, flat_vol=0,
        ud_ratio=0.67, net_flow=-40,
    )
    segments = [
        SegmentFact("opening", "08:45-09:30", 1.1, 200, 3000, 0.3,
                     2, 1, 205000000, 204000000, "bull"),
        SegmentFact("midday", "09:30-12:00", 0.95, -50, 4000, 0.4,
                     1, 1, 205500000, 204500000, "neutral"),
        SegmentFact("closing", "12:00-13:45", 0.71, -300, 3000, 0.3,
                     0, 3, 205000000, 204000000, closing_side),
    ]
    sd = SessionData(
        session="day", symbol="TXFD6", date="2026-03-27",
        open=205000000, high=206000000, low=204000000, close=close,
        volume=10000, tick_count=50000,
        bars_5m=[], flow_5m=[], large_trades=[],
        spread_dist={}, depth_imbalance=[],
    )
    all_levels = (supports or []) + (resistances or [])
    return FactReport(
        session_data=sd,
        segments=segments,
        chips=ChipFacts(
            clusters=[], vap_peaks=[], buy_zone=None, sell_zone=None,
            total_buy_volume=200, total_sell_volume=400, net_ratio=net_ratio,
        ),
        flow=FlowFacts(
            session_ud=session_ud, session_net_flow=-2000,
            strongest_buy_bar=dummy_bar, strongest_sell_bar=dummy_bar,
            sustained_runs=[("bear", 5, "12:00-12:25")],
            volume_spikes=[], eod_ud=0.71, eod_drift=eod_drift,
        ),
        structure=StructureFacts(
            double_bottoms=[], double_tops=[],
            failed_breakouts=[], round_numbers=[],
            session_high=PriceLevel(price=206000000, strength=0.5, reason="日盤高點"),
            session_low=PriceLevel(price=204000000, strength=0.5, reason="日盤低點"),
        ),
        volatility=VolatilityFacts(
            atr_5m=300000, session_range=2000000,
            range_atr_ratio=0.85, atr_session=2350000,
        ),
        cross_day=CrossDayFacts(
            prev_days=[DaySnapshot(
                date="2026-03-26", session="day",
                open=206000000, high=207000000, low=205000000, close=206500000,
                volume=12000, ud_ratio=1.2, net_flow=1000,
            )],
            volume_change_pct=-16.7,
            price_position="below_prev_low",
            trend_direction=trend,
            flow_reversal=flow_reversal,
        ),
    )


# ---- BiasReasoner ----

class TestBiasReasoner:
    def test_bearish_with_concordant_evidence(self):
        fr = _make_fact_report(session_ud=0.82, eod_drift=-0.25, net_ratio=0.35, closing_side="bear")
        bj = BiasReasoner().judge(fr)
        assert bj.bias == "bearish"
        assert bj.confidence >= 0.6
        assert len(bj.evidences) >= 4

    def test_neutral_in_dead_zone(self):
        fr = _make_fact_report(session_ud=1.0, eod_drift=0.05, net_ratio=0.50,
                               closing_side="neutral", trend="sideways", flow_reversal=False)
        bj = BiasReasoner().judge(fr)
        assert bj.bias == "neutral"

    def test_bullish_when_all_bull(self):
        fr = _make_fact_report(session_ud=1.25, eod_drift=0.30, net_ratio=0.65,
                               closing_side="bull", trend="up", flow_reversal=False)
        bj = BiasReasoner().judge(fr)
        assert bj.bias == "bullish"
        assert bj.confidence >= 0.6

    def test_contradictions_noted(self):
        fr = _make_fact_report(session_ud=0.80, net_ratio=0.65)  # flow bear, chips bull
        bj = BiasReasoner().judge(fr)
        bear_ev = [e for e in bj.evidences if e.direction == "bear"]
        bull_ev = [e for e in bj.evidences if e.direction == "bull"]
        assert len(bear_ev) >= 1
        assert len(bull_ev) >= 1


# ---- LevelReasoner ----

class TestLevelReasoner:
    def test_buffer_zone_creates_pivot(self):
        """Level within ±5 pts of close should be 'pivot', not support/resistance."""
        close = 204500000
        level = PriceLevel(price=close + 30000, strength=0.8, reason="test")  # +3 pts
        fr = _make_fact_report(close=close)
        fr.structure.round_numbers = [level]
        levels = LevelReasoner().analyze(fr)
        near = [l for l in levels if abs(l.price - level.price) < 50000]
        assert len(near) >= 1
        assert near[0].side == "pivot"

    def test_confluence_merges_nearby_levels(self):
        """Two levels within ±5 pts should merge and increase confluence count."""
        close = 204500000
        level1 = PriceLevel(price=203000000, strength=0.6, reason="大單買")
        level2 = PriceLevel(price=203020000, strength=0.5, reason="成交量集中")
        fr = _make_fact_report(close=close)
        fr.structure.round_numbers = [level1]
        fr.chips.vap_peaks = [level2]
        levels = LevelReasoner().analyze(fr)
        merged = [l for l in levels if abs(l.price - 203000000) < 50000]
        assert len(merged) == 1
        assert merged[0].confluence_count >= 2

    def test_no_hard_limit_on_count(self):
        """Should return more than 3 levels when they have sufficient strength."""
        close = 205000000
        fr = _make_fact_report(close=close)
        fr.structure.round_numbers = [
            PriceLevel(price=204000000, strength=0.8, reason=f"S{i}")
            for i in range(5)
        ]
        levels = LevelReasoner().analyze(fr)
        supports = [l for l in levels if l.side == "support"]
        assert len(supports) >= 4  # not capped at 3


# ---- ScenarioReasoner ----

class TestScenarioReasoner:
    def test_break_below_fires_with_single_support(self):
        """SC-01 should fire with just S1 (not require ≥2 supports)."""
        fr = _make_fact_report()
        fr.structure.session_low = PriceLevel(price=204000000, strength=0.5, reason="日盤低點")
        bias = BiasJudgment(bias="bearish", confidence=0.7, evidences=[], summary="偏空")
        levels = [EnrichedLevel(price=204000000, side="support", strength=0.5,
                                sources=["日盤低點"], confluence_count=1)]
        scenarios = ScenarioReasoner().generate(fr, bias, levels)
        ids = [s.id for s in scenarios]
        assert "break_below" in ids

    def test_gap_fill_fires_on_gap(self):
        """gap_fill should fire when open gaps ≥0.3% from prev close."""
        fr = _make_fact_report()
        # prev close 206500000, today open 205000000 → gap down ~0.73%
        bias = BiasJudgment(bias="bearish", confidence=0.5, evidences=[], summary="偏空")
        scenarios = ScenarioReasoner().generate(fr, bias, [])
        ids = [s.id for s in scenarios]
        assert "gap_fill" in ids

    def test_range_bound_fires_on_low_ratio(self):
        """range_bound should fire when range_atr_ratio < 0.7 and bias neutral."""
        fr = _make_fact_report(session_ud=1.0, eod_drift=0.0, net_ratio=0.5,
                               closing_side="neutral", trend="sideways", flow_reversal=False)
        fr.volatility = VolatilityFacts(
            atr_5m=300000, session_range=1000000,
            range_atr_ratio=0.5, atr_session=2000000,
        )
        bias = BiasJudgment(bias="neutral", confidence=0.3, evidences=[], summary="中性")
        scenarios = ScenarioReasoner().generate(fr, bias, [])
        ids = [s.id for s in scenarios]
        assert "range_bound" in ids


# ---- NarrativeReasoner ----

class TestNarrativeReasoner:
    def test_storyline_has_one_paragraph_per_segment(self):
        fr = _make_fact_report()
        nr = NarrativeReasoner().narrate(fr)
        assert len(nr.storyline) == len(fr.segments)

    def test_turning_point_on_ud_flip(self):
        fr = _make_fact_report()
        # opening bull → closing bear should produce turning point
        nr = NarrativeReasoner().narrate(fr)
        assert len(nr.turning_points) >= 1

    def test_conclusion_not_empty(self):
        fr = _make_fact_report()
        nr = NarrativeReasoner().narrate(fr)
        assert len(nr.conclusion) > 0


# ---- reason_all ----

class TestReasonAll:
    def test_returns_reasoning_report(self):
        fr = _make_fact_report()
        rr = reason_all(fr)
        assert rr.bias.bias in ("bullish", "bearish", "neutral")
        assert isinstance(rr.narrative, NarrativeReport)
        assert isinstance(rr.levels, list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_report_reasoner.py -v`
Expected: ImportError — `reasoner.py` not yet created

- [ ] **Step 3: Implement reasoner.py**

Create `src/hft_platform/reports/reasoner.py` with 4 reasoner classes:

Key implementation notes:
- **BiasReasoner.judge(fr: FactReport) → BiasJudgment**: Iterate 8 evidence sources (spec §3.2 table). For each, check bull/bear/neutral condition, create `Evidence`. Sum weighted directions. Dead zone: if U/D in 0.85-1.15, its evidence weight is halved. Final bias: weighted_bull > weighted_bear + 0.10 → bullish, reverse → bearish, else neutral. Confidence = concordant_weight / total_weight.
- **LevelReasoner.analyze(fr: FactReport) → list[EnrichedLevel]**: Collect all PriceLevels from structure + chips.vap_peaks + chips.clusters (cluster centers as levels). Group within ±50,000 (5 pts) proximity. For each group: merge into single EnrichedLevel with confluence_count = len(group), strength = max, sources = all reasons. Classify side using buffer: > close + 50000 → resistance, < close - 50000 → support, else → pivot. Filter: keep confluence ≥ 2 OR (confluence=1 AND strength ≥ 0.7).
- **ScenarioReasoner.generate(fr, bias, levels) → list[Scenario]**: Check each trigger condition from spec §3.2 table. For break_below: need any support level, bias ≠ bullish. target = prev_day_low or S1 - atr_session. stop = S1 + 0.5 * atr_session. For gap_fill: |open - prev_close| / prev_close ≥ 0.003. etc. Build reasoning list from relevant facts.
- **NarrativeReasoner.narrate(fr: FactReport) → NarrativeReport**: For each SegmentFact, generate a paragraph using template: "{time_range}：{dominant description}，U/D={ud_ratio:.2f}，量能{volume_description}。{large_trade_note}". Detect turning points between adjacent segments (dominant_side flip). Conclusion from final segment + cross-day trend.
- **reason_all(fr: FactReport) → ReasoningReport**: Calls all 4 reasoners in order.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_reasoner.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/reasoner.py tests/unit/test_report_reasoner.py
git commit -m "feat(reports): add Layer 2 Reasoner with bias, levels, scenarios, narrative"
```

---

### Task 5: Flow Heatmap (heatmap.py)

**Files:**
- Create: `src/hft_platform/reports/heatmap.py`
- Test: `tests/unit/test_report_heatmap.py`

- [ ] **Step 1: Write test for heatmap generation**

Create `tests/unit/test_report_heatmap.py`:

```python
"""Test flow heatmap generation."""
from __future__ import annotations

from hft_platform.reports.heatmap import generate_heatmap
from hft_platform.reports.models import Bar5m, FlowBar, LargeTrade, SessionData


def _make_session_data() -> SessionData:
    bars = [
        Bar5m(ts=f"2026-03-27 09:{i*5:02d}:00", open=205000000,
              high=205200000, low=204800000, close=205100000,
              volume=100 + i * 10, ticks=50)
        for i in range(10)
    ]
    flow = [
        FlowBar(ts=f"2026-03-27 09:{i*5:02d}:00", ticks=50,
                total_vol=100 + i * 10, uptick_vol=50 + i * 5,
                downtick_vol=50 + i * 5 - (i * 2), flat_vol=i * 2,
                ud_ratio=1.0 + (i - 5) * 0.1, net_flow=(i - 5) * 10)
        for i in range(10)
    ]
    trades = [
        LargeTrade(ts="2026-03-27 09:15:00", price=205000000, volume=50, direction="buy"),
        LargeTrade(ts="2026-03-27 09:30:00", price=204800000, volume=40, direction="sell"),
    ]
    return SessionData(
        session="day", symbol="TXFD6", date="2026-03-27",
        open=205000000, high=205200000, low=204800000, close=205100000,
        volume=1500, tick_count=5000,
        bars_5m=bars, flow_5m=flow, large_trades=trades,
        spread_dist={}, depth_imbalance=[],
    )


def test_generate_heatmap_returns_png_bytes():
    sd = _make_session_data()
    result = generate_heatmap(sd)
    assert isinstance(result, bytes)
    assert result[:4] == b"\x89PNG"
    assert len(result) > 1000  # non-trivial image


def test_generate_heatmap_empty_data():
    sd = SessionData(
        session="day", symbol="TXFD6", date="2026-03-27",
        open=0, high=0, low=0, close=0, volume=0, tick_count=0,
        bars_5m=[], flow_5m=[], large_trades=[],
        spread_dist={}, depth_imbalance=[],
    )
    result = generate_heatmap(sd)
    assert result is None  # no data → no image
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_report_heatmap.py -v`
Expected: ImportError

- [ ] **Step 3: Implement heatmap.py**

Create `src/hft_platform/reports/heatmap.py`:

```python
"""Flow heatmap generator for Telegram reports.

Produces a PNG image showing U/D ratio heatmap, volume bars, large trade
markers, and price overlay for a trading session.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from hft_platform.reports.models import SessionData

_log = structlog.get_logger()

PLATFORM_SCALE = 10_000


def generate_heatmap(sd: SessionData) -> bytes | None:
    """Generate a flow heatmap PNG from session data.

    Returns PNG bytes, or None if insufficient data.
    """
    if not sd.flow_5m or not sd.bars_5m:
        return None

    try:
        import matplotlib
        matplotlib.use("Agg")  # non-interactive backend
        import matplotlib.pyplot as plt
        import matplotlib.colors as mcolors
        import numpy as np
    except ImportError:
        _log.warning("matplotlib_not_available")
        return None

    # Parse timestamps and data
    times = []
    ud_ratios = []
    volumes = []
    for bar in sd.flow_5m:
        try:
            t = datetime.strptime(bar.ts[:19], "%Y-%m-%d %H:%M:%S")
            times.append(t)
            ud_ratios.append(bar.ud_ratio)
            volumes.append(bar.total_vol)
        except (ValueError, IndexError):
            continue

    if len(times) < 2:
        return None

    # Parse price data from 5m bars
    bar_times = []
    bar_mids = []
    for bar in sd.bars_5m:
        try:
            t = datetime.strptime(bar.ts[:19], "%Y-%m-%d %H:%M:%S")
            bar_times.append(t)
            bar_mids.append((bar.high + bar.low) / 2 / PLATFORM_SCALE)
        except (ValueError, IndexError):
            continue

    fig, ax1 = plt.subplots(figsize=(14, 4))

    # Custom colormap: red (bear) → white (neutral) → green (bull)
    cmap = mcolors.LinearSegmentedColormap.from_list(
        "flow", ["#d32f2f", "#ffffff", "#388e3c"]
    )
    norm = mcolors.TwoSlopeNorm(vmin=0.5, vcenter=1.0, vmax=1.5)

    # Volume bars (gray, semi-transparent)
    max_vol = max(volumes) if volumes else 1
    vol_heights = [v / max_vol for v in volumes]
    ax1.bar(times, vol_heights, width=0.003, alpha=0.3, color="#9e9e9e", label="Volume")

    # Heatmap strip at bottom
    for i, (t, ratio) in enumerate(zip(times, ud_ratios)):
        color = cmap(norm(min(max(ratio, 0.5), 1.5)))
        ax1.axvspan(
            t, times[min(i + 1, len(times) - 1)],
            ymin=0, ymax=0.08, color=color, alpha=0.9,
        )

    # Large trade markers
    for trade in sd.large_trades:
        try:
            t = datetime.strptime(trade.ts[:19], "%Y-%m-%d %H:%M:%S")
            marker = "^" if trade.direction == "buy" else "v"
            color = "#388e3c" if trade.direction == "buy" else "#d32f2f"
            size = min(200, trade.volume * 3)
            ax1.scatter([t], [0.5], marker=marker, s=size, c=color,
                       alpha=0.7, zorder=5, edgecolors="black", linewidths=0.5)
        except (ValueError, IndexError):
            continue

    ax1.set_ylabel("Volume (norm)", fontsize=9)
    ax1.set_ylim(0, 1.2)
    ax1.tick_params(axis="x", rotation=45, labelsize=8)

    # Price line on right Y axis
    if bar_times:
        ax2 = ax1.twinx()
        ax2.plot(bar_times, bar_mids, color="#1565c0", linewidth=1.5,
                alpha=0.8, label="Mid Price")
        ax2.set_ylabel("Price", fontsize=9)
        ax2.tick_params(labelsize=8)

    title = f"{sd.symbol} {sd.session}盤 {sd.date} 流向熱力圖"
    ax1.set_title(title, fontsize=11, fontweight="bold")

    fig.tight_layout()

    # Write to BytesIO
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_heatmap.py -v`
Expected: All 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/heatmap.py tests/unit/test_report_heatmap.py
git commit -m "feat(reports): add flow heatmap generator with matplotlib"
```

---

### Task 6: Layer 3 — ReportComposer (composer.py)

**Files:**
- Create: `src/hft_platform/reports/composer.py`
- Test: `tests/unit/test_report_composer.py`

- [ ] **Step 1: Write tests for composer**

Create `tests/unit/test_report_composer.py`:

```python
"""Test Layer 3 ReportComposer."""
from __future__ import annotations

from hft_platform.reports.composer import ReportComposer
from hft_platform.reports.models import (
    BiasJudgment,
    ChipCluster,
    ChipFacts,
    ComposedReport,
    CrossDayFacts,
    DaySnapshot,
    EnrichedLevel,
    Evidence,
    FactReport,
    FlowBar,
    FlowFacts,
    MessagePart,
    NarrativeReport,
    PriceLevel,
    ReasoningReport,
    Scenario,
    SegmentFact,
    SessionData,
    StructureFacts,
    VolatilityFacts,
)

PLATFORM_SCALE = 10_000


def _make_full_reports() -> tuple[FactReport, ReasoningReport]:
    """Build minimal but complete FactReport + ReasoningReport for testing."""
    dummy_bar = FlowBar(
        ts="09:00", ticks=100, total_vol=200,
        uptick_vol=80, downtick_vol=120, flat_vol=0,
        ud_ratio=0.67, net_flow=-40,
    )
    sd = SessionData(
        session="day", symbol="TXFD6", date="2026-03-27",
        open=205000000, high=206000000, low=204000000, close=204500000,
        volume=10000, tick_count=50000,
        bars_5m=[], flow_5m=[dummy_bar], large_trades=[],
        spread_dist={2: 5000, 3: 3000}, depth_imbalance=[],
    )
    fr = FactReport(
        session_data=sd,
        segments=[
            SegmentFact("opening", "08:45-09:30", 1.1, 200, 3000, 0.3,
                         2, 1, 205000000, 204000000, "bull"),
            SegmentFact("closing", "12:00-13:45", 0.71, -300, 3000, 0.3,
                         0, 3, 205000000, 204000000, "bear"),
        ],
        chips=ChipFacts(
            clusters=[ChipCluster(
                price_center=204500000, price_range=(204000000, 205000000),
                buy_volume=100, sell_volume=200, trade_count=5,
                dominant_side="sell", first_ts="10:00", last_ts="11:30",
                time_range="10:00-11:30",
            )],
            vap_peaks=[], buy_zone=None,
            sell_zone=(204000000, 205000000),
            total_buy_volume=200, total_sell_volume=400, net_ratio=0.333,
        ),
        flow=FlowFacts(
            session_ud=0.82, session_net_flow=-2000,
            strongest_buy_bar=dummy_bar, strongest_sell_bar=dummy_bar,
            sustained_runs=[("bear", 5, "12:00-12:25")],
            volume_spikes=[], eod_ud=0.71, eod_drift=-0.11,
        ),
        structure=StructureFacts(
            double_bottoms=[], double_tops=[], failed_breakouts=[],
            round_numbers=[],
            session_high=PriceLevel(price=206000000, strength=0.5, reason="日盤高點"),
            session_low=PriceLevel(price=204000000, strength=0.5, reason="日盤低點"),
        ),
        volatility=VolatilityFacts(
            atr_5m=300000, session_range=2000000,
            range_atr_ratio=0.85, atr_session=2350000,
        ),
        cross_day=CrossDayFacts(
            prev_days=[DaySnapshot(
                "2026-03-26", "day", 206000000, 207000000, 205000000,
                206500000, 12000, 1.2, 1000,
            )],
            volume_change_pct=-16.7,
            price_position="below_prev_low",
            trend_direction="down",
            flow_reversal=True,
        ),
    )
    rr = ReasoningReport(
        bias=BiasJudgment(
            bias="bearish", confidence=0.72,
            evidences=[Evidence("flow.session_ud", "U/D=0.82", "bear", 0.20)],
            summary="偏空: U/D=0.82",
        ),
        levels=[
            EnrichedLevel(204000000, "support", 0.8, ["日盤低點", "大單買"], 2),
            EnrichedLevel(206000000, "resistance", 0.7, ["日盤高點"], 1),
        ],
        scenarios=[Scenario(
            id="break_below", label="破底加速", probability="中",
            condition="若跌破 S1 (20,400)",
            target=203500000, description="偏空 + 尾盤賣壓",
        )],
        narrative=NarrativeReport(
            storyline=["開盤多方嘗試", "尾盤空方接管"],
            turning_points=[("12:00", "空方發動")],
            conclusion="空方尾盤接管，籌碼偏空",
        ),
    )
    return fr, rr


class TestReportComposer:
    def test_compose_returns_composed_report(self):
        fr, rr = _make_full_reports()
        cr = ReportComposer().compose(fr, rr)
        assert isinstance(cr, ComposedReport)
        assert len(cr.messages) >= 7  # at least 7 text + 1 disclaimer

    def test_summary_is_free_tier(self):
        fr, rr = _make_full_reports()
        cr = ReportComposer().compose(fr, rr)
        assert cr.messages[0].min_tier == "free"
        assert cr.messages[0].kind == "text"
        assert "TXFD6" in cr.messages[0].content

    def test_disclaimer_is_free_tier(self):
        fr, rr = _make_full_reports()
        cr = ReportComposer().compose(fr, rr)
        disclaimer = cr.messages[-1]
        assert disclaimer.min_tier == "free"
        assert "免責" in disclaimer.content or "風險" in disclaimer.content

    def test_paid_messages_present(self):
        fr, rr = _make_full_reports()
        cr = ReportComposer().compose(fr, rr)
        paid = [m for m in cr.messages if m.min_tier == "paid"]
        assert len(paid) >= 5  # narrative, flow, chips, levels, scenarios

    def test_no_message_exceeds_telegram_limit(self):
        fr, rr = _make_full_reports()
        cr = ReportComposer().compose(fr, rr)
        for msg in cr.messages:
            if msg.kind == "text":
                assert len(msg.content) <= 4096

    def test_summary_contains_bias(self):
        fr, rr = _make_full_reports()
        cr = ReportComposer().compose(fr, rr)
        summary = cr.messages[0].content
        assert "偏空" in summary or "bearish" in summary

    def test_summary_contains_cross_day(self):
        fr, rr = _make_full_reports()
        cr = ReportComposer().compose(fr, rr)
        summary = cr.messages[0].content
        assert "前日" in summary or "量縮" in summary or "跌破" in summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_report_composer.py -v`
Expected: ImportError

- [ ] **Step 3: Implement composer.py**

Create `src/hft_platform/reports/composer.py`:

Key implementation notes:
- `ReportComposer.compose(fr: FactReport, rr: ReasoningReport) → ComposedReport`
- Reuse helper functions `_p()` and `_pct()` from existing `renderer.py` (copy them into composer.py as private helpers, since renderer.py will be deleted).
- Build 8 text messages + optional heatmap image:
  1. `_compose_summary(fr, rr)` → MessagePart(kind="text", min_tier="free"): OHLCV line, bias with evidence tree, cross-day comparison
  2. `_compose_narrative(rr)` → MessagePart(kind="text", min_tier="paid"): storyline paragraphs + turning points + conclusion
  3. `_compose_flow(fr)` → MessagePart(kind="text", min_tier="paid"): session/segment U/D table, sustained runs, spikes, EOD drift
  4. `_compose_chips(fr)` → MessagePart(kind="text", min_tier="paid"): clusters with time ranges, buy/sell zones, net ratio
  5. `_compose_levels(rr)` → MessagePart(kind="text", min_tier="paid"): supports, resistances, pivots with sources
  6. `_compose_scenarios(rr)` → MessagePart(kind="text", min_tier="paid"): each scenario with trigger, target, stop, reasoning
  7. `_compose_heatmap(fr)` → MessagePart(kind="image", min_tier="paid") or skip if None: calls `generate_heatmap(fr.session_data)`
  8. `_compose_disclaimer()` → MessagePart(kind="text", min_tier="free")
- For each text message, if content > 4096 chars, split into continuation parts.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_composer.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/composer.py tests/unit/test_report_composer.py
git commit -m "feat(reports): add Layer 3 ReportComposer with tier-aware MessageParts"
```

---

### Task 7: Rewire Pipeline + Distributor

**Files:**
- Modify: `src/hft_platform/reports/pipeline.py`
- Modify: `src/hft_platform/reports/distributor.py`
- Test: `tests/unit/test_report_pipeline_build.py` (update existing)

- [ ] **Step 1: Write test for new build_report()**

Update `tests/unit/test_report_pipeline_build.py` (or create `tests/unit/test_report_pipeline_v2.py` if existing tests should stay):

```python
"""Test rewired build_report() returning ComposedReport."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.reports.models import ComposedReport, MessagePart, SessionData


def _make_session_data(**kwargs) -> SessionData:
    defaults = dict(
        session="day", symbol="TXFD6", date="2026-03-27",
        open=205000000, high=206000000, low=204000000, close=204500000,
        volume=10000, tick_count=50000,
        bars_5m=[], flow_5m=[], large_trades=[],
        spread_dist={}, depth_imbalance=[],
    )
    defaults.update(kwargs)
    return SessionData(**defaults)


@patch("hft_platform.reports.pipeline.DataCollector")
@patch("hft_platform.reports.pipeline.extract_all")
@patch("hft_platform.reports.pipeline.reason_all")
@patch("hft_platform.reports.pipeline.ReportComposer")
def test_build_report_returns_composed_report(mock_composer, mock_reason, mock_extract, mock_collector):
    from hft_platform.reports.pipeline import build_report

    sd = _make_session_data(tick_count=100)
    mock_collector.return_value.collect.return_value = sd
    mock_collector.return_value.collect_cross_day.return_value = []

    mock_extract.return_value = MagicMock()
    mock_reason.return_value = MagicMock()
    mock_composer.return_value.compose.return_value = ComposedReport(
        messages=[MessagePart(kind="text", content="test", min_tier="free")]
    )

    result = build_report("day", "2026-03-27")
    assert isinstance(result, ComposedReport)
    assert len(result.messages) == 1


@patch("hft_platform.reports.pipeline.DataCollector")
def test_build_report_returns_none_on_no_data(mock_collector):
    from hft_platform.reports.pipeline import build_report

    sd = _make_session_data(tick_count=0)
    mock_collector.return_value.collect.return_value = sd

    result = build_report("day", "2026-03-27")
    assert result is None
```

- [ ] **Step 2: Write test for updated Distributor.send()**

Add to the same test file or create `tests/unit/test_distributor_v2.py`:

```python
"""Test updated Distributor with ComposedReport."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from hft_platform.reports.distributor import Distributor
from hft_platform.reports.models import ChannelConfig, ComposedReport, MessagePart


def test_distributor_filters_by_tier():
    sender = MagicMock()
    sender.send = AsyncMock(return_value=True)
    sender.send_photo = AsyncMock(return_value=True)

    free_channel = ChannelConfig(name="free", chat_id="111", tier="free", enabled=True)
    paid_channel = ChannelConfig(name="owner", chat_id="222", tier="paid", enabled=True)

    cr = ComposedReport(messages=[
        MessagePart(kind="text", content="Summary", min_tier="free"),
        MessagePart(kind="text", content="Detail", min_tier="paid"),
    ])

    dist = Distributor(sender, [free_channel, paid_channel])
    asyncio.get_event_loop().run_until_complete(dist.send(cr))

    # Free channel should only get the free message
    free_calls = [c for c in sender.send.call_args_list if c[0][0] == "111"]
    assert len(free_calls) == 1
    assert free_calls[0][0][1] == "Summary"

    # Paid channel should get both messages
    paid_calls = [c for c in sender.send.call_args_list if c[0][0] == "222"]
    assert len(paid_calls) == 2
```

- [ ] **Step 3: Rewire pipeline.py**

Modify `src/hft_platform/reports/pipeline.py`:
- Change `build_report()` signature: return `ComposedReport | None` instead of `dict[str, list[str]] | None`
- Replace stages 2-4 imports with: `from hft_platform.reports.facts import extract_all`, `from hft_platform.reports.reasoner import reason_all`, `from hft_platform.reports.composer import ReportComposer`
- New flow: collect → collect_cross_day → extract_all → reason_all → ReportComposer.compose → return ComposedReport
- Update `run_pipeline()` to pass ComposedReport to distributor
- Update CLI debug/dry-run paths to iterate `cr.messages` and print text parts

- [ ] **Step 4: Update distributor.py**

Modify `src/hft_platform/reports/distributor.py`:
- Add `send_photo()` method to `ReportSender` (POST to Telegram sendPhoto API)
- Change `Distributor.send()` signature: accept `ComposedReport` instead of `dict[str, list[str]]`
- For each channel, filter `cr.messages` by `min_tier` vs `channel.tier` (free channel gets min_tier="free" only; paid/owner channels get all)
- For each MessagePart: kind="text" → `sender.send()`, kind="image" → `sender.send_photo()`

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_pipeline_v2.py tests/unit/test_distributor_v2.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/reports/pipeline.py src/hft_platform/reports/distributor.py \
       tests/unit/test_report_pipeline_v2.py tests/unit/test_distributor_v2.py
git commit -m "feat(reports): rewire pipeline and distributor to ComposedReport contract"
```

---

### Task 8: Update Bot Handlers + Scheduler

**Files:**
- Modify: `src/hft_platform/bot/handlers.py`
- Modify: `src/hft_platform/bot/scheduler.py`
- Test: `tests/unit/test_bot_handlers.py` (update existing)

- [ ] **Step 1: Update handlers.py**

Modify `src/hft_platform/bot/handlers.py`:
- `cmd_report()`: `build_report()` now returns `ComposedReport`. Iterate `cr.messages`, for each: if kind="text" → `send_message()`, if kind="image" → `send_photo(BytesIO(msg.image))`.
- `cmd_levels()`: Import `extract_all` + `LevelReasoner` from new modules. Use `collect_core()` + dummy cross-day facts. Call `LevelReasoner().analyze(fr)` to get `list[EnrichedLevel]`. Format with side/strength/sources.
- `cmd_flow()`: Import `extract_all` from new modules. Use `collect_core()`. Format `FactReport.flow` (session_ud, net_flow, strongest bars) + `FactReport.segments` (per-segment U/D table).

- [ ] **Step 2: Update scheduler.py**

Modify `src/hft_platform/bot/scheduler.py`:
- `_push_report()`: `build_report()` returns `ComposedReport`. Iterate `cr.messages`, dispatch text/image. Update `last_day_report` / `last_night_report`.

- [ ] **Step 3: Run existing bot tests + add regression**

Run: `uv run pytest tests/unit/test_bot_handlers.py tests/unit/test_bot_scheduler.py -v`

Fix any failures due to changed return type of `build_report()`. Key change: mocks that return `{"paid": [...]}` must now return `ComposedReport(messages=[...])`.

- [ ] **Step 4: Commit**

```bash
git add src/hft_platform/bot/handlers.py src/hft_platform/bot/scheduler.py \
       tests/unit/test_bot_handlers.py tests/unit/test_bot_scheduler.py
git commit -m "feat(bot): update handlers and scheduler to use ComposedReport"
```

---

### Task 9: Add matplotlib to Dockerfile

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Add matplotlib to Dockerfile**

Find the existing `RUN pip install` line in `Dockerfile` and add matplotlib:

```dockerfile
RUN pip install --no-cache-dir "python-telegram-bot[job-queue]>=21.0" "matplotlib>=3.8"
```

- [ ] **Step 2: Verify Dockerfile syntax**

Run: `docker compose config --quiet`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "chore: add matplotlib to Dockerfile for flow heatmap"
```

---

### Task 10: Integration Test — Full Pipeline

**Files:**
- Create: `tests/unit/test_report_integration.py`

- [ ] **Step 1: Write full pipeline integration test**

Create `tests/unit/test_report_integration.py`:

```python
"""Integration test: full three-layer pipeline with fixture data."""
from __future__ import annotations

from hft_platform.reports.composer import ReportComposer
from hft_platform.reports.facts import extract_all
from hft_platform.reports.models import (
    Bar5m,
    ComposedReport,
    DaySnapshot,
    FlowBar,
    LargeTrade,
    SessionData,
)
from hft_platform.reports.reasoner import reason_all


def _build_fixture_session() -> SessionData:
    """Realistic fixture with enough data to exercise all extractors."""
    bars = [
        Bar5m(f"2026-03-27 {h:02d}:{m:02d}:00",
              204000000 + i * 10000, 204200000 + i * 10000,
              203800000 + i * 10000, 204100000 + i * 10000,
              100 + i * 5, 50)
        for i, (h, m) in enumerate(
            [(h, m) for h in range(9, 14) for m in range(0, 60, 5)]
        )
    ]
    flow = [
        FlowBar(f"2026-03-27 {h:02d}:{m:02d}:00", 50,
                100 + i * 3, 50 + (i % 5) * 3, 50 - (i % 5) * 2 + 10,
                max(0, (i % 5) * 3 - (i % 5) * 2 + 10 - 100 - i * 3),
                (50 + (i % 5) * 3) / max(1, (50 - (i % 5) * 2 + 10)),
                (i % 5) * 3 - ((i % 5) * 2 - 10))
        for i, (h, m) in enumerate(
            [(h, m) for h in range(9, 14) for m in range(0, 60, 5)]
        )
    ]
    trades = [
        LargeTrade("2026-03-27 09:15:00", 204500000, 50, "buy"),
        LargeTrade("2026-03-27 09:20:00", 204550000, 40, "sell"),
        LargeTrade("2026-03-27 10:30:00", 205000000, 80, "sell"),
        LargeTrade("2026-03-27 12:45:00", 204200000, 60, "sell"),
    ]
    return SessionData(
        session="day", symbol="TXFD6", date="2026-03-27",
        open=204000000, high=206500000, low=203500000, close=204200000,
        volume=15000, tick_count=60000,
        bars_5m=bars[:20], flow_5m=flow[:20], large_trades=trades,
        spread_dist={2: 5000, 3: 3000, 4: 1000},
        depth_imbalance=[],
    )


def test_full_pipeline_produces_composed_report():
    sd = _build_fixture_session()
    prev_days = [DaySnapshot(
        "2026-03-26", "day", 205000000, 207000000, 204000000,
        206000000, 18000, 1.15, 800,
    )]

    # Layer 1
    fr = extract_all(sd, prev_days=prev_days)
    assert len(fr.segments) >= 2
    assert fr.flow.session_ud > 0
    assert fr.volatility.atr_5m > 0

    # Layer 2
    rr = reason_all(fr)
    assert rr.bias.bias in ("bullish", "bearish", "neutral")
    assert len(rr.levels) >= 1
    assert len(rr.narrative.storyline) >= 1

    # Layer 3
    cr = ReportComposer().compose(fr, rr)
    assert isinstance(cr, ComposedReport)
    assert len(cr.messages) >= 7

    # Verify tier assignments
    free_msgs = [m for m in cr.messages if m.min_tier == "free"]
    paid_msgs = [m for m in cr.messages if m.min_tier == "paid"]
    assert len(free_msgs) >= 2  # summary + disclaimer
    assert len(paid_msgs) >= 5

    # Verify no message exceeds Telegram limit
    for msg in cr.messages:
        if msg.kind == "text":
            assert len(msg.content) <= 4096, f"Message too long: {len(msg.content)} chars"


def test_pipeline_with_empty_prev_days():
    """Pipeline should work gracefully with no cross-day data."""
    sd = _build_fixture_session()
    fr = extract_all(sd, prev_days=[])
    rr = reason_all(fr)
    cr = ReportComposer().compose(fr, rr)
    assert isinstance(cr, ComposedReport)
    assert len(cr.messages) >= 7
```

- [ ] **Step 2: Run integration test**

Run: `uv run pytest tests/unit/test_report_integration.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/unit/test_report_*.py tests/unit/test_bot_*.py tests/unit/test_collector_*.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_report_integration.py
git commit -m "test(reports): add integration test for full three-layer pipeline"
```

---

### Task 11: Delete Old Modules

**Files:**
- Delete: `src/hft_platform/reports/signals.py`
- Delete: `src/hft_platform/reports/scenarios.py`
- Delete: `src/hft_platform/reports/renderer.py`
- Modify: `src/hft_platform/reports/__init__.py` (update exports)

- [ ] **Step 1: Verify no remaining imports of old modules**

Run:
```bash
uv run ruff check src/ --select F401,E402 2>&1 | grep -E "signals|scenarios|renderer"
grep -r "from hft_platform.reports.signals" src/ --include="*.py"
grep -r "from hft_platform.reports.scenarios" src/ --include="*.py"
grep -r "from hft_platform.reports.renderer" src/ --include="*.py"
```

Expected: No hits outside the old modules themselves and their tests.

- [ ] **Step 2: Delete old modules**

```bash
rm src/hft_platform/reports/signals.py
rm src/hft_platform/reports/scenarios.py
rm src/hft_platform/reports/renderer.py
```

- [ ] **Step 3: Update __init__.py exports**

Replace old exports (SignalEngine, ScenarioBuilder, ReportRenderer) with new ones (extract_all, reason_all, ReportComposer, ComposedReport).

- [ ] **Step 4: Clean up old test files that test deleted modules**

Remove or update tests that directly test `SignalEngine`, `ScenarioBuilder`, `ReportRenderer`. The logic they tested is now covered by `test_report_facts.py`, `test_report_reasoner.py`, `test_report_composer.py`.

- [ ] **Step 5: Run full test suite to verify nothing is broken**

Run: `uv run pytest tests/ -x -q`
Expected: All tests PASS, no import errors

- [ ] **Step 6: Run lint + typecheck**

Run: `uv run ruff check src/ tests/ && uv run mypy src/hft_platform/reports/`
Expected: Clean

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(reports): delete signals.py, scenarios.py, renderer.py — logic migrated to three-layer architecture"
```

---

### Task 12: Deploy + Smoke Test

**Files:** None (ops task)

- [ ] **Step 1: Run make ci locally**

```bash
make ci
```
Expected: lint + typecheck + tests all pass

- [ ] **Step 2: Commit any remaining fixes**

- [ ] **Step 3: Push to GitHub**

```bash
git push origin main
```

- [ ] **Step 4: Bundle and deploy to remote**

```bash
git bundle create /tmp/hft-report-opt.bundle main~12..main
scp /tmp/hft-report-opt.bundle ${REMOTE_USER}@${REMOTE_HOST}:~/subhft/
ssh ${REMOTE_USER}@${REMOTE_HOST} "cd ~/subhft && git fetch /tmp/hft-report-opt.bundle main:main && git checkout main"
```

- [ ] **Step 5: Rebuild and restart bot container**

```bash
ssh ${REMOTE_USER}@${REMOTE_HOST} "cd ~/subhft && docker compose build hft-bot && docker compose up -d hft-bot"
```

- [ ] **Step 6: Verify bot is running**

```bash
ssh ${REMOTE_USER}@${REMOTE_HOST} "cd ~/subhft && docker compose logs hft-bot --tail 20"
```
Expected: `bot.started`, no errors

- [ ] **Step 7: Test /report command in Telegram**

Send `/report day` in Telegram. Verify:
- 8 text messages + 1 heatmap image received
- Summary contains bias with evidence tree
- Cross-day comparison present
- Time-segment narrative present
- Chip structure present
- Dynamic S/R levels (no 3-level cap)
- Scenarios with reasoning chains
