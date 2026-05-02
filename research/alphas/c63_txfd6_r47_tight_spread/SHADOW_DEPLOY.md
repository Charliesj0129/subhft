# C63 TXFD6 R47-minimal Tight Spread — Shadow Deployment Runbook

```
⚠️ SHADOW DEPLOY — MANUAL USER APPROVAL REQUIRED BEFORE LIVE
- Cost model: ESTIMATED (inst tier); cost-fragile: break-even RT = 2.83pt
- HARD COST GATE: If broker confirms RT > 2.5pt, DO NOT deploy
- C63 REPLACES C33 on TXFD6 — never co-deploy
- Shadow target: +34,404 NTD/day at 30% scale (from +114,680 raw at sp=3/mp=3)
- 7 auto-disable rules in SHADOW_DEPLOY.md
```

**Purpose**: step-by-step runbook for operating C63 under
`HFT_ORDER_SHADOW_MODE=1`. **enabled=false by default** — user must
manually approve `true` AFTER shadow clears release-gate + HARD COST GATE +
C33 disabled (see RELEASE_GATE.md).

Per `memory/feedback_no_auto_deploy.md`: remote deployment is always manual.

## Pre-flight checks

### 1. HARD COST GATE (BLOCKING)

Before any shadow → live transition, user must have broker-confirmed
TXF RT ≤ 2.5 pt retail-equivalent. If confirmed RT > 2.5 pt:
- ABORT deployment.
- Consider PARK or revert to C33 (which survives retail RT 3 pt).
- Re-run T5 at confirmed RT to verify.

Even during shadow (no real orders), the HARD COST GATE is
informational — used to validate projected economics.

### 2. C33 DISABLED (HARD PREREQUISITE)

Verify `config/base/strategies.yaml`:
```yaml
- id: "C33_TXFD6_SOLO_MAKER"
  enabled: false   # MUST be false before C63 enabled=true
```

Flat any residual C33 position on TXFD6 before enabling C63.

### 3. C63 Config parity

```yaml
- id: "C63_TXFD6_TIGHT_SPREAD_MAKER"
  enabled: false                    # MUST remain false until user approval
  module: "hft_platform.strategies.c63_txfd6_tight_spread_maker"
  class: "C63TxfD6TightSpreadMaker"
  product_type: "FUT"
  symbols: ["TXFD6"]
  params:
    max_pos: 3
    spread_threshold_pts: 3         # SIGNATURE LEVER (C33 is 5)
    inventory_skew_tenths: 2
    shadow_mode: true
    queue_share: 0.05
    variant: "R47-minimal-tight-spread"
```

### 4. Risk limits — `strategy_limits.yaml`

```yaml
C63_TXFD6_TIGHT_SPREAD_MAKER:
  max_position_lots: 3
  max_order_qty: 1
  daily_loss_hard_stop_ntd: 20000
  max_inventory_holding_seconds: 3600
  storm_guard_tier: B
  cycle_timeout_s: 5
  auto_disable:
    hard_cost_gate_retail_rt_max_pt: 2.5
    # ... (7 rules; see config)
```

### 5. Environment

```bash
export HFT_ORDER_SHADOW_MODE=1
```

### 6. Concurrent-strategy check — these must ALL be disabled

- `C33_TXFD6_SOLO_MAKER` (same instrument, same mechanism — DOUBLE-BOOK RISK)
- `C14_TXF_FRONTMONTH_MAKER` (TXFD6 via rotator)
- `C27_VOL_AMPLIFIED_C14` (TXFD6 switched replacement)

### 7. Platform — bring up services

```bash
docker compose up -d clickhouse redis prometheus grafana alertmanager
docker compose up -d hft-engine
docker compose logs -f hft-engine
```

## Launch sequence (user-manual only)

Agents do not execute these:

1. User sets `C33_TXFD6_SOLO_MAKER` `enabled: false`, restarts engine,
   confirms flat via Prometheus.
2. User sets `C63_TXFD6_TIGHT_SPREAD_MAKER` `enabled: true`.
3. User restarts engine: `docker compose restart hft-engine`.
4. User confirms C63 quoting via Prometheus:
   `hft_strategy_quotes_posted_total{strategy_id="C63_TXFD6_TIGHT_SPREAD_MAKER"}`
5. User watches shadow session for 1 full trading day (08:45-13:45).

## Metrics to watch (Prometheus)

### Primary

- **close_maker_rate** (trailing 200-cycle): must remain ≥ 0.80.
- **Daily PnL (shadow)**: target ≥ +34,404 NTD/day (30% haircut).

### Secondary

- `hft_strategy_quotes_posted_total{strategy_id="C63_TXFD6_TIGHT_SPREAD_MAKER"}`
- `hft_strategy_spread_blocked_total{strategy_id="C63_TXFD6_TIGHT_SPREAD_MAKER"}`
- `hft_strategy_position_current{strategy_id="C63_TXFD6_TIGHT_SPREAD_MAKER"}`
  (must stay within [-3, +3])
- `hft_strategy_gap_events_total{strategy_id="C63_TXFD6_TIGHT_SPREAD_MAKER"}`

### Regime / structural

- **TXFD6 session median spread** (daily summary): must stay 3-6 pt
  (baseline 4 pt; ±20% drift triggers Rule 3).
- `hft_strategy_quotes_posted_total` / `hft_strategy_stats_count_total`
  ratio — informational (drift >2x T5 baseline warrants attention).

## Stop conditions (7 auto-disable rules — per team-lead T8 dispatch)

### Rule 1 (HARD, PRE-DEPLOY) — Broker-confirmed cost gate

**If broker-confirmed TXF RT > 2.5 pt retail-equivalent, DO NOT deploy.**
Already established at Gate 1 of RELEASE_GATE. Enforced via
`hard_cost_gate_retail_rt_max_pt: 2.5` in strategy_limits.yaml.

### Rule 2 — Rolling PnL floor

**5-day rolling mean daily PnL < +20,000 NTD/day for 2 consecutive
5-day windows → review.**
Reason: sustained below-threshold rolling PnL indicates edge compression.
Config: `rolling_pnl_window_days=5`, `rolling_pnl_min_ntd_per_day=20000`,
`rolling_pnl_trigger_count=2`.

### Rule 3 — Spread regime drift

**TXFD6 session median spread < 3 pt (compression) OR > 6 pt (expansion)
for 3 consecutive sessions → review.**
- Compression < 3 pt: sp=3 threshold rarely fires; mechanism dormant.
- Expansion > 6 pt: regime different from baseline; re-evaluate edge.
Config: `spread_regime_baseline_pt=4`, `spread_regime_drift_pct_max=20`,
`spread_regime_floor_pt=3`, `spread_regime_ceiling_pt=6`,
`spread_regime_consec_days=3`.

### Rule 4 — Close-maker rate drift

**Shadow close-maker-rate < 80% over 200 consecutive cycles.**
Reason: framework-validity condition. T5 realized 100.0%.
Config: `shadow_close_maker_rate_min=0.80`,
`shadow_close_maker_trailing_cycles=200`.

### Rule 5 — Loss-tail asymmetry

**Shadow daily loss-tail > 2× trailing mean daily PnL for 2 consecutive
days.**
Reason: adverse-selection surprise beyond V-shape recovery envelope.
Config: `loss_tail_ratio_max=2`, `loss_tail_consec_days=2`.

### Rule 6 — PnL-vs-projection shortfall

**Shadow PnL < 20% of +34,404 NTD/day (i.e., < +6,881 NTD/day rolling
mean) after 30 days.**
Reason: structural under-performance against shadow target.
Config: `shadow_pnl_vs_projection_floor_pct=20`,
`shadow_projection_ntd_per_day=34404`, `shadow_projection_min_days=30`.

### Rule 7 — Walk-forward replication failure

**Walk-forward k=5 fails to replicate (< 3/5 positive sessions in any
rolling 5-session block).**
Reason: T6 decisive overfitting / instability check. T5 showed 12/20
positive at sp=3/mp=3; any 5-day block with < 3 positive signals
regime rotation.
Config: `walk_forward_k=5`, `walk_forward_min_positive=3`.

### Global catastrophic loss

`intraday_pnl` global `hard_limit_ntd: 8000` triggers platform-wide
degrade. Per-strategy `daily_loss_hard_stop_ntd: 20000` acts as C63
circuit breaker.

Each trigger writes a log line `c63_auto_disable_<rule_N>` and sets
C63 `enabled: false` at runtime (not in YAML; user must investigate).

## Shadow-run evaluation (post-session)

After each shadow session:

1. Daily PnL query:
   ```sql
   SELECT strategy_id, count(), avg(net_ntd), min(net_ntd), max(net_ntd)
   FROM hft.strategy_pnl_daily
   WHERE strategy_id = 'C63_TXFD6_TIGHT_SPREAD_MAKER' AND toDate(ts) = today()
   ```
2. Close-maker rate:
   ```sql
   SELECT countIf(close_type='MAKER') * 100.0 / count() AS close_maker_pct
   FROM hft.cycles
   WHERE strategy_id = 'C63_TXFD6_TIGHT_SPREAD_MAKER' AND toDate(ts) = today()
   ```
3. TXFD6 session median spread:
   ```sql
   SELECT quantile(0.5)(
     (asks_price[1] - bids_price[1]) / toFloat64(1000000)
   ) AS median_sp_pts
   FROM hft.market_data
   WHERE symbol='TXFD6' AND type='BidAsk' AND toDate(fromUnixTimestamp64Nano(exch_ts)) = today()
   ```
4. Populate `RELEASE_GATE.md` Shadow-Only Checklist.
5. User decides: continue or halt.

## Rollback procedure

1. User sets `C63_TXFD6_TIGHT_SPREAD_MAKER` `enabled: false` immediately.
2. User optionally re-enables `C33_TXFD6_SOLO_MAKER` (known-safe fallback).
3. User restarts engine.
4. Shadow session count resets to 0; 5-session qualifier restarts.

## Known constraints

### Cost-fragility

C63 has the narrowest RT safety margin of any PROMOTE to date:
- inst RT 1.5 pt: +114,680 NTD/day
- retail RT 3 pt: -14,447 NTD/day (SIGN FLIP)
- break-even: ~2.83 pt RT

If the institutional cost assumption fails (RT > 2.83 pt), C63 is
loss-making. HARD cost gate at 2.5 pt builds in a 13% safety cushion.

### Regime stationarity

T5 evidence is on 20 most-recent TXFD6 days (2026-02-23..04-14; spread
baseline 4 pt). Q2 (session-median-spread quartile) concentrates 70% of PnL.
Any material spread regime change materially impacts expected edge.

### Rebate is negligible (NOT a lever)

TXF rebate 0.1 pt/RT is dimensionally dead as a mechanism (C64 SELF_KILL);
on C63 fills (8,608 trips/20 days), rebate uplift is ~+2,000 NTD/day. Not
load-bearing for the PROMOTE; confirm broker non-rebate doesn't change
verdict.
