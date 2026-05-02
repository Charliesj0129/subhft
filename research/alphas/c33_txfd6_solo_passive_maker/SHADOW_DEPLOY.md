# C33 TXFD6 Solo Passive Maker — Shadow Deployment Runbook

**Purpose**: step-by-step runbook for operating C33 under
`HFT_ORDER_SHADOW_MODE=1`. **enabled=false by default** — user must
manually approve `true` (see RELEASE_GATE.md).

Per `memory/feedback_no_auto_deploy.md`: remote deployment is always manual.

## Pre-flight checks

1. **Config parity** — inspect `config/base/strategies.yaml`:
   ```yaml
   - id: "C33_TXFD6_SOLO_MAKER"
     enabled: false            # MUST remain false until user approval
     module: "hft_platform.strategies.c33_txfd6_solo_maker"
     class: "C33TxfD6SoloMaker"
     product_type: "FUT"
     symbols: ["TXFD6"]
     params:
       max_pos: 3
       spread_threshold_pts: 5
       inventory_skew_tenths: 2
       shadow_mode: true
       queue_share: 0.05       # informational
       variant: "R47-minimal"
   ```

2. **Risk limits** — inspect `config/base/strategy_limits.yaml`:
   ```yaml
   C33_TXFD6_SOLO_MAKER:
     max_position_lots: 3
     max_order_qty: 1
     daily_loss_hard_stop_ntd: 150000
     cycle_timeout_s: 5
     auto_disable:
       regime_persistence_sp_med_min: 3
       regime_persistence_consec_days: 3
       shadow_close_maker_rate_min: 0.80
       shadow_close_maker_trailing_cycles: 200
   ```

3. **Environment** — ensure shadow mode is active:
   ```bash
   export HFT_ORDER_SHADOW_MODE=1
   ```
   Under this flag, `order.adapter` intercepts all `place_order` /
   `cancel_order` before broker dispatch; no real orders reach Shioaji.

4. **Concurrent-strategy check** — the following must ALL be disabled:
   - `C14_TXF_FRONTMONTH_MAKER` (also TXFD6 — double-book risk)
   - `C27_VOL_AMPLIFIED_C14` (TXFD6 switched replacement for C14)
   C33 and these strategies MUST NOT run simultaneously.

5. **Platform** — bring up services:
   ```bash
   docker compose up -d clickhouse redis prometheus grafana alertmanager
   docker compose up -d hft-engine
   docker compose logs -f hft-engine
   ```

## Launch sequence (user-manual only)

This is the step the user manually performs. Agents do not execute these:

1. User edits `strategies.yaml` `enabled: true` for the C33 entry (other
   TXF-conflicting strategies remain `enabled: false`).
2. User restarts the engine: `docker compose restart hft-engine`.
3. User confirms C33 is quoting by inspecting Prometheus:
   `hft_strategy_quotes_posted_total{strategy_id="C33_TXFD6_SOLO_MAKER"}`
4. User watches the shadow session for 1 full trading day (08:45–13:45).

## Metrics to watch (Prometheus)

### Primary (DA-binding)

- **close_maker_rate** (trailing 200-cycle rolling fraction of closes
  where both sides were passive-maker fills). **Must remain ≥ 0.80**.
  Auto-disable at < 0.80.
- **Daily PnL (shadow)**: sum of per-cycle mark-to-market, shadow only.
  Target ≥ +7,000 NTD/day (30% haircut floor).

### Secondary

- `hft_strategy_quotes_posted_total{strategy_id="C33_TXFD6_SOLO_MAKER"}`
- `hft_strategy_spread_blocked_total{strategy_id="C33_TXFD6_SOLO_MAKER"}`
- `hft_strategy_position_current{strategy_id="C33_TXFD6_SOLO_MAKER"}`
  (must stay within [-3, +3])
- `hft_strategy_gap_events_total{strategy_id="C33_TXFD6_SOLO_MAKER"}`
  (elevated gap-events may indicate pending-slot deadlock — see
  `r47_risk_feedback_no_side` pattern)

### Regime / structural

- **TXFD6 session median spread** (from the recorder's daily summary):
  must remain ≥ 3 pt. Auto-disable if < 3 for 3 consecutive sessions.
- **Reset rate**: `quotes_posted` / `stats_count` — informational; not a
  gate in R47-minimal, but drift > 2× vs T5 baseline warrants attention.

## Stop conditions (auto-disable)

The strategy **MUST auto-disable** when any of the following trigger:

1. **Regime-persistence**: TXFD6 `sp_med < 3` for 3 consecutive sessions.
2. **close_maker_rate drift**: trailing 200-cycle close_maker_rate < 0.80
   (DA decisive framework validity condition).
3. **Daily PnL hard stop**: cumulative daily shadow PnL < −150,000 NTD.
4. **Catastrophic loss** (global): intraday_pnl global
   `hard_limit_ntd: 8000` triggers platform-wide degrade.

Each trigger writes a log line with `c33_auto_disable_<reason>` and sets
the strategy `enabled: false` in the runtime (does NOT edit the YAML;
user must re-investigate before re-enabling).

## Shadow-run evaluation (post-session)

After each shadow session:

1. Extract ClickHouse query for daily PnL:
   ```sql
   SELECT strategy_id, count(), avg(net_ntd), min(net_ntd), max(net_ntd)
   FROM hft.strategy_pnl_daily
   WHERE strategy_id = 'C33_TXFD6_SOLO_MAKER' AND toDate(ts) = today()
   ```
2. Compute close_maker_rate from fills table:
   ```sql
   SELECT countIf(close_type='MAKER') * 100.0 / count() AS close_maker_pct
   FROM hft.cycles
   WHERE strategy_id = 'C33_TXFD6_SOLO_MAKER' AND toDate(ts) = today()
   ```
3. Populate `RELEASE_GATE.md` checklist for the session.
4. User decides whether to continue shadow (another day) or halt.

## Rollback procedure

If shadow reveals an issue:

1. User sets `enabled: false` in `strategies.yaml` (immediate).
2. User restarts engine.
3. User opens a fix PR (research-side if strategy logic, infra-side if
   platform issue).
4. Shadow session count resets to 0; 5-session qualifier must restart.

## Known constraints

- **Single-day CK data depth**: TXFD6 inventory in CK as of 2026-04-14 is
  ~15 current-regime days. Shadow session must not conflate shadow days
  with historical T5 days — each shadow day is a fresh OOS observation.
- **Regime persistence assumption**: T5 evidence is on the current
  wide-spread TXFD6 regime (sp_med 4.3 pt). TMFD6 trajectory Mar→Apr
  compressed from 28 pt to 2 pt; TXFD6 *could* follow. The shadow-kill
  (sp_med < 3 for 3 days) catches this.
