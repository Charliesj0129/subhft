# Telegram Report Optimization — Three-Layer Analysis Architecture

**Date**: 2026-03-29
**Status**: Design Approved
**Scope**: Reports pipeline refactor + bot handler updates + flow heatmap

## 1. Problem Statement

Current report system has these issues:

1. **Bias judgment inaccurate**: U/D=0.92 maps to -0.8 score (too sensitive), produces "neutral" when should be "bearish"
2. **Too few S/R levels**: Hard-coded top-3 limit
3. **S/R classification bug**: `price > close` → resistance, no buffer zone. R3 below close gets misclassified.
4. **SC-01 "破底加速" doesn't fire**: Requires ≥2 supports, but when close is low most levels become resistance
5. **No time-segment analysis**: Opening/midday/closing dynamics invisible
6. **No chip structure**: Large trade clustering underutilized, no buy/sell zone identification
7. **No cross-day context**: No comparison with previous days' price/volume/flow
8. **Rules don't cross-reference**: 8 flat weighted rules operate independently

## 2. Target User

Owner (self-use). Professional terminology OK, data-dense, no length limit (10+ messages acceptable).

## 3. Architecture: Three-Layer Analysis

```
SessionData (ClickHouse Q1-Q7)
    ↓
Layer 1: FactExtractor → FactReport
    ↓
Layer 2: Reasoner → ReasoningReport
    ↓
Layer 3: ReportComposer → ComposedReport
```

### Pipeline/Distributor Contract Update

Current `build_report()` returns `dict[str, list[str]]` (tier → text messages).
New `build_report()` returns `ComposedReport`:

```python
@dataclass(slots=True)
class MessagePart:
    """A single part of the composed report."""
    kind: str       # "text" or "image"
    content: str    # text content (for kind="text")
    image: bytes | None = None  # PNG bytes (for kind="image")
    caption: str = ""  # image caption (for kind="image")

@dataclass(slots=True)
class ComposedReport:
    messages: list[MessagePart]   # ordered list of text + image parts
```

Backward compatibility:
- `pipeline.py::build_report()` returns `ComposedReport` (new signature)
- `pipeline.py::build_report_legacy()` wraps `build_report()` → `dict[str, list[str]]` (drops images, preserves text). Used by CLI `run_pipeline()` until distributor is updated.
- `distributor.py::Distributor.send()` updated to accept `ComposedReport`. For each part: `kind="text"` → `send_message()`, `kind="image"` → `send_photo()`.
- `bot/handlers.py` and `bot/scheduler.py` use new `ComposedReport` directly.
- CLI distribution path: if channel type is `telegram`, send both text + image. If channel type is `file` or `stdout`, skip image parts.

### 3.1 Layer 1 — FactExtractor

Pure facts, no judgment. Six fact extractors:

#### TimeSegmentFacts

Split session into segments aligned with actual collector time ranges:

- Day session (collector: 07:00-13:45 CST):
  - pre_open: 07:00-08:45 (pre-market matching, typically low volume)
  - opening: 08:45-09:30
  - midday: 09:30-12:00
  - closing: 12:00-13:45

- Night session (collector: 15:00-05:00 next day CST):
  - opening: 15:00-15:45
  - midday: 15:45-03:00
  - closing: 03:00-05:00

All segment volumes sum to SessionData.volume (no orphan ticks). `pre_open` is included in totals but may have zero ticks on some instruments. Segment boundaries are inclusive-start, exclusive-end.

```python
@dataclass(slots=True)
class SegmentFact:
    name: str              # "opening" / "midday" / "closing"
    time_range: str        # "08:45-09:30"
    ud_ratio: float
    net_flow: int
    volume: int
    volume_pct: float      # % of session total
    large_buy_count: int
    large_sell_count: int
    high: int              # segment high (scaled int)
    low: int               # segment low (scaled int)
    dominant_side: str     # "bull" / "bear" / "neutral"
```

#### ChipFacts

Large trade clustering + volume-at-price analysis:

```python
@dataclass(slots=True)
class ChipCluster:
    price_center: int
    price_range: tuple[int, int]
    buy_volume: int
    sell_volume: int
    trade_count: int
    dominant_side: str     # "buy" / "sell" / "mixed"
    first_ts: str          # earliest trade timestamp in cluster
    last_ts: str           # latest trade timestamp in cluster
    time_range: str        # "09:15-10:30" human-readable

@dataclass(slots=True)
class ChipFacts:
    clusters: list[ChipCluster]
    vap_peaks: list[PriceLevel]
    buy_zone: tuple[int, int] | None   # buy-dominant price range
    sell_zone: tuple[int, int] | None  # sell-dominant price range
    total_buy_volume: int              # session-wide large trade buy volume
    total_sell_volume: int             # session-wide large trade sell volume
    net_ratio: float                   # buy / (buy + sell), 0.5 = balanced
```

`total_buy_volume`, `total_sell_volume`, `net_ratio` are aggregated at extraction time so Layer 2 never needs to read raw trades. `ChipCluster.first_ts`/`last_ts` enable NarrativeReasoner to place cluster events on the timeline without breaking layer separation.

Clustering tolerance: configurable, default 50,000 scaled units (~5 pts). Improved from current IF-04's fixed 30,000 (~3 pts).

#### FlowFacts

Extracted from current IF-01/02/05/06 logic, fact-only:

```python
@dataclass(slots=True)
class FlowFacts:
    session_ud: float
    session_net_flow: int
    strongest_buy_bar: FlowBar
    strongest_sell_bar: FlowBar
    sustained_runs: list[tuple[str, int, str]]  # [(side, bar_count, time_range)]
    volume_spikes: list[tuple[FlowBar, float]]  # [(bar, ratio_vs_avg)]
    eod_ud: float
    eod_drift: float       # eod_ud - session_ud
```

#### StructureFacts

Extracted from current SR-02/03/04/06 logic:

```python
@dataclass(slots=True)
class StructureFacts:
    double_bottoms: list[PriceLevel]
    double_tops: list[PriceLevel]
    failed_breakouts: list[PriceLevel]
    round_numbers: list[PriceLevel]
    session_high: PriceLevel
    session_low: PriceLevel
```

#### VolatilityFacts

Computed from existing Q2 (5m bars), no new query needed:

```python
@dataclass(slots=True)
class VolatilityFacts:
    atr_5m: int            # Average True Range of 5m bars (scaled int)
    session_range: int     # high - low (scaled int)
    range_vs_atr: float    # session_range / (atr_5m * bar_count) — expansion/contraction
```

ATR is computed from 5m bars' high-low-prev_close (standard Wilder ATR).
Used by ScenarioReasoner for target/stop calculation and range_bound trigger.

#### CrossDayFacts (NEW — requires Q7 query)

```python
@dataclass(slots=True)
class DaySnapshot:
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
    prev_days: list[DaySnapshot]   # 1-3 previous days
    volume_change_pct: float       # vs previous day
    price_position: str            # "above_prev_high" / "below_prev_low" / "inside_range"
    trend_direction: str           # "up" / "down" / "sideways"
    flow_reversal: bool            # flow direction flipped vs previous day
```

Q7 query: single ClickHouse query for previous 3 days' OHLCV + flow summary. Low cost (aggregation only).

#### FactReport

```python
@dataclass(slots=True)
class FactReport:
    session_data: SessionData      # raw data preserved
    segments: list[SegmentFact]
    chips: ChipFacts
    flow: FlowFacts
    structure: StructureFacts
    volatility: VolatilityFacts
    cross_day: CrossDayFacts
```

### 3.2 Layer 2 — Reasoner

Four reasoners that can cross-reference any facts:

#### BiasReasoner

Evidence-driven, replaces weighted average:

```python
@dataclass(slots=True)
class Evidence:
    source: str        # "flow.session_ud", "chip.sell_zone", etc.
    fact_value: str    # human-readable fact
    direction: str     # "bull" / "bear" / "neutral"
    weight: float      # 0.0-1.0

@dataclass(slots=True)
class BiasJudgment:
    bias: str               # "bullish" / "bearish" / "neutral"
    confidence: float       # 0.0-1.0
    evidences: list[Evidence]
    summary: str            # one-line summary with key evidence
```

Evidence sources and weights:

| Source | Weight | Bull condition | Bear condition |
|--------|--------|----------------|----------------|
| flow.session_ud | 0.20 | > 1.15 | < 0.85 |
| flow.eod_drift | 0.15 | > +0.20 | < -0.20 |
| flow.sustained_runs | 0.15 | bull run ≥ 4 bars | bear run ≥ 4 bars |
| chips.net_ratio | 0.20 | net_ratio > 0.57 (buy > sell × 1.3) | net_ratio < 0.43 (sell > buy × 1.3) |
| segments.closing | 0.10 | closing.dominant_side == "bull" | closing.dominant_side == "bear" |
| cross_day.trend | 0.10 | trend_direction == "up" | trend_direction == "down" |
| cross_day.flow_reversal | 0.05 | reversal to bull | reversal to bear |
| structure.failed_breakouts | 0.05 | failed low breakout | failed high breakout |

Confidence calculation:
- Count evidences pointing same direction as bias
- ≥ 4 concordant sources from different categories → high confidence (0.7+)
- Contradictions explicitly noted in summary

Dead zone: U/D 0.85-1.15, chip ratio 0.77-1.30 treated as "slight lean", need corroboration.

#### LevelReasoner

Unified S/R with confluence scoring:

```python
@dataclass(slots=True)
class EnrichedLevel:
    price: int
    side: str               # "support" / "resistance" / "pivot" (buffer zone)
    strength: float         # 0.0-1.0
    sources: list[str]      # human-readable source descriptions
    confluence_count: int   # number of independent sources within ±5 pts
```

Classification with buffer:
- `price > close + 50,000` (5 pts) → resistance
- `price < close - 50,000` (5 pts) → support
- Within buffer → "pivot" (攻防關鍵位)

No hard limit on count. Display rules:
- All levels with confluence ≥ 2
- Plus levels with confluence = 1 but strength ≥ 0.7
- Sorted by strength descending within each side

#### ScenarioReasoner

Fact-triggered, dynamic count:

| Scenario | ID | Trigger condition | Target |
|----------|----|-------------------|--------|
| 破底加速 | break_below | S1 exists + bias ≠ bullish | prev_day_low or ATR-based |
| 守支撐反彈 | hold_bounce | S1 exists + any bullish evidence | R1 or prev_day_high |
| 趨勢延續 | trend_continue | cross_day trend ≥ 2 days same direction + bias concordant | ATR extension |
| 跳空回補 | gap_fill | open vs prev_close gap ≥ 0.3% | prev_close |
| 區間震盪 | range_bound | intraday range < ATR × 0.7 + bias neutral | range boundaries |

Only scenarios with satisfied triggers are generated. Each includes:

```python
@dataclass(slots=True)
class Scenario:
    id: str
    title: str
    probability: str       # "高" / "中" / "低"
    trigger: str           # "若跌破 S1 (20,389)"
    target: int
    stop: int
    reasoning: list[str]   # evidence chain
```

Probability assignment: based on bias concordance + evidence count supporting the scenario.

#### NarrativeReasoner

Weaves time-segment facts into a storyline:

```python
@dataclass(slots=True)
class NarrativeReport:
    storyline: list[str]       # chronological paragraphs (1 per segment)
    turning_points: list[tuple[str, str]]  # [(time, event description)]
    conclusion: str
```

Turning point detection:
- U/D ratio flips direction between adjacent segments
- Volume spike (> 2× average) with directional bias
- Large trade cluster appears
- Price breaks session high/low

Narrative is template-driven with conditional clauses, not LLM-generated.

#### ReasoningReport

```python
@dataclass(slots=True)
class ReasoningReport:
    bias: BiasJudgment
    levels: list[EnrichedLevel]
    scenarios: list[Scenario]
    narrative: NarrativeReport
```

### 3.3 Layer 3 — ReportComposer

Assembles ReasoningReport + FactReport into Telegram messages:

| # | Message | Content |
|---|---------|---------|
| 1 | 📊 摘要 | OHLCV + bias conclusion with evidence summary + cross-day comparison |
| 2 | 📖 時段敘事 | Opening → midday → closing storyline + turning points |
| 3 | 🔍 流向深度 | Session/segment U/D, sustained runs, volume spikes, EOD drift |
| 4 | 🏦 籌碼結構 | Chip clusters, buy/sell zones, dominant player behavior |
| 5 | 🎯 關鍵點位 | All enriched S/R levels with confluence sources and strength |
| 6 | 📋 情境規劃 | Dynamic scenarios with reasoning chains + entry/target/stop |
| 7 | 📈 流向熱力圖 | matplotlib image (see §4) |
| 8 | ⚠️ 免責聲明 | Static disclaimer |

Each text message ≤ 4096 chars. If a section exceeds limit, split into continuation messages.

### 3.4 Summary Message Example

```
📊 台指期日盤報告 2026-03-27

TXFD6  20,456 → 20,289  ▼167 (-0.82%)
High 20,523 | Low 20,234 | Vol 18,450
Ticks 45,678 | Spread 中位數 2pts

偏向：偏空 (信心 72%)
├ 全場 U/D=0.82 空方主導
├ 尾盤空方加壓 (U/D 0.95→0.71)
├ 大單賣壓 380口 vs 買盤 210口
└ 連續第 2 日量縮 (-12%)

vs 前日：價格跌破前日低點，流向從偏多翻空
vs 前 3 日：下行趨勢，量能遞減
```

## 4. Flow Heatmap

Generated with `matplotlib`, sent via `bot.send_photo()` as `io.BytesIO` (no disk write).

Layout:
- X axis: time (5-min intervals)
- Color strip: U/D ratio → red (bear) ↔ white (neutral) ↔ green (bull)
- Volume bars: gray semi-transparent, height = volume
- Large trade markers: ▲ buy / ▼ sell, size proportional to volume
- Price line: right Y axis, overlaid

Colormap: custom diverging (red → white → green), centered at U/D = 1.0.

Dependencies: `matplotlib` (already common, add to Dockerfile). No new external deps.

## 5. Bug Fixes (Integrated into New Architecture)

### Bug 1: Bias over-sensitivity
**Was**: `score = (ratio - 1.0) * 10.0` — U/D=0.92 → -0.8
**Fix**: BiasReasoner dead zone 0.85-1.15 requires corroboration from other evidence sources.

### Bug 2: R3 below close classified as resistance
**Was**: `price > close` → resistance, no buffer
**Fix**: LevelReasoner buffer zone ±5 pts. Within buffer → "pivot" (攻防關鍵位).

### Bug 3: SC-01 never fires
**Was**: Requires ≥2 supports
**Fix**: ScenarioReasoner only needs S1. Target from ATR or prev_day_low.

### Bug 4: Only 3 S/R levels
**Was**: Hard-coded `[:3]`
**Fix**: Dynamic count based on confluence ≥ 2 or (confluence=1 + strength ≥ 0.7).

## 6. Rule Migration

All 8 existing rules are decomposed into Layer 1 facts:

| Old Rule | Destination |
|----------|-------------|
| IF-01 Session U/D | FlowFacts.session_ud |
| IF-02 Sustained Pressure | FlowFacts.sustained_runs |
| IF-03 Large Trade Net | ChipFacts (buy/sell volumes) |
| IF-04 Clustering | ChipFacts.clusters (improved tolerance) |
| IF-05 EOD Drift | FlowFacts.eod_drift + SegmentFact(closing) |
| IF-06 Volume Spike | FlowFacts.volume_spikes |
| SR-02 Double Pattern | StructureFacts.double_bottoms/tops |
| SR-06 Failed Breakout | StructureFacts.failed_breakouts |

Logic preserved, organization upgraded. Old modules (`signals.py`, `scenarios.py`, `renderer.py`) deleted after new architecture passes tests.

## 7. File Impact

| File | Action | Description |
|------|--------|-------------|
| `reports/facts.py` | NEW | Layer 1 — all FactExtractor classes |
| `reports/reasoner.py` | NEW | Layer 2 — all Reasoner classes |
| `reports/composer.py` | NEW | Layer 3 — ReportComposer |
| `reports/heatmap.py` | NEW | Flow heatmap generation |
| `reports/models.py` | MODIFY | Add new dataclasses (Evidence, BiasJudgment, EnrichedLevel, etc.) |
| `reports/collector.py` | MODIFY | Add Q7 cross-day query |
| `reports/pipeline.py` | MODIFY | `build_report()` returns `ComposedReport`, add `build_report_legacy()` wrapper |
| `reports/distributor.py` | MODIFY | `Distributor.send()` accepts `ComposedReport`, handles text + image parts |
| `bot/handlers.py` | MODIFY | `/levels`, `/flow` use new architecture |
| `bot/scheduler.py` | MODIFY | Use `ComposedReport` for scheduled push |
| `Dockerfile` | MODIFY | Add `matplotlib` |
| `reports/signals.py` | DELETE | Logic migrated to facts.py + reasoner.py |
| `reports/scenarios.py` | DELETE | Logic migrated to reasoner.py |
| `reports/renderer.py` | DELETE | Replaced by composer.py |

## 8. ClickHouse Query Budget

| Query | Purpose | Cost |
|-------|---------|------|
| Q1 | OHLCV | Trivial (1 row) |
| Q2 | 5m bars | Light (~170 rows day) |
| Q3 | 5m flow | Light (~170 rows) |
| Q4 | Large trades | Light (variable, ~50-200 rows) |
| Q5 | Spread dist | Medium (can OOM on wide data) |
| Q6 | Depth imbalance | Medium (hourly aggregation) |
| Q7 (NEW) | Cross-day summary | Trivial (3 rows, aggregation only) |

Q7 is a single aggregation query over previous 3 trading days. Negligible cost.

## 9. Bot Handler Updates

- `/report`: Sends full 8-message + 1-image report via new pipeline
- `/levels`: Uses LevelReasoner directly (via lightweight `collect_core()` + Q7)
- `/flow`: Uses FlowFacts + SegmentFacts (via `collect_core()`)
- `/status`: No change
- Scheduled push: Same schedule, new pipeline

## 10. Testing Strategy

- Unit tests for each Layer 1 extractor (mock SessionData)
- Unit tests for each Layer 2 reasoner (mock FactReport)
- Unit tests for Layer 3 composer (mock ReasoningReport)
- Unit test for heatmap generation (verify BytesIO output)
- Integration test: full pipeline with fixture SessionData
- Bug regression tests: specific cases for bugs 1-4
