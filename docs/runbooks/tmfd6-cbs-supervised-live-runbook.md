# TMFD6 CBS Supervised Live Runbook

## Scope

This runbook is for supervised live validation of `CBS_TMFD6` only.

Current rollout assumptions:

- Symbol: `TMFD6`
- Strategy: `CBS_TMFD6`
- Max position: `1` lot
- Intraday loss hard limit: `8000 NTD`
- Session policy: same-session flat only, enforced by `SessionGovernor`
- Enabled TMFD6 strategy: `CBS_TMFD6` only

Runtime sources of truth:

- Strategy enablement and params: `config/base/strategies.yaml`
- Risk limits: `config/base/strategy_limits.yaml`
- Session boundaries: `config/base/session_governor.yaml`
- Symbol metadata: `config/symbols.yaml`

## Preflight

Before live startup, verify all of the following:

1. `TMFD6` is the intended front-month contract for the current trading date.
2. `config/base/strategies.yaml` has `CBS_TMFD6.enabled: true`.
3. `config/base/strategies.yaml` has `OPPORTUNISTIC_MM_TMFD6.enabled: false`.
4. `config/base/strategy_limits.yaml` has `CBS_TMFD6.max_position_lots: 1`.
5. `config/base/strategy_limits.yaml` has `intraday_pnl.scope: global` and `hard_limit_ntd: 8000`.
6. `config/base/session_governor.yaml` includes:
   - day `close_only 13:40`, `force_flat 13:44`, `closed 13:45`
   - night `close_only 04:55`, `force_flat 04:59`, `closed 05:00`
7. Broker credentials and CA files are present:
   - `SHIOAJI_API_KEY`
   - `SHIOAJI_SECRET_KEY`
   - `SHIOAJI_ACTIVATE_CA=1`
   - `SHIOAJI_CA_PATH` or `CA_CERT_PATH`
   - `SHIOAJI_CA_PASSWORD` or `CA_PASSWORD`
8. No other process is trading the same futures account.
9. You will actively monitor the full session. Do not leave this unattended.
10. If this is a fresh machine, prime the runtime once so `.runtime/position_checkpoint.json` exists before the actual live session.

Run the generic local checks first:

```bash
uv run python -m hft_platform check
uv run python -m hft_platform golive check --json
```

If `golive check` fails on `position_checkpoint`, `kill_switch`, `wal_backlog`, or `alertmanager_config`, resolve that before continuing.

## Live Environment

Export the minimum required environment for supervised live:

```bash
export HFT_BROKER=shioaji
export HFT_MODE=live
export HFT_ORDER_MODE=live
export HFT_LIVE_CONFIRM=yes-i-know
export HFT_ORDER_SHADOW_MODE=0
export HFT_SESSION_GOVERNOR_ENABLED=1

export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export SHIOAJI_ACTIVATE_CA=1
export SHIOAJI_CA_PATH=./certs/Sinopac.pfx
export SHIOAJI_CA_PASSWORD=...
export HFT_POSITION_CHECKPOINT_PATH=.runtime/position_checkpoint.json
```

Notes:

- `HFT_MODE=live` is required. `HFT_ORDER_MODE=live` with `HFT_MODE=sim` is blocked at bootstrap.
- `HFT_LIVE_CONFIRM=yes-i-know` is mandatory.
- Do not combine shadow mode with live order mode.
- `HFT_SESSION_GOVERNOR_ENABLED=1` is required for session `CLOSE_ONLY` / `FORCE_FLAT` enforcement.
- If you use `CA_CERT_PATH` and `CA_PASSWORD` instead of `SHIOAJI_CA_PATH` and `SHIOAJI_CA_PASSWORD`, the Shioaji client still accepts that.
- `golive check` now follows `HFT_POSITION_CHECKPOINT_PATH` first, then the legacy `HFT_CHECKPOINT_PATH`.
- During active local development, prefer `uv run python -m hft_platform ...` over `uv run hft ...` so the command reflects the current source tree immediately.

## Startup

Start from a clean shell after exporting env vars:

```bash
uv run python -m hft_platform run live
```

Expected startup behavior:

1. CLI summary shows mode `live`.
2. Bootstrap logs `live_mode_confirmed`.
3. Bootstrap logs `LIVE ORDER MODE ACTIVE`.
4. Metrics server starts on the configured Prometheus port.
5. `session_governor_started` appears in logs.

Do not proceed if startup downgrades to `sim`.

## What To Watch

During the session, actively watch for these events in logs:

- `cbs_entry_signal`
- `session_phase_transition`
- `LIVE ORDER MODE ACTIVE`
- any `critical` or `error` lines from `bootstrap`, `risk`, `order`, `execution`, `ops.session_governor`

The expected trade lifecycle is:

1. `cbs_entry_signal`
2. IOC entry submission at touch
3. entry fill callback
4. passive limit exit submission
5. one of:
   - take-profit fill
   - stop/timeout triggers cancel of resting limit, then IOC exit
   - session `FORCE_FLAT` cancels risk-taking and flattens position

If you see repeated `cbs_entry_signal` with no corresponding fill or order state progression, stop and investigate before continuing the rollout.

## Session Boundaries

`SessionGovernor` owns the trading window for `TMFD6`:

- Day open: `08:45`
- Day close-only: `13:40`
- Day force-flat: `13:44`
- Day closed: `13:45`
- Night open: `15:00`
- Night close-only: `04:55`
- Night force-flat: `04:59`
- Night closed: `05:00`

Operational expectations:

- In `OPEN`, new CBS entries are allowed.
- In `CLOSE_ONLY`, no new risk should be added; only exit paths should remain active.
- In `FORCE_FLAT`, all remaining `TMFD6` exposure must be flattened.
- If any position survives past `closed`, treat that as a rollout failure and manually reconcile with the broker immediately.

## Loss Guard And Halt

The current rollout uses global intraday PnL guardrails:

- soft limit: `500 NTD`
- hard limit: `8000 NTD`

Operational rule:

- If the hard limit triggers, no discretion. Stop the strategy for the rest of the session and confirm the account is flat.
- Because the scope is `global`, no other live strategy should be enabled on the same engine during this rollout.

## Manual Intervention

Use this sequence when behavior deviates from expectation:

1. Verify actual broker position and working orders in the Shioaji side first.
2. Trigger kill switch or stop the process.
3. If the engine is still connected but not flattening as expected, use the platform flatten path if available for your environment.
4. If local and broker state diverge, flatten manually at the broker and reconcile before restart.

Fast rollback to non-live mode:

```bash
export HFT_ORDER_MODE=sim
uv run python -m hft_platform run sim
```

If you must stop live immediately, terminate the live process and confirm:

1. no open `TMFD6` position
2. no working `TMFD6` orders
3. local checkpoint and broker position agree

## Post-Session

After each supervised live session:

1. Confirm broker position is flat.
2. Confirm no working `TMFD6` orders remain.
3. Review session logs for:
   - entry count
   - passive exits posted
   - forced exits
   - session phase transitions
   - risk rejects or halts
4. Confirm the `8000 NTD` hard-loss guard did not trigger unexpectedly.
5. Record any broker/local reconciliation discrepancy before the next session.

Do not widen size, frequency assumptions, or risk limits until you have multiple clean supervised sessions.
