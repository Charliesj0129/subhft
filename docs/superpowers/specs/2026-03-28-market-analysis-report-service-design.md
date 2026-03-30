# Market Analysis Report Service — Design Spec

**Date**: 2026-03-28
**Status**: Approved
**Author**: Charlie + Claude

## 1. Overview

An automated market analysis report service that generates actionable trading intelligence from HFT platform's ClickHouse tick data, delivering structured Telegram messages with informed flow analysis, precise price levels, and scenario planning.

### Goals

1. **Automate** the manual analysis workflow (uptick/downtick flow, large trade tracking, support/resistance detection, scenario planning) into a reproducible pipeline
2. **Deliver** structured Telegram reports at day session close and night session close
3. **Monetize** via tiered Telegram channels (free summary vs paid full analysis)
4. **Iterate** — start with TXFD6/TMFD6, expand to stocks and options later

### Non-Goals

- Real-time alerting (existing `NotificationDispatcher` handles this)
- ML-based predictions (rule-driven only, may evolve later)
- Embedded in hot path (offline batch analysis only)

## 2. Architecture

### Pipeline Model

```
Cron trigger (13:50 day / 05:10 night)
  → ReportPipeline (orchestrator)
    → Stage 1: DataCollector    (ClickHouse → SessionData)
    → Stage 2: SignalEngine     (SessionData → SignalReport)
    → Stage 3: ScenarioBuilder  (SignalReport → ScenarioReport)
    → Stage 4: ReportRenderer   (ScenarioReport → List[TelegramMessage])
    → Stage 5: Distributor      (messages → Telegram channels)
```

Each stage is independently testable. Stages communicate via typed dataclass contracts.

### File Layout

```
src/hft_platform/reports/
├── __init__.py
├── pipeline.py              # ReportPipeline orchestrator + CLI entry
├── collector.py             # Stage 1: DataCollector
├── signals.py               # Stage 2: SignalEngine
├── scenarios.py             # Stage 3: ScenarioBuilder
├── renderer.py              # Stage 4: ReportRenderer
├── distributor.py           # Stage 5: Distributor
├── models.py                # Inter-stage data contracts
└── rules/
    ├── __init__.py
    ├── support_resistance.py
    ├── informed_flow.py
    └── scenario_rules.py
```

Config:
```
config/reports/channels.yaml   # Telegram channel definitions
```

## 3. Data Contracts (models.py)

### SessionData (Stage 1 → Stage 2)

**Price scale convention**: All price fields in this module use **platform scale (x10,000)**,
matching `contracts/types.py:ScaledPrice`. The DataCollector is responsible for converting
ClickHouse scale (x1,000,000) to platform scale at ingestion boundary
(`ch_value // CH_TO_PLATFORM_DIVISOR` where divisor = 100, per `monitor/_types.py:448`).
This ensures the reports module can interoperate with other platform modules without scale confusion.

```python
from hft_platform.contracts.types import ScaledPrice

@dataclass(slots=True)
class Bar5m:
    ts: str                    # "2026-03-27 15:00:00" (Asia/Taipei)
    open: ScaledPrice          # platform scale (x10,000)
    high: ScaledPrice
    low: ScaledPrice
    close: ScaledPrice
    volume: int
    ticks: int

@dataclass(slots=True)
class FlowBar:
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
    ts: str
    price: ScaledPrice         # platform scale (x10,000)
    volume: int
    direction: str             # "buy" | "sell" | "unknown" (assigned by SignalEngine)

@dataclass(slots=True)
class DepthBar:
    hour: int
    avg_bid_vol: float
    avg_ask_vol: float
    bid_ratio: float

@dataclass(slots=True)
class SessionData:
    session: str               # "day" | "night"
    symbol: str                # "TXFD6"
    date: str                  # "2026-03-27" (trading date, see §5 Date Resolution)
    open: ScaledPrice
    high: ScaledPrice
    low: ScaledPrice
    close: ScaledPrice
    volume: int
    tick_count: int
    bars_5m: list[Bar5m]
    flow_5m: list[FlowBar]
    large_trades: list[LargeTrade]
    spread_dist: dict[int, int]     # spread_pts → count
    depth_imbalance: list[DepthBar]
```

### SignalReport (Stage 2 → Stage 3)

```python
@dataclass(slots=True)
class PriceLevel:
    price: ScaledPrice         # platform scale (x10,000)
    strength: float            # 0.0-1.0
    reason: str                # e.g. "雙底", "大單聚集 28口"

@dataclass(slots=True)
class SignalReport:
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
    bias: str                  # "bearish" | "bullish" | "neutral"
    bias_confidence: float     # 0.0-1.0
    rule_scores: dict[str, float]  # rule_id → score for transparency
```

### ScenarioReport (Stage 3 → Stage 4)

```python
@dataclass(slots=True)
class Scenario:
    id: str                    # "break_below_support"
    label: str                 # "破底加速"
    probability: str           # "較高" | "較低"
    condition: str             # "若破 32,375"
    target: int                # scaled price
    description: str           # full text

@dataclass(slots=True)
class KeyLevel:
    price: int
    label: str                 # "S1", "R1"
    importance: int            # 1-3 stars
    reason: str

@dataclass(slots=True)
class ScenarioReport:
    signal: SignalReport
    direction: str             # "偏空" | "偏多" | "中性"
    confidence_pct: int        # 60-80
    entry_zone: tuple[ScaledPrice, ScaledPrice]
    target: ScaledPrice
    stop_loss: ScaledPrice
    scenarios: list[Scenario]
    key_levels: list[KeyLevel]
```

## 4. SignalEngine Rules

### A. Informed Flow Rules (rules/informed_flow.py)

| ID | Name | Logic | Score |
|----|------|-------|-------|
| IF-01 | Session U/D Ratio | total uptick / total downtick | < 0.9 → -1.0, > 1.1 → +1.0, linear between |
| IF-02 | Sustained Pressure | consecutive 5min bars with U/D < 0.7 or > 1.3 | count ≥ 4 → ±1.0 |
| IF-03 | Large Trade Net | net large trade volume (buy - sell) | normalized to -1.0 ~ +1.0 |
| IF-04 | Large Trade Cluster | ≥3 large trades within ±3pts and 60s | marks price as institutional level |
| IF-05 | End-of-Session Drift | last 30min U/D vs session U/D | divergence > 0.2 → directional score |
| IF-06 | Volume Spike Window | 5min vol > 2× session mean | marks as key event, inherits direction |

### B. Support/Resistance Rules (rules/support_resistance.py)

| ID | Name | Logic |
|----|------|-------|
| SR-01 | Large Trade Price | ≥20 lot trades cluster at price → S/R level |
| SR-02 | Double Bottom/Top | 2 touches at same price (±5pts) with reversal |
| SR-03 | Round Number | multiples of 100/500/1000 pts |
| SR-04 | Session Extreme | session high & low |
| SR-05 | Volume-at-Price | 50pts buckets, top 3 by volume |
| SR-06 | Failed Breakout | break above/below then reversal (e.g. 38 lots打回) |

### C. Scoring & Aggregation

```python
WEIGHTS = {
    "IF-01_session_ud":      0.25,
    "IF-02_sustained":       0.15,
    "IF-03_large_net":       0.20,
    "IF-04_cluster":         0.10,
    "IF-05_eod_drift":       0.10,
    "IF-06_vol_spike":       0.05,
    "SR-02_double_pattern":  0.10,
    "SR-06_failed_breakout": 0.05,
}
# weighted_sum < -0.3 → bearish
# weighted_sum > +0.3 → bullish
# else → neutral
# |weighted_sum| maps to confidence_pct (30% ~ 80%)
```

### D. Price Level Generation

1. Collect all PriceLevel from SR rules
2. Rank by strength (touch count × large trade volume × pattern bonus)
3. Take top 3 supports + top 3 resistances
4. Entry zone = nearest resistance ± mean bounce (bearish) or nearest support ± mean pullback (bullish)
5. Target = next S/R level in trade direction
6. Stop loss = entry zone opposite side, distance = 5-day ATR × 0.5

### E. Scenario Rules (rules/scenario_rules.py)

| ID | Name | Trigger | Target |
|----|------|---------|--------|
| SC-01 | Break Below Support | price breaks strongest support | next support below |
| SC-02 | Hold and Bounce | support holds + U/D flips bullish | nearest resistance |
| SC-03 | Trend Continuation | session U/D < 0.85 + large net sell | entry at resistance, target at support |
| SC-04 | Reversal Setup | double bottom + end-of-session U/D > 1.2 | break above neckline target |

## 5. DataCollector (collector.py)

### Timezone Handling

All ClickHouse queries use explicit timezone:

```python
TZ = "Asia/Taipei"
TS_EXPR = f"toDateTime64(exch_ts/1e9, 3, '{TZ}')"
```

Night session filter uses exch_ts range (NOT toDate()):

```python
def _night_filter(self, date: str) -> str:
    # date='2026-03-27' → 3/27 15:00 ~ 3/28 05:00 CST
    return (
        f"{TS_EXPR} >= toDateTime64('{date} 15:00:00', 3, '{TZ}') AND "
        f"{TS_EXPR} < toDateTime64('{date} 15:00:00', 3, '{TZ}') + INTERVAL 14 HOUR"
    )
```

### Date Resolution (--session night default date)

When `--date` is not provided, the pipeline resolves the **trading date** automatically:

```python
def resolve_trading_date(session: str, now: datetime | None = None) -> str:
    """Determine which trading date to report on.

    Day session: always today (cron runs at 13:50 same day).
    Night session: the date the session OPENED on (15:00 side).
      - Cron runs at 05:10 on 3/28 → night session opened 3/27 15:00
      - So trading_date = yesterday (now.date() - 1 day)
      - Edge case: if now.hour >= 15, session just opened today → trading_date = today
    """
    if now is None:
        now = datetime.now(ZoneInfo("Asia/Taipei"))
    if session == "day":
        return now.strftime("%Y-%m-%d")
    # night: cron fires at 05:10 next day, so the session date is yesterday
    if now.hour < 15:
        return (now.date() - timedelta(days=1)).isoformat()
    else:
        return now.strftime("%Y-%m-%d")
```

Examples (all times Asia/Taipei):
| now | --session | resolved date | session range |
|-----|-----------|---------------|---------------|
| 2026-03-27 13:50 | day | 2026-03-27 | 07:00-13:45 on 3/27 |
| 2026-03-28 05:10 | night | **2026-03-27** | 3/27 15:00 ~ 3/28 05:00 |
| 2026-03-27 15:30 | night | 2026-03-27 | 3/27 15:00 ~ 3/28 05:00 |

### Price Scale Boundary

ClickHouse stores `price_scaled` at **x1,000,000** (`recorder/mapper.py:9`).
Platform internal convention is **x10,000** (`contracts/types.py:6`).

DataCollector converts at the CH read boundary:

```python
from hft_platform.monitor._types import CH_PRICE_SCALE, PLATFORM_SCALE, CH_TO_PLATFORM_DIVISOR

def _ch_to_platform(ch_price: int) -> int:
    """Convert CH scale (x1,000,000) to platform scale (x10,000)."""
    return ch_price // CH_TO_PLATFORM_DIVISOR  # divisor = 100
```

All pipeline internals use **platform scale (x10,000)**. Only ReportRenderer converts to human-readable (`price // 10_000`).

### Large Trade Threshold

```python
LARGE_TRADE_THRESHOLD = {
    "TXFD6": 10,   # ≥10 lots
    "TMFD6": 30,   # ≥30 lots (1/4 contract size, ×4 to compensate)
    "MXFD6": 30,
}
```

### ClickHouse Memory Protection

Every query includes: `SETTINGS max_memory_usage = 2000000000`

Avoid: quantile() on array columns, full-table scans without exch_ts range.

### Queries

| ID | Purpose | Key Technique |
|----|---------|---------------|
| Q1 | Session OHLCV | argMin/argMax + min/max/sum |
| Q2 | 5-min bars | toStartOfFiveMinutes + GROUP BY |
| Q3 | Uptick/Downtick | lagInFrame() OVER (ORDER BY exch_ts) |
| Q4 | Large trades | WHERE volume >= threshold |
| Q5 | Spread distribution | GROUP BY spread_pts (integer division) |
| Q6 | Hourly depth imbalance | avg(bids_vol[1]) / avg(bids_vol[1] + asks_vol[1]) |

## 6. ReportRenderer (renderer.py)

### Message Structure

| # | Content | Free | Paid |
|---|---------|------|------|
| 1 | 行情摘要 (OHLCV, spread) | ✅ | ✅ |
| 2 | 知情流 (U/D, large trades) | 精簡 | 完整 |
| 3 | 精準點位 (S/R, entry/target/stop) | ❌ | ✅ |
| 4 | 情境規劃 (scenarios) | ❌ | ✅ |
| 5 | 免責聲明 | ✅ | ✅ |

Free tier = messages 1, 2(brief), 5 → **2-3 messages**
Paid tier = all 5 → **5 messages**

### Template Implementation

- f-string + helper functions (no Jinja2)
- Prices converted to human-readable only at render time
- HTML parse mode for Telegram (bold, monospace, line separators)
- Each message ≤ 4096 characters (Telegram limit)

### Example Output (Paid Tier, Night Session)

Message 1:
```
📊 台指期夜盤報告 2026-03-27

TXFD6  33,049 → 32,438  ▼611 (-1.85%)
High 33,049 | Low 32,375 | Vol 58,107

TMFD6  33,080 → 32,438  ▼642 (-1.94%)

全場成交: 38,153 + 21,244 ticks
Spread 中位數: TMFD6 3pts | TXFD6 4pts
```

Message 2:
```
🔍 知情流分析

▎全場 U/D = 0.906  淨流 -1,581 口
▎最強空方: 21:50 U/D=0.533 net=-252
▎最強多方: 23:00 U/D=1.811 net=+167

▎大單 (≥10口):
  🔴 賣方 ~650 口  🟢 買方 ~380 口
  關鍵: 28口@32,400 (止損觸發)
       38口@32,610 (反彈打壓)
       32口@32,750 (多方嘗試)

▎時段 U/D:
  15-17 ███░░ 0.71 持續賣壓
  17-19 ██░░░ 0.73 加速殺盤
  19-20 ▓▓▓░░ 0.96 築底
  21-22 █░░░░ 0.63 二次崩殺
  22-23 ░░▓▓▓ 1.17 空方回補
  23-01 ██░░░ 0.78 再度偏空
```

Message 3:
```
🎯 關鍵點位

▎支撐:
  S1  32,375  ★★★ 雙底 (日盤+夜盤同價位)
  S2  32,000  ★★☆ 整千關卡
  S3  31,800  ★☆☆ 前波低點

▎壓力:
  R1  32,750  ★★★ 反彈天花板 (38口確認)
  R2  33,000  ★★☆ 被31口砸穿後未收復
  R3  33,200  ★☆☆ 前日高點區

▎進場參考 (空方):
  進場區  32,700-32,750
  目標    32,375
  止損    32,850
```

Message 4:
```
📋 情境規劃

【情境 A】破底加速 — 機率較高
  若破 32,375 → 目標看 32,000
  觸發: 開盤跳空低於 32,375
  特徵: 量增價跌 + 大單持續空方

【情境 B】雙底反彈 — 機率較低
  若守住 32,375 且站回 32,750
  → 空方失敗，目標看 33,000
  觸發: 開盤守 32,500 + U/D 翻 >1.1

【情境 C】區間震盪
  若在 32,375-32,750 之間反覆
  → 等方向確認再操作
  觀察: 大單方向 + 32,500 攻防
```

Message 5:
```
⚠️ 本報告基於歷史行情數據自動生成，
僅供參考，不構成投資建議。
投資有風險，請自行評估。
```

## 7. Distributor (distributor.py)

### Channel Config

Channel config is **pure env-var driven** (no YAML `${...}` substitution needed).
The existing `config/loader.py` uses `yaml.safe_load` which does NOT expand env vars,
so we avoid that pattern entirely.

```python
# reports/distributor.py
@dataclass(slots=True, frozen=True)
class ChannelConfig:
    name: str
    chat_id: str
    tier: str       # "free" | "paid"
    enabled: bool

def load_channels() -> list[ChannelConfig]:
    """Build channel list from environment variables."""
    channels = []
    # Owner channel — always uses the main bot chat_id
    owner_id = os.environ.get("HFT_TELEGRAM_CHAT_ID", "")
    if owner_id:
        channels.append(ChannelConfig("owner", owner_id, "paid", enabled=True))
    # Paid channel
    paid_id = os.environ.get("HFT_REPORT_PAID_CHANNEL_ID", "")
    if paid_id:
        channels.append(ChannelConfig("paid", paid_id, "paid",
            enabled=os.environ.get("HFT_REPORT_PAID_ENABLED", "0") == "1"))
    # Free channel
    free_id = os.environ.get("HFT_REPORT_FREE_CHANNEL_ID", "")
    if free_id:
        channels.append(ChannelConfig("free", free_id, "free",
            enabled=os.environ.get("HFT_REPORT_FREE_ENABLED", "0") == "1"))
    return channels
```

### Environment Variables (New)

| Variable | Purpose | Default |
|----------|---------|---------|
| `HFT_REPORT_ENABLED` | Master switch | `0` |
| `HFT_REPORT_PAID_CHANNEL_ID` | Paid channel chat_id | — |
| `HFT_REPORT_PAID_ENABLED` | Enable paid channel | `0` |
| `HFT_REPORT_FREE_CHANNEL_ID` | Free channel chat_id | — |
| `HFT_REPORT_FREE_ENABLED` | Enable free channel | `0` |

### Send Logic — New ReportSender (NOT reusing TelegramSender directly)

The existing `TelegramSender` has two problems for report delivery:
1. `enabled=False` by default, and rate-limiting silently drops non-critical messages
   within 1s window (`telegram.py:71` returns False, no retry).
2. It is a single-chat sender — no `chat_id` parameter on `send()`.

The Distributor creates its **own lightweight sender** wrapping `aiohttp` directly:

```python
class ReportSender:
    """Dedicated sender for report delivery. NOT the same as TelegramSender.

    Differences from TelegramSender:
    - Always enabled (if bot_token is set)
    - Accepts chat_id per call (multi-channel)
    - Queues messages with explicit inter-message delay (not rate-limit-drop)
    - Retries on transient failure (429/5xx) with backoff
    """

    async def send(self, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
        """Send one message. Returns True on success.

        On 429 (rate limited): wait Retry-After header, then retry (up to 3 times).
        On 5xx: retry with exponential backoff (1s, 2s, 4s).
        On 4xx (other): log error, return False, do not retry.
        """

    async def send_batch(self, chat_id: str, messages: list[str], delay_s: float = 1.5) -> int:
        """Send multiple messages sequentially with delay. Returns count of successful sends."""
```

The 1.5s default delay is conservative for Telegram group limits (20 msg/min = 3s/msg, but
we're sending to at most 3 channels sequentially, so 1.5s per message per channel is safe).

### Rollout Path

```
Phase 1: HFT_REPORT_ENABLED=1, owner channel only
         → Self-validate report quality for 2-4 weeks

Phase 2: Enable free channel
         → Public summary, observe reception

Phase 3: Enable paid channel
         → Full analysis available for subscribers
```

## 8. Trigger & Scheduling

### Cron Entries

```cron
# Day session report: 13:50 CST Mon-Fri
# resolve_trading_date("day") at 13:50 → today
50 13 * * 1-5  cd ~/subhft && python -m hft_platform.reports.pipeline --session day >> /tmp/report_day.log 2>&1

# Night session report: 05:10 CST Tue-Sat
# resolve_trading_date("night") at 05:10 → YESTERDAY (the date the session opened on)
# e.g. cron fires 2026-03-28 05:10 → reports on 2026-03-27 night session (3/27 15:00 ~ 3/28 05:00)
10 5  * * 2-6  cd ~/subhft && python -m hft_platform.reports.pipeline --session night >> /tmp/report_night.log 2>&1
```

Note: Tue-Sat because Monday night session opens Sunday (no TAIFEX Sunday trading).
If TAIFEX changes holiday schedule, cron days need manual adjustment.

### CLI Interface

```bash
# Normal operation (cron) — date auto-resolved by resolve_trading_date()
python -m hft_platform.reports.pipeline --session day
python -m hft_platform.reports.pipeline --session night

# Manual run with explicit date (overrides auto-resolution)
# Useful for backfilling or re-generating past reports
python -m hft_platform.reports.pipeline --session day --date 2026-03-27
python -m hft_platform.reports.pipeline --session night --date 2026-03-27

# Dry run (generate but don't send)
python -m hft_platform.reports.pipeline --session night --dry-run

# Debug (print rendered messages to stdout)
python -m hft_platform.reports.pipeline --session night --debug
```

When `--date` is provided, it is used directly as the trading date (no auto-resolution).
The pipeline logs the resolved date at startup for auditability.

## 9. Testing Strategy

| Layer | What to Test | How |
|-------|-------------|-----|
| DataCollector | Correct timezone handling, price scaling | Unit test with fixture CH data |
| SignalEngine rules | Each rule independently | Unit test with synthetic FlowBar/LargeTrade |
| ScenarioBuilder | Scenario selection logic | Unit test with known SignalReport inputs |
| ReportRenderer | Message length ≤ 4096, free vs paid tiers | Unit test render output |
| Distributor | Channel routing, tier filtering | Unit test with mock TelegramSender |
| Integration | Full pipeline dry-run | `--dry-run` against real CH data |

Key test cases:
- **resolve_trading_date**: at 05:10 on 3/28 with --session night → must return "2026-03-27"
- **resolve_trading_date**: at 15:30 on 3/27 with --session night → must return "2026-03-27"
- Night session crossing midnight (timezone edge case in CH queries)
- **CH→platform price scale**: DataCollector converts x1,000,000 to x10,000 at boundary
- Empty session (no trades, holiday)
- Extreme values (U/D = 0, all large trades one direction)
- Message truncation (4096 char limit)
- **ReportSender retry**: 429 rate limit → waits Retry-After → retries
- **ReportSender multi-channel**: sends to owner + free + paid with correct tier messages

## 10. Future Extensions

1. **More symbols**: Add stocks (2330) and TXO options — new DataCollector configs, same pipeline
2. **Chart generation**: Add ChartRenderer stage (matplotlib → PNG → Telegram photo)
3. **Confidence scoring**: Backtest rule accuracy, add historical win rates to scenarios
4. **Subscriber management**: Telegram bot commands for subscribe/unsubscribe
5. **Web dashboard**: Host HTML reports with expanded detail, link from Telegram
