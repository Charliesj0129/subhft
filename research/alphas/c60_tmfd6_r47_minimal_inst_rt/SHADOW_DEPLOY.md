# C60 TMFD6 R47-minimal Maker (inst RT) — Shadow Deployment Runbook

```
⚠️ SHADOW DEPLOY — MANUAL USER APPROVAL REQUIRED BEFORE LIVE
- Cost model: ESTIMATED (inst tier, not broker-confirmed)
- If broker confirms RT > 2.5 pt, REOPEN promotion decision
- Shadow target: +1,367 NTD/day at 30% scale (from +4,557 raw at mp=2)
- 6 auto-disable rules in SHADOW_DEPLOY.md
```

**Purpose**: step-by-step runbook for operating C60 under
`HFT_ORDER_SHADOW_MODE=1`. **enabled=false by default** — user must
manually approve `true` AFTER shadow clears release-gate (see RELEASE_GATE.md).

Per `memory/feedback_no_auto_deploy.md`: remote deployment is always manual.

## Pre-flight checks

### 1. Config parity — inspect `config/base/strategies.yaml`

```yaml
- id: "C60_TMFD6_SOLO_MAKER"
  enabled: false                    # MUST remain false until user approval
  module: "hft_platform.strategies.c60_tmfd6_solo_maker"
  class: "C60TmfD6SoloMakerMinimal"
  product_type: "FUT"
  symbols: ["TMFD6"]
  params:
    max_pos: 2                      # CANONICAL per DA T6
    spread_threshold_pts: 5
    inventory_skew_tenths: 2
    qi_skew_threshold: 0.10
    qi_skew_widen_ticks: 1
    enable_qi_layer: true
    shadow_mode: true
    queue_share: 0.05
    variant: "R47-minimal"
```

### 2. Risk limits — inspect `config/base/strategy_limits.yaml`

```yaml
C60_TMFD6_SOLO_MAKER:
  max_position_lots: 2              # Canonical cap per DA T6
  max_order_qty: 1
  daily_loss_hard_stop_ntd: 15000   # ~3x worst observed fresh-sim day
  max_inventory_holding_seconds: 3600
  storm_guard_tier: B
  cycle_timeout_s: 5
  auto_disable:
    regime_persistence_sp_med_min: 1
    regime_persistence_consec_days: 3
    shadow_close_maker_rate_min: 0.80
    shadow_close_maker_trailing_cycles: 200
    shadow_daily_pnl_min_ntd: 1367
    shadow_min_sessions: 5
    shadow_min_sp_med_regime_pts: 2
    rolling_pnl_window_days: 5
    rolling_pnl_min_ntd_per_day: -3000
    rolling_pnl_trigger_count: 2
    shadow_pnl_vs_projection_floor_pct: 10
    walk_forward_k: 5
    walk_forward_min_positive: 3
```

### 3. Environment — ensure shadow mode is active

```bash
export HFT_ORDER_SHADOW_MODE=1
```

Under this flag, `order.adapter` intercepts all `place_order` /
`cancel_order` before broker dispatch; no real orders reach Shioaji.

### 4. Concurrent-strategy check — the following must ALL be disabled

- `C17_TMF_FRONTMONTH_MAKER` (also targets TMF family — double-book risk).
- Any other TMFD6-targeted strategy.
- C33 on TXFD6 is permitted to run concurrently (different instrument).

### 5. Broker-confirmation placeholder (BLOCKING)

C60 is PROMOTE_CONDITIONAL. **Before shadow cohorts 1..5**, the user is
expected to clarify/confirm the inst-tier cost assumption with the broker.
If the user has NOT confirmed:

- Shadow can still proceed (no real orders are placed), BUT the projected
  +4,557 NTD/day metric is ESTIMATED; the shadow PnL observation is a
  *ceiling* on what could be realized post-confirmation.
- If actual broker RT differs materially from 1.5 pt, re-run T5 fresh
  CK-direct at the confirmed RT and re-assess PROMOTE verdict before
  proceeding to live.

### 6. Platform — bring up services

```bash
docker compose up -d clickhouse redis prometheus grafana alertmanager
docker compose up -d hft-engine
docker compose logs -f hft-engine
```

## Launch sequence (user-manual only)

Agents do not execute these:

1. User edits `strategies.yaml` `enabled: true` for the C60 entry (other
   TMF-conflicting strategies remain `enabled: false`).
2. User restarts the engine: `docker compose restart hft-engine`.
3. User confirms C60 is quoting by inspecting Prometheus:
   `hft_strategy_quotes_posted_total{strategy_id="C60_TMFD6_SOLO_MAKER"}`.
4. User watches the shadow session for 1 full trading day (08:45-13:45).

## Metrics to watch (Prometheus)

### Primary (DA-binding)

- **close_maker_rate** (trailing 200-cycle rolling fraction of closes where
  both sides were passive-maker fills). **Must remain >= 0.80**.
  Auto-disable at < 0.80.
- **Daily PnL (shadow)**: sum of per-cycle mark-to-market, shadow only.
  Target >= +1,367 NTD/day (30% haircut vs +4,557 NTD/day projection).

### Secondary

- `hft_strategy_quotes_posted_total{strategy_id="C60_TMFD6_SOLO_MAKER"}`
- `hft_strategy_spread_blocked_total{strategy_id="C60_TMFD6_SOLO_MAKER"}`
- `hft_strategy_position_current{strategy_id="C60_TMFD6_SOLO_MAKER"}`
  (must stay within [-2, +2])
- `hft_strategy_qi_widen_events_total{strategy_id="C60_TMFD6_SOLO_MAKER"}`
  (informational: fraction of events where D4 QI triggered)
- `hft_strategy_gap_events_total{strategy_id="C60_TMFD6_SOLO_MAKER"}`
  (elevated gap-events may indicate pending-slot deadlock — see
  `r47_risk_feedback_no_side` pattern)

### Regime / structural

- **TMFD6 session median spread** (from the recorder's daily summary):
  must remain >= 2 pt. Auto-disable if `< 1 for 3 consec sessions`.
- **Reset rate**: `quotes_posted / stats_count` — informational; not a gate
  in R47-minimal, but drift > 2x vs T5 baseline warrants attention.

## Stop conditions (6 auto-disable rules — per team-lead T8 dispatch)

The strategy **MUST auto-disable** when any of the following trigger.
These are the canonical 6 rules referenced in the SHADOW-DEPLOY BANNER.

### Rule 1 — Rolling PnL floor (DA T6 cond #2)

**5-day rolling mean daily PnL < -3,000 NTD/day for 2 consecutive 5-day
windows.**
Reason: sustained rolling loss indicates regime-endogenous fill-rate
change or misspecified cost model.
Config key: `rolling_pnl_window_days=5`, `rolling_pnl_min_ntd_per_day=-3000`,
`rolling_pnl_trigger_count=2`.

### Rule 2 — Spread regime shift (DA T6 cond #3)

**TMFD6 median spread regime shift > 20% from 2 pt baseline.**
- Compression: median spread <= 1.6 pt for 3 consecutive sessions.
- Expansion: median spread >= 2.4 pt for 3 consecutive sessions.
Either direction invalidates the current operating assumption. The
deep-compression hard floor (sp_med < 1 pt for 3 consec days) is also
caught here as a subset of compression.
Reason: T5 was calibrated on median-2pt regime; a +/-20% structural shift
changes cost-drag math materially.
Config key: `spread_regime_baseline_pt=2`, `spread_regime_shift_pct_max=20`,
`spread_regime_consec_days=3`.

### Rule 3 — Close-maker rate drift (DA T6 cond #1)

**Shadow close-maker-rate < 80% over 200 consecutive cycles.**
Reason: DA decisive framework-validity condition. T5 realized 100.0%;
any drop to < 80% signals that maker-close economic assumption has broken.
Config key: `shadow_close_maker_rate_min=0.80`,
`shadow_close_maker_trailing_cycles=200`.

### Rule 4 — Loss-tail asymmetry

**Shadow daily loss-tail > 2x trailing mean daily PnL for 2 consecutive
days.**
Reason: asymmetric downside vs expected carry indicates adverse-selection
surprise beyond the V-shape recovery envelope.
Config key: `loss_tail_ratio_max=2`, `loss_tail_consec_days=2`.

### Rule 5 — PnL-vs-projection shortfall

**Shadow PnL < 10% of +1,367 NTD/day (i.e., < +137 NTD/day rolling mean)
after 30 days.**
Reason: structural under-performance against the +1,367 shadow-target
(30% scale of +4,557 raw). If shadow cannot clear the haircut floor, the
inst-tier model does not transfer to the current operating environment.
Config key: `shadow_pnl_vs_projection_floor_pct=10`,
`shadow_projection_ntd_per_day=1367`, `shadow_projection_min_days=30`.

### Rule 6 — Walk-forward replication failure (DA T6 cond #4)

**Walk-forward k=5 fails to replicate (fewer than 3/5 positive sessions
in any rolling 5-session block).**
Reason: DA decisive overfitting / instability check. T5 showed 12/20 at
mp=2; any 5-day block with < 3 positive signals regime rotation.
Config key: `walk_forward_k=5`, `walk_forward_min_positive=3`.

### Global catastrophic loss (platform-wide, independent of C60)

`intraday_pnl` global `hard_limit_ntd: 8000` triggers platform-wide
degrade; C60 cancels its outstanding quotes as part of the global degrade.
Also the per-strategy `daily_loss_hard_stop_ntd: 15000` acts as a C60
circuit breaker.

Each trigger writes a log line with `c60_auto_disable_<rule_N>` and sets
the strategy `enabled: false` in the runtime (does NOT edit the YAML;
user must re-investigate before re-enabling).

## Shadow-run evaluation (post-session)

After each shadow session:

1. Extract ClickHouse daily PnL:
   ```sql
   SELECT strategy_id, count(), avg(net_ntd), min(net_ntd), max(net_ntd)
   FROM hft.strategy_pnl_daily
   WHERE strategy_id = 'C60_TMFD6_SOLO_MAKER' AND toDate(ts) = today()
   ```
2. Compute close_maker_rate from fills table:
   ```sql
   SELECT countIf(close_type='MAKER') * 100.0 / count() AS close_maker_pct
   FROM hft.cycles
   WHERE strategy_id = 'C60_TMFD6_SOLO_MAKER' AND toDate(ts) = today()
   ```
3. Populate `RELEASE_GATE.md` Shadow-Only Checklist for the session.
4. User decides whether to continue shadow (another day) or halt.

## Rollback procedure

If shadow reveals an issue:

1. User sets `enabled: false` in `strategies.yaml` (immediate).
2. User restarts engine.
3. User opens a fix PR (research-side if strategy logic; infra-side if
   platform issue).
4. Shadow session count resets to 0; 5-session qualifier restarts.

## Known constraints

### Regime persistence assumption

T5 evidence is on the current compressed-spread TMFD6 regime
(sp_med 2-3 pt, recent 20 days). Jan-Feb 2026 had wide-spread regime
(sp_med 28-68 pt). A revert to wide-spread would materially change
economics — the shadow-kill (`sp_med < 1` for 3 days) catches compression
but does NOT catch expansion. Expansion would actually INCREASE the edge
but at the cost of lower event density; executor should note mp=3 may
become preferred in that regime (per R47 structural properties).

### Inst RT is ESTIMATED

The +4,557 NTD/day projection is at `TMF RT = 1.5 pt` ESTIMATED. If broker
confirms a different RT post-shadow, re-run T5 and reconsider. Shadow itself
costs nothing to the user's P&L (no real orders), so running shadow BEFORE
broker confirmation is safe; however, any decision to flip `enabled: true`
REQUIRES broker confirmation first.

### Rebate is ESTIMATED

The +20K-21K NTD rebate uplift (20 days = ~+1,000 NTD/day) assumes
10 NTD/side MM-designation rebate. If broker confirms no rebate, the baseline
+4,557 NTD/day still stands. If rebate is < 10 NTD/side, uplift shrinks
linearly.

### 20-day CK data depth

TMFD6 inventory in CK as of 2026-04-14 is 30 total days, 20 of which are
current-regime days. Shadow sessions must not conflate shadow days with
historical T5 days — each shadow day is a fresh OOS observation and MUST
be appended to daily ledger for rolling auto-disable thresholds.
