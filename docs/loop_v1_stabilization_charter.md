# Loop_v1 Stabilization Charter

> **Immutable spec.** This charter defines the entry/exit criteria, KPIs, and failure protocols for the loop_v1 30-trading-day stabilization. Edits require a CODEOWNERS-approved PR and an entry in `docs/loop_v1_stabilization_log.md`.

## Purpose

Prove that the single hard production loop (`R47_MAKER_TMF` on `TMFR1`) is stable enough to:

1. Be deployed via the immutable release pack (`docker-compose.prod.locked.yml`).
2. Produce a deterministic replay (live ↔ replay parity ≥ phase threshold).
3. Survive 30 trading days of live execution at single-lot / max-loss-5000-NTD risk profile.
4. Output a canonical per-order explanation row for every order it places.

## Phase Plan

The stabilization clock runs **sequentially**, not in parallel. A phase can only start after the prior phase's exit criteria are met and an entry is added to the log.

| Phase  | Days | Order Mode | Risk Profile                | Daily Replay Match |
|--------|------|------------|-----------------------------|--------------------|
| Sim    | 5    | `sim`      | n/a                         | ≥ 99%              |
| Shadow | 10   | `shadow`   | broker callbacks, no orders | ≥ 95%              |
| Live   | 30   | `live`     | 1 lot, max loss 5000 NTD/d  | ≥ 95% on ≥25/30d   |

Total: 45 trading days (~9 calendar weeks).

### Sim Phase Exit Criteria (5 trading days)

- Zero engine restarts.
- Daily replay `match_pct` ≥ 99% on every session.
- All `OrderIntent → OrderCommand → FillEvent` chains have complete decision traces (L5 verifies).
- No P0 / P1 incidents.

### Shadow Phase Exit Criteria (10 trading days)

- Live broker callbacks resolve through the order/risk path (no real orders dispatched).
- Daily replay `match_pct` ≥ 95% on every session.
- ≤ 1 P3 incident; 0 P0 / P1 / P2 incidents.
- Order explanation assembler produces one row per intent (no incomplete lifecycles).

### Live Phase Exit Criteria (30 trading days)

- ≥ 25/30 days with daily replay `match_pct` ≥ 95%.
- 0 P0 incidents; ≤ 2 P1 incidents.
- Net PnL after broker-confirmed cost recorded daily (no synthetic equity).
- Daily fill rate, drawdown, turnover within design envelope (charter v1: drawdown ≤ 5000 NTD/d, turnover ≥ 1 round-trip/h during regular hours).
- 0 stale-instrument subscriptions (L2 gate).

## KPIs

Tracked daily in `docs/loop_v1_stabilization_log.md`:

| KPI                              | Source                                              | Threshold                |
|----------------------------------|-----------------------------------------------------|--------------------------|
| `match_pct`                      | `outputs/replay/<session>/report.json`              | phase-dependent          |
| Net PnL (broker-confirmed cost)  | `hft.fills` + `hft.order_explanations`              | per-phase risk profile   |
| Drawdown                         | `hft.fills` running PnL                             | ≤ 5000 NTD/day in live   |
| Turnover                         | `hft.order_intents` count                           | ≥ 1 RT/h regular hours   |
| Fill rate                        | `hft.fills` / `hft.order_intents`                   | informational            |
| Live-vs-replay divergence count  | `outputs/replay/<session>/divergence_histogram.json`| ≤ 5% of intents          |
| Incident count by severity       | manual log entry                                    | per-phase exit criteria  |

## Daily Job

The cron / systemd timer runs:

```bash
make daily-replay-diff \
  SESSION=$(date +%F) \
  PHASE=$(cat .stabilization-phase) \
  FIXTURE=$(realpath ~/.local/share/hft-fixtures/wal_$(date +%F | tr - _).tar.gz)
```

Outputs:
- `outputs/replay/<session>/{report.json,timeline.md,divergence_histogram.json}` — L4 artifacts.
- `/var/lib/node_exporter/textfile_collector/hft_replay_match.prom` — Prometheus gauge.
- Append-only entry in `docs/loop_v1_stabilization_log.md`.

Alerts fire from `config/monitoring/alerts/replay_parity_alert.yml` based on `phase` label.

## Freeze Enforcement

During stabilization the loop is **frozen**:

1. **CODEOWNERS** — `config/loops/`, `src/hft_platform/strategies/`, `src/hft_platform/alpha/`, `src/hft_platform/order/` require explicit human approval. No bot approvals.
2. **GitHub environment protection** — `production` deploy environment requires manual approval; `deploy.yml` cannot auto-deploy on `workflow_run`.
3. **Dependabot allowlist** — restricted to security-only updates; major-version bumps blocked.
4. **Freeze-guard CI** — `.github/workflows/freeze-guard.yml` blocks any PR adding new strategies / new loop YAMLs unless labelled `freeze-override: <reason>`.

### Freeze-Allowed PRs

The roadmap-delivery-check (`.github/workflows/ci.yml`) tolerates these PR types during freeze:

- Bug fixes touching frozen surfaces (must include `bugfix:` prefix in title).
- Security CVE patches (Dependabot security branch).
- Infrastructure-only changes (`docker-compose*.yml`, `.github/workflows/`, `Makefile`, `scripts/ops/`, `docs/`).
- Stabilization log entries (`docs/loop_v1_stabilization_log.md`).

Anything else requires `freeze-override: <reason>` label, which only the human operator can apply.

## Failure Protocols

### Sim Phase Failure Protocol

1. Halt sim run; capture last `outputs/replay/<session>/`.
2. Bisect against last green commit.
3. Reset Sim phase clock to day 0; do **not** advance to Shadow.
4. Log the bisect result in `docs/loop_v1_stabilization_log.md` under "incidents".

### Shadow Phase Failure Protocol

1. Halt shadow run; capture explanation rows + replay report.
2. Triage by checking divergence histogram — if first divergence is in `OrderCommand` translation, suspect L8 assembler or order adapter changes.
3. Hold at Shadow day count; do **not** roll back to Sim unless root cause is sim-deterministic.
4. After fix, require 3 consecutive clean Shadow days before resuming the original countdown.

### Live Phase Failure Protocol

1. **Single-day breach (`match_pct` < 95)**: log incident, continue. Track cumulative breach count.
2. **5+ breach days within 30**: halt live, rollback to Shadow phase, reset Live clock.
3. **P0 incident**: kill-switch trips automatically; manual review required before resume.
4. **2+ P1 incidents**: pause live, review with operator, may require Shadow re-bake.

### Daily Job Failure Protocol

If `make daily-replay-diff` fails or produces stale metrics:

1. Check cron / systemd timer status: `systemctl status hft-daily-replay-diff.timer`.
2. Check fixture archive freshness: `ls ~/.local/share/hft-fixtures/wal_*.tar.gz`.
3. Verify ClickHouse `hft.order_intents` populated: `SELECT count() FROM hft.order_intents WHERE toDate(ingest_ts/1e9) = today() - 1`.
4. Re-run manually with `make daily-replay-diff SESSION=<yesterday> FIXTURE=<path>`.

### Pre-Recorder Sessions

Sessions predating intent recorder enablement (`HFT_INTENT_RECORDER_ENABLED=1`, default off pre-Slice C) are classified `pre_recorder` by the L4 eligibility check. They cannot satisfy parity gates and do not count against the stabilization clock until Slice C Task 14 backfill is run.

## Strategy #2 Onboarding

**FORBIDDEN** during stabilization. The freeze-guard CI blocks any PR adding `src/hft_platform/strategies/<not r47_maker>.py` or `config/loops/<not r47_tmf_v1>.yaml`. After Live phase exit (≥25/30 days passing), the freeze can be lifted via a charter-amendment PR signed off by the operator.

## Charter Amendments

This charter is intentionally rigid. To amend:

1. Open PR labelled `charter-amendment`.
2. Include a "why" section in the PR description.
3. Require CODEOWNERS approval.
4. Append rationale to `docs/loop_v1_stabilization_log.md` under a `## Charter Amendments` section.

Effective date for any amendment: PR merge timestamp.

## Rollback Plan

If stabilization fails irrecoverably:

1. Revert `loop-v1/convergence` branch from `main`.
2. Re-enable old strategies registry (`research/strategy_archive/strategies_2026_05.yaml`).
3. Switch `HFT_LOOP` env var off in production.
4. Document the failure mode in `docs/loop_v1_stabilization_log.md` with a `## Final Disposition` section.

The locked compose file (`docker-compose.prod.locked.yml`) and audit-column migrations (L7) remain in place — they are independently valuable and not coupled to loop_v1's success.
