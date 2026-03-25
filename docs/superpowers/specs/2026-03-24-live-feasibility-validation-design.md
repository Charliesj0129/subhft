# Live Feasibility Validation Design Spec

## Overview

微台指 (TMF) 小額實盤可行性驗證基礎建設。跳過 Shadow Trading（Shioaji sim 對帳不可靠），直接用真金白銀驗證策略 edge，每日最大虧損硬切 1000 NTD。

**場景**：單一策略 + 微台指 TMF（Shioaji 商品代碼 TMF，1 點 = 10 NTD），Gate D 通過，平衡型風險偏好。

**核心原則**：
- 用真實 fill 數據回答「策略有沒有 edge」
- 所有模組 Shadow/Live 通用，切換只需改 `HFT_ORDER_MODE`
- 全部金額計算用 scaled int，符合 Precision Law
- 新模組不阻塞熱路徑

## Module 1: Intraday PnL Watermark + Hard Cut

### Purpose

日內即時 PnL 追蹤 + 三道防線分級保護，防止單日虧損超過 1000 NTD。

### Relationship to Existing DailyLossLimitValidator

The platform already has `DailyLossLimitValidator` (`risk/validators.py:175`) with:
- Accumulated realized PnL per strategy + platform-wide unrealized PnL
- HALT trigger via `RiskEngine._check_daily_loss_halt()` → StormGuard escalation
- Reset boundary: 05:00 TST (21:00 UTC), aligned with Taiwan futures settlement

**Decision: Extend, don't duplicate.** Module 1 adds watermark/soft-limit/drawdown capabilities to the existing `DailyLossLimitValidator` rather than creating a parallel authority. Specifically:

- `DailyLossLimitValidator` remains the **single loss-limit authority**
- Its existing `_default_max_daily_loss` becomes the hard limit (reconfigure to 1000 NTD equivalent in scaled-int)
- New fields added to DailyLossLimitValidator: `_peak_pnl`, `_soft_limit_active`, `_soft_limit_cooldown_until_ns`
- Reset boundary stays at 05:00 TST (existing, correct for Taiwan futures including night session)
- Soft limit and peak drawdown are new check layers within the same validator's `check()` method

This avoids two authorities disagreeing on when a "day" ends or triggering conflicting HALT/reduce-only.

### Design

觸發源：每筆 FillEvent 後重算 realized PnL（via existing `RiskEngine.notify_fill_pnl()`）；unrealized PnL via existing `RiskEngine.update_unrealized_pnl()`.

**Extended DailyLossLimitValidator fields** (PnL stored in platform-native scaled-int, converted to NTD only for logging/reporting):
- Existing `_accumulated_loss`: realized PnL per strategy (scaled int) — **kept as-is**
- Existing `_unrealized_pnl`: platform-wide unrealized (scaled int) — **kept as-is**
- New `_total_pnl_scaled`: sum of realized + unrealized (scaled int)
- New `_peak_pnl_scaled`: 日內最高 total_pnl (scaled int)
- New `_soft_limit_active`: bool, whether soft limit reduce-only is engaged
- New `_soft_limit_cooldown_until_ns`: earliest ns timestamp to re-evaluate recovery

All internal comparisons use scaled-int. Config thresholds are in NTD, converted to scaled-int once at startup: `threshold_scaled = threshold_ntd * price_scale / point_value` (for TMF: `500 NTD * 10000 / 10 = 500000`).

### Three Lines of Defense

| Line | Threshold | Action | Recovery |
|------|-----------|--------|----------|
| Soft Limit | total_pnl < -500 NTD (50 ticks) | Reduce-only (close only) | Auto: PnL recovers to -300 NTD + 60s cooldown elapsed |
| Peak Drawdown | drawdown > 40% of peak_pnl (only when peak > 200 NTD) | Reduce-only | Auto: drawdown narrows to 20% |
| Hard Limit | total_pnl < -1000 NTD (100 ticks) | HALT, stop for the day | Not recoverable, resets at 05:00 TST (existing reset boundary) |

### Implementation

- **Extend existing**: `src/hft_platform/risk/validators.py` — add fields and logic to `DailyLossLimitValidator`
- No new bus subscriptions needed — existing `RiskEngine.notify_fill_pnl()` and `update_unrealized_pnl()` already feed the validator (called from `ExecutionRouter` on each fill, `router.py:136-141`)
- Soft limit triggers Autonomy reduce-only; hard limit triggers StormGuard HALT (existing `_check_daily_loss_halt` path)
- New Autonomy reason codes required: `"pnl_soft_limit"`, `"pnl_peak_drawdown"` (add to `_ALLOWED_REASON_CODES` in `ops/autonomy.py`). Hard limit uses existing StormGuard HALT path.
- **Config location**: `config/base/strategy_limits.yaml` (existing risk config path loaded by bootstrap at `bootstrap.py:745`), NOT `config/base/risk.yaml` which does not exist
- All state transitions → ClickHouse audit + Prometheus metric + Telegram alert (existing autonomy alert format)

### Oscillation Guard

Soft limit auto-recovery creates oscillation risk if PnL bounces near the boundary (only 20 ticks range between -300 and -500 NTD equivalent). Mitigation:
- **Cooldown timer**: after entering reduce-only from soft limit, must stay in reduce-only for at least 60 seconds (checked via `timebase.now_ns()`) before re-evaluating recovery
- **Config**: `soft_limit_cooldown_s: 60`

### Peak Drawdown Minimum Threshold

40% drawdown of a small peak would trigger on normal noise. Mitigation:
- **Minimum peak threshold**: peak drawdown rule only activates when `_peak_pnl_scaled` exceeds a minimum (config in NTD, converted to scaled-int at startup like other thresholds)
- **Config**: `peak_drawdown_min_peak_ntd: 200` (= 20 ticks for TMF)

### TAIFEX-Specific

- TMF point_value = 10 NTD, read from `symbols.yaml` (already present: `point_value: 10` for TMFD6/TMFF6)
- Reset boundary: 05:00 TST (existing `DailyLossLimitValidator` boundary, correct for Taiwan futures including night session)
- No separate day/night session tracking needed — existing 05:00 TST boundary already spans the full trading day (08:45-13:45 day + 15:00-05:00 night)

## Module 2: Slippage Tracking

### Purpose

Per-fill slippage recording: capture decision-time mid-price, compare with fill price, persist for TCA and scorecard.

### Design

**Data flow**:
1. When StrategyRunner produces OrderIntent, capture `LOBStatsEvent.mid_price_x2 / 2` → write to `OrderIntent.decision_mid` field (new `int` field on dataclass, default 0, zero-cost when unused — no dict allocation, Allocator Law compliant)
2. `decision_mid` propagates naturally: OrderIntent → OrderCommand (wraps intent) → OrderAdapter
3. **Correlation mechanism**: OrderAdapter already maintains `order_id_map` (broker IDs → `order_key` where `order_key = "strategy_id:intent_id"`, `adapter.py:92,99`). A new lightweight `_decision_mid_map: Dict[str, int]` keyed by `order_key` is populated when OrderAdapter processes an OrderCommand (reading `cmd.intent.decision_mid`). When FillEvent arrives, the execution path resolves `order_key` via `order_id_map`, then SlippageTracker reads `_decision_mid_map[order_key]`. Entries are evicted when the order reaches terminal state (`adapter.py:235`, existing cleanup path).
4. Produce `SlippageRecord`

**Why not use FillEvent directly?** FillEvent arrives from broker via `ExecutionRouter` (`router.py:108`) as a normalized fill — it carries `order_id` (broker ID) and `strategy_id` but NOT the original OrderIntent. The `order_id_map` bridge is the only existing correlation path from broker fill → platform order.

**SlippageRecord schema** (all scaled int):

| Field | Type | Description |
|-------|------|-------------|
| order_id | str | Links to original order |
| symbol | str | e.g. "TMFJ6" |
| side | Side | BUY / SELL |
| decision_mid | int | Mid-price at decision time (x10000) |
| fill_price | int | Actual fill price (x10000) |
| slippage_ticks | int | `(fill_price - decision_mid) × side_sign / tick_size_scaled`; positive = adverse |
| slippage_ntd | int | slippage_ticks × point_value (10 NTD for TMF) |
| latency_ns | int | fill_ts - order_ts |
| ts | int | Fill timestamp (ns) |

Note: `tick_size_scaled` for TMF = 10000 (1 index point in x10000 scaling). `slippage_ticks` is the actual tick count, not raw scaled-int difference.

### Implementation

- **New module**: `src/hft_platform/execution/slippage_tracker.py`
- **Contract change**: Add `decision_mid: int = 0` field to `OrderIntent` in `contracts/strategy.py` (single int, `__slots__` compatible, no allocation)
- Subscribes to FillEvent (published on bus by `ExecutionRouter`, `router.py:144-148`), resolves `order_key` via `order_id_map`, reads `decision_mid` from `OrderAdapter._decision_mid_map[order_key]`
- Writes to ClickHouse table `hft.slippage_records` via existing recorder pipeline (put_nowait → batcher → writer)
- `point_value` and `tick_size` from `symbols.yaml`, not hardcoded
- Zero float in computation path (Precision Law compliant)
- New `map_slippage_record` function needed for recorder batcher/writer (similar to existing `map_fill_record`)

## Module 3: Daily PnL Report (Telegram Integration)

### Purpose

End-of-day summary with execution quality metrics, integrated into existing Daily Summary Telegram format.

### Design

Triggered after EOD reconciliation (13:45+ for day session). Aggregates from ClickHouse, pushes via existing Telegram dispatcher.

**Integrated Daily Summary format**:

```
📊 Daily Summary — 2026-03-24

Sessions: futures_day ✅
P&L: +254 NTD (realized) / +0 NTD (unrealized)
Orders: 12 sent / 12 filled / 0 cancelled
Fills: avg slippage -0.8 點/筆, cost -96 NTD
Fees: -46 NTD (手續費) / -20 NTD (交易稅)
Net P&L: +254 NTD (+32 點)

Risk
  Peak PnL: +480 NTD / Max Drawdown: -160 NTD (33%)
  Soft Limit: 0 / Hard Limit: 0
Autonomy: 0 transitions
Reconciliation: ✅ matched

Cumulative (Day 3)
  Net PnL: +520 NTD / Win Rate: 58% / PF: 1.8
  Avg Slippage: -0.7 點/筆

Evidence: outputs/production_rollout/daily/20260324/
```

**Three Telegram message types (clear separation)**:

| Message | Timing | Purpose |
|---------|--------|---------|
| Autonomy Alert | Real-time | State transitions (HALT, reduce-only) |
| Heartbeat | Every 30 min | Passive intraday visibility |
| Daily Summary | Post-close | Full daily report + cumulative validation |

### Implementation

- **New module**: `src/hft_platform/ops/daily_pnl_report.py`
- Extends existing Telegram dispatcher with `daily_pnl` template
- Aggregates from: `hft.fills`, `hft.slippage_records`, `hft.daily_reports`
- Cumulative stats computed from `hft.daily_reports` lookback
- Persists to ClickHouse `hft.daily_reports` table
- Evidence pack: `outputs/production_rollout/daily/YYYYMMDD/`

## Module 4: TCA Attribution Engine

### Purpose

Post-market analysis tool to decompose PnL into alpha, slippage, and fees. Answers: "Is my alpha big enough to cover execution costs?"

### Design

**PnL decomposition per trade**:

```
Gross Alpha  = fill_pnl + slippage_cost   (what you would have earned with perfect execution)
Slippage     = decision_mid vs fill_price  (execution delay + market impact cost)
Fees         = commission + tax            (fixed costs)
Net Alpha    = Gross Alpha - Slippage - Fees  (true edge)
```

**Analysis dimensions**:

| Dimension | Question Answered |
|-----------|------------------|
| Per-trade | Which trades had worst slippage? Any pattern? |
| By time-of-day | When is execution quality best/worst? |
| By direction | BUY vs SELL slippage asymmetry? |
| Trend | Is slippage improving or degrading over days? |

### Implementation

- **CLI tool**: `hft tca report --days 5`
- Offline analysis, not real-time (float permitted per Architecture Rule 11)
- Data sources: `hft.fills`, `hft.slippage_records`, `hft.daily_reports`
- Output: terminal table + CSV to `outputs/tca/YYYYMMDD.csv`
- Key metric: **Net Alpha Retention Rate** = Net Alpha / Gross Alpha (target > 50%)

### Not in scope

- No market impact model (single TMF contract, negligible impact)
- No real-time TCA (post-market sufficient)
- No multi-strategy split (single strategy)

## Module 5: Liquidity Gate

### Purpose

Reject new orders when bid-ask spread is abnormally wide, avoiding high-slippage fills during illiquid windows (open/close auctions, news events).

### Design

New validator in RiskEngine chain, reading `spread_scaled` from latest `LOBStatsEvent`.

**Thresholds**:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `spread_reject_ticks` | 3 | Reject new orders if spread > 3 ticks |
| `spread_warn_ticks` | 2 | Log warning if spread > 2 ticks |
| `cooldown_s` | 5 | Minimum wait after rejection before re-evaluation |
| `gate_start_offset_s` | 60 | Delay gate activation after session open (skip auction noise) |

TMF normal spread = 1 tick (10 NTD). 3-tick spread = 30 NTD cost before trading starts = 3% of daily budget.

### Implementation

- **New validator**: `src/hft_platform/risk/liquidity_gate.py`
- Added as 6th validator in RiskEngine chain
- **LOB wiring change required**: `RiskValidator` base class accepts optional `lob` parameter (`validators.py:16,71`), but `RiskEngine.__init__` (`engine.py:117-124`) currently constructs validators without passing LOB, and `bootstrap.py:864` constructs RiskEngine without LOB reference. Implementation must:
  1. Add `lob_engine` parameter to `RiskEngine.__init__`
  2. Pass `lob_engine` from bootstrap (already available in bootstrap scope as the LOBEngine instance)
  3. `LiquidityGateValidator` receives LOB reference via constructor, reads latest `spread_scaled` from `lob.last_stats`
- **Unit conversion**: config thresholds are in ticks. Comparison: `spread_scaled > spread_reject_ticks * tick_size_scaled` (for TMF: `tick_size_scaled = 10000`, so `spread_reject_ticks=3` → reject when `spread_scaled > 30000`). Conversion done once at startup, cached as `_spread_reject_scaled`.
- **Does NOT block close/reduce-only orders** — only blocks new position opens
- Thresholds from config YAML
- Prometheus: `liquidity_gate_rejections_total`
- ClickHouse: `hft.liquidity_gate_events`

### TAIFEX-Specific

- 08:45 open: gate activates at 08:46 (configurable offset)
- 13:44-13:45 close: gate naturally blocks wide-spread orders
- 15:00 night session open: same offset logic

## Module 6: Feasibility Validation Scorecard

### Purpose

After 5-10 trading days, statistically answer: "Continue, scale up, or stop?"

### Design

**CLI tool**: `hft feasibility report --min-days 5 --strategy <id>`

**Scorecard sections**:

1. **Profitability**: cumulative Net PnL, daily avg, win-day rate, Profit Factor, max single-day loss, max consecutive loss days
2. **Execution Quality**: avg slippage, slippage % of gross, fee % of gross, Net Alpha Retention Rate
3. **Risk Record**: Soft/Hard Limit triggers, max intraday drawdown
4. **Statistical Test**: one-sample t-test on daily Net PnL (H0: mean = 0)

**Pass/Fail criteria (ALL must pass for PASS)**:

| Condition | Threshold | Rationale |
|-----------|-----------|-----------|
| Cumulative Net PnL | > 0 NTD | Basic bar |
| Daily PnL t-test | p < 0.10 | Statistical significance (lenient for small sample) |
| Net Alpha Retention Rate | > 50% | Execution costs can't eat > half of alpha |
| Hard Limit triggers | ≤ 1 | Risk system shouldn't fire frequently |
| Max consecutive loss days | ≤ 3 | Strategy can't stay broken for long |

**Three outcomes**:

| Result | Condition | Recommended Action |
|--------|-----------|-------------------|
| **PASS** | All criteria met | Scale up to MTX (小台) or add contracts |
| **INCONCLUSIVE** | t-test p > 0.10 but PnL > 0 | Continue 5 more days for larger sample |
| **FAIL** | Cumulative loss OR retention < 50% | Stop live, return to research |

### Implementation

- CLI: `hft feasibility report --min-days 5 --strategy <id>`
- Aggregates from `hft.daily_reports`, `hft.slippage_records`, `hft.fills`
- Uses `scipy.stats.ttest_1samp` (offline, float OK per Architecture Rule 11)
- Output: terminal report + JSON to `outputs/feasibility/YYYYMMDD.json`

## Module Dependency & Timeline

```
                    ┌──────────────┐
                    │ 1. PnL水位線  │ ← 上線前必備
                    │ 2. Slippage  │ ← 上線前必備
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │ 3. Daily報表  │ ← 上線同步
                    └──────┬───────┘
                           │
                  ┌────────┼────────┐
                  ▼                 ▼
        ┌─────────────┐   ┌────────────┐
        │ 4. TCA 歸因  │   │ 5. 流動性閘門│ ← 跑幾天後
        └─────────────┘   └────────────┘
                  │
           ┌──────▼───────┐
           │ 6. Scorecard  │ ← 5-10 交易日後
           └──────────────┘
```

**Phasing**:
- **Phase A (上線前)**: Module 1 + 2 — 沒有這兩個不開機
- **Phase B (上線同步)**: Module 3 — 第一天就要有日報
- **Phase C (跑幾天後)**: Module 4 + 5 — 收集數據後加入分析和防護
- **Phase D (5-10 日後)**: Module 6 — 統計決策

## New Config Parameters

```yaml
# config/base/strategy_limits.yaml (existing risk config path, loaded by bootstrap.py:745)
# Add new sections alongside existing validator defaults:

intraday_pnl:
  soft_limit_ntd: 500           # enter reduce-only below this (converted to scaled-int at startup)
  hard_limit_ntd: 1000          # HALT below this (overrides existing max_daily_loss)
  peak_drawdown_pct: 0.40       # reduce-only when drawdown > 40% of peak
  soft_recovery_ntd: 300        # auto-recover when PnL above this
  drawdown_recovery_pct: 0.20   # auto-recover when drawdown narrows to 20%
  soft_limit_cooldown_s: 60     # min seconds in reduce-only before recovery check
  peak_drawdown_min_peak_ntd: 200  # peak drawdown rule only active when peak > this

liquidity_gate:
  spread_reject_ticks: 3        # reject new orders when spread > N ticks
  spread_warn_ticks: 2          # log warning when spread > N ticks
  cooldown_s: 5                 # min wait after rejection
  gate_start_offset_s: 60       # delay after session open (skip auction noise)
```

## New ClickHouse Tables

- `hft.slippage_records` — per-fill slippage data (Module 2)
- `hft.daily_reports` — daily PnL summary rows (Module 3)
- `hft.liquidity_gate_events` — gate rejection log (Module 5)

Each table requires a migration script in `src/hft_platform/migrations/clickhouse/` (per Architecture Governance Rule 5).

## Shared Query Module

Module 3 (Daily Report), Module 4 (TCA), and Module 6 (Scorecard) all aggregate from the same ClickHouse tables. Extract shared aggregation queries into `src/hft_platform/analytics/queries.py` to avoid SQL duplication.

## New CLI Commands

- `hft tca report --days N` — TCA attribution analysis (Module 4)
- `hft feasibility report --min-days N --strategy <id>` — Scorecard (Module 6)

## Telegram Integration

No new message types. Extends existing formats:
- **Daily Summary**: adds Fills, Fees, Risk, Cumulative sections
- **Autonomy Alert**: unchanged, fires on Soft/Hard Limit triggers from Module 1
- **Heartbeat**: unchanged, already carries PnL

## Success Criteria

After 10 trading days with this infrastructure:
1. Can answer "What is my true Net Alpha after all costs?" with data
2. Daily loss never exceeds 1000 NTD (Hard Limit)
3. Have statistical evidence (p-value) for whether strategy has edge
4. Clear PASS/INCONCLUSIVE/FAIL decision on whether to scale up
