# Alpha Development Workflow

**Status:** Canonical workflow for new alpha authors and the `alpha-research` agent team. Last revised 2026-05-06.
**Audience:** Quant researchers proposing new factors; teammates running the autonomous alpha-research loop; reviewers evaluating promotion candidates.
**Scope:** End-to-end lifecycle — hypothesis → scaffold → backtest → Gates A–F → canary → shadow → live registry. This doc is the single entry point; deep-dives are linked per section.

> ⚠️ **LIVE REGISTRY IS FROZEN.** Under `loop_v1` step L11 the live registry is locked to `r47_tmf_v1` for a 30-day stabilization window. New alphas may complete Gate F, canary, and shadow evaluation but **cannot replace the active loop** until the freeze ends. See `docs/loop_v1_stabilization_charter.md` and `.github/workflows/freeze-guard.yml`.

---

## TL;DR — the lifecycle

```
hypothesis ──▶ scaffold ──▶ backtest ──▶ alpha cheap-screen (Slice D)
                                              │
                                              ▼
                Gate A (manifest) ──▶ Gate B (per-alpha pytest) ──▶ Gate C (sub-gates)
                                              │
                                              ▼
                        Gate D (strict promotion eligibility)  ──▶ Gate E (paper trade, 1–7d)
                                              │
                                              ▼
                                Gate F (Rust readiness, optional)
                                              │
                                              ▼
                          canary ──▶ shadow ──▶ ⛔ FROZEN live registry
```

12 lines an author must internalise:

1. Money fields use scaled int (×10000); never `float` on `risk/order/execution`.
2. Latency profile is mandatory — Gate D rejects scorecards without one.
3. Backtest must emit a replay-parity report under strict.
4. `hft alpha cheap-screen` (Slice D) for pre-Gate-A IC/turnover/cost triage; `hft alpha screen` for loose Gate-A–C iteration; `hft alpha validate --profile vm_ul6_strict` for promotion eligibility.
5. Strict profile has 14 blocking sub-gates today (16 once Slice B merges).
6. Synthetic equity is rejected — `require_real_equity: true`.
7. Canary defaults to 1–7 day evaluation window.
8. Live promotion is paused — no merges to `config/loops/<id>.yaml` during the freeze.
9. Recording must run with `HFT_INTENT_RECORDER_ENABLED=1` to feed the replay-parity gate.
10. CI gates: `make discipline-hft`, `make latency-gate-ci`, `make coverage-domain`, `make ci`.
11. Auto-kill ledger writes on `cheap-screen` / Gate-C / Gate-D rejection (Slice D, merged #342 on 2026-05-06).
12. Every promotion artifact must trace to a paper or hypothesis ref under `research/alphas/<id>/papers/`.

---

## 0. Pre-flight constraints

Before writing one line of an alpha, internalise these rules — Gate D and CI will reject violations.

| Rule | Source | Notes |
|------|--------|-------|
| 5 Core Laws (Allocator / Cache / Async / Precision / Boundary) | `.agent/rules/01-core-laws.md` | Hot-path code in alpha modules must obey Laws 1–4. Use scaled int for any value that crosses into `risk/order/execution`. |
| HFT-P004 — no `: float` on money fields in `contracts/order/execution/risk` | `scripts/check_discipline.py` (AST rule) | Enforced by `make discipline-hft`. `float` is permitted in `src/hft_platform/alpha/` and `research/` per Architecture Governance §11. |
| Latency realism | `docs/architecture/latency-baseline-shioaji-sim-vs-system.md` | Use ≥ P95 for promotion, P99 for stress. System latency is ~tens of µs; broker RTT is ~tens of ms. Never assume sub-broker-RTT. |
| Per-method backtest reliability | memory `backtest_method_reliability.md` | All historical PnL claims must specify the backtest method. 14× pessimistic to 577× optimistic biases observed. |
| TAIFEX cost reality | memory `feedback_taifex_fee_structure.md` | Retail trader, no rebates, RT ≈ 3 pt for TXF / 4 NTD for TMF. Edge < 2× spread must use bid/ask execution. |

---

## 1. Hypothesis & literature (T0–T1, Researcher)

1. Search literature for an existing formulation:
   - `make research-search-papers QUERY="order flow imbalance"`
   - `make research-fetch-paper ARXIV=2408.03594`
   - `make research-record-paper PAPER=…`
2. Add to the hypothesis queue: `research/tools/hypothesis_queue.py`.
3. Output: a research note + paper PDF/metadata under `research/alphas/<alpha_id>/papers/`.

The Devil's Advocate (T2) checks the proposal against `.agent/skills/taifex-alpha-kill-criteria/SKILL.md` and the `killed_directions:` blacklist in `.agent/teams/alpha-research/shared-context.template.yaml` before any code is scaffolded.

---

## 2. Scaffold the alpha

```bash
hft alpha scaffold --alpha-id <id> --strategy-type maker
# or:
make research-scaffold ALPHA=<id>
```

Produces `research/alphas/<id>/{impl.py, manifest.yaml, README.md}` from `_templates/`.

**Manifest required fields:**

- `strategy_type`: `maker` | `taker`
- `data_fields`: list of LOB / feature columns the alpha consumes
- `complexity`: `simple` | `composite`
- `paper_refs`: pointers under `papers/`

> **No live registry entry yet.** Loop binding (`config/loops/<loop_id>.yaml`) is the only source of truth for live strategies. Scaffolded alphas live exclusively under `research/alphas/`. See loop_v1 step L10.

---

## 3. Backtest & data-meta stamping

```bash
make research ALPHA=<id> OWNER=<owner> DATA='data/clickhouse/<symbol>/...' [ARGS=…]
make research-stamp-data-meta DATA='...'
make research-validate-data-meta DATA='...'
```

Mandatory artifacts produced:

| Artifact | Required by | Source |
|---|---|---|
| `scorecard.json` | Gate D | `validation.py` |
| `replay_parity_report.json` (`BacktestResult.replay_parity_report`) | Gate D under strict | `replay_parity.py` |
| `latency_profile` block (broker, P50/P95/P99 for place/update/cancel) | Gate D `latency_audit --strict` | `config/research/latency_profiles.yaml` |
| `equity_source ∈ {real, real_no_trade}` | Gate D L6 | `vm_ul6_strict.yaml::require_real_equity` |
| Data-meta sidecar | Gate A / B | `make research-stamp-data-meta` |

> **Replay-parity is mandatory under strict.** Engines that don't emit a `ReplayParityReport` will fail Gate D closed. New backtest engines must integrate `replay_parity.py` — see `docs/runbooks/replay-parity-gate.md` for the deep dive.

> **Money fields:** scaled int (×10000) only — even inside `alpha/`, if the value crosses into `risk/order/execution`.

---

## 4. Loose pre-Gate-C screen vs. cheap pre-Gate-A screen

Two screens with similar names — disambiguate before running.

### 4a. Existing loose `hft alpha screen` (already on main)

```bash
hft alpha screen --alpha-id <id> --data <paths>
```

- Source: `src/hft_platform/cli/_alpha.py:220` (`cmd_alpha_screen`); parser at `_parser.py:576`.
- Runs the full Gate A → B → C chain with **default loose thresholds** (no strict profile).
- **Stamps `screen_only=true` on the scorecard.** `hft alpha promote` refuses any artifact with that flag.
- This is the everyday research-iteration entry point. Use it for daily triage; use `hft alpha validate --profile vm_ul6_strict` only when the artifact must be promotion-eligible.

Quoting the validator parser docstring (`_parser.py:567`):

> *"REQUIRED: strict validation profile (e.g. vm_ul6_strict). Loose runs must use `hft alpha screen` instead — `validate` is promotion-eligible only and refuses non-strict profiles."*

### 4b. Slice D cheap pre-Gate-A screener (`alpha cheap-screen`, merged #342)

```bash
hft alpha cheap-screen <alpha_id> [--ic-min 0.005] [--turnover-max 2.0] [--write-kill]
```

A *narrower* screener that runs **before** Gate A. Source: `src/hft_platform/alpha/screener.py`; CLI at `cli/_alpha.py:972` (`cmd_alpha_cheap_screen`); parser at `_parser.py:611`.

- **Thresholds:** IC ≥ 0.005, turnover < 2.0, cost-floor check, ≤ 60 s budget.
- **Auto-kill:** with `--write-kill`, a `verdict='kill'` outcome appends a `gate='pre_screen'` row to the kill ledger (`audit.alpha_kill_ledger` ClickHouse table + `research/alphas/_kill_ledger.jsonl` fallback) keyed by `kill_id = sha256(alpha_id ":" gate ":" stable_artifact_hash)`. Idempotent (`(alpha_id, kill_id)` dedupe in both sinks).
- **Disambiguation:** `cheap-screen` is **not** a substitute for `screen`. The two run different stages: `cheap-screen` is a 60 s pre-Gate-A signal triage; `screen` is the loose Gate-A–C path that produces a (non-promotion-eligible) scorecard. Use `cheap-screen` first to drop dead signals cheaply, then `screen` for promising ones, then `validate --profile vm_ul6_strict` for promotion candidates.

---

## 5. Gate A — manifest & data-field validation

```bash
hft alpha validate --alpha-id <id> --profile vm_ul6_strict --data <paths>
```

- Source: `src/hft_platform/alpha/_gate_a.py`, `validation.py`.
- Validates `manifest.yaml` schema, declared `data_fields` resolvability, `paper_refs` existence.
- Refuses non-strict profiles (see parser docstring quoted above).

---

## 6. Gate B — per-alpha pytest

- Tests under `research/alphas/<id>/tests/` are auto-discovered.
- Source: `src/hft_platform/alpha/_gate_b.py`.
- Failure here blocks Gate C from running.

---

## 7. Gate C — backtest scorecard + sub-gate evaluation

`hft alpha validate --profile vm_ul6_strict` runs Gate C with the strict profile; `hft alpha screen` runs it loose.

### Sub-gate inventory (verified 2026-05-06)

10 module files under `src/hft_platform/alpha/_sub_gates/` (excluding infra: `__init__.py`, `registry.py`, `common.py`):

| Module | Audience | Origin |
|---|---|---|
| `common.py` (3 sub-gates) — `sharpe_threshold`, `max_drawdown`, `winning_day_pct` | maker + taker | baseline |
| `maker.py` (2 sub-gates) — `fill_quality`, `fill_rate_validation` | maker | baseline |
| `taker.py` (1 sub-gate) — `ic_evaluation` | taker | baseline |
| `min_sample_size.py` | maker | Slice A (#337) |
| `single_day_dominance.py` | maker | Slice A (#337) |
| `loo_day_sensitivity.py` | maker | Slice A (#337) |
| `outlier_trade_removal.py` | maker | Slice A (#337) |
| `day_bootstrap_ci.py` | maker | Slice A (#337) |
| `stationary_block_bootstrap.py` | maker | Slice A (#337) |
| `deflated_sharpe_maker.py` | maker | Slice A (#337) |
| `replay_parity.py` | maker + taker | Slice C (#339) |

### Blocking sub-gates under `vm_ul6_strict` (verified from `config/research/profiles/vm_ul6_strict.yaml::blocking_sub_gates`)

Today: **14 blocking** under strict —

- 6 promoted from advisory: `sharpe_threshold`, `max_drawdown`, `winning_day_pct`, `fill_quality`, `fill_rate_validation`, `ic_evaluation`
- 7 Slice A (merged #337): `min_sample_size`, `single_day_dominance`, `loo_day_sensitivity`, `outlier_trade_removal`, `day_bootstrap_ci`, `stationary_block_bootstrap`, `deflated_sharpe_maker`
- 1 Slice C (merged #339): `replay_parity`

After Slice B (PR #340) merges → **16 blocking**:

- + `inventory_mtm` (residual mark-to-market gate)
- + `cost_uncertainty`

### Profile selection

- `--profile loose` (default for `screen`): all sub-gates run, but only the 6 baseline names are blocking; output is advisory.
- `--profile vm_ul6_strict` (required for promotion): the 14 names above are blocking; failure short-circuits Gate D.

Sources: `_gate_c.py`, `_sub_gates/registry.py`, `config/research/profiles/vm_ul6_strict.yaml`.

---

## 8. Gate D — strict promotion eligibility

```bash
hft alpha promote --alpha-id <id> --profile vm_ul6_strict
```

Source: `src/hft_platform/alpha/promotion.py::_evaluate_gate_d`.

### Quantitative thresholds (copied from `vm_ul6_strict.yaml`)

**Taker:**
- `sharpe_oos_min: 1.5`, `max_drawdown_pct: 10`, `winning_day_pct_min: 58`
- `min_oos_trades: 200`, `min_oos_days: 30`
- `replay_parity_match_pct_min: 95.0`

**Maker:**
- `sharpe_is_min: 1.0`, `sharpe_oos_min: 1.0`
- `pnl_per_fill_min_pts: 2.0` (×0.5 multiplier)
- `edge_to_cost_ratio_min: 1.5`, `adverse_fill_pct_max: 40`
- `winning_day_pct_min: 60`, `max_drawdown_pct: 15`
- `fill_rate_deviation_max: 0.5`
- Slice A small-sample: `min_fills: 300`, `min_days: 60`, `outlier_day_contribution_max_pct: 25`, `loo_day_sign_preserved: true`, `outlier_trade_removal_pct: 5`
- Bootstrap CIs: `bootstrap_ci_lower_bound_min: 0` (n=2000, α=0.05); `block_bootstrap_ci_lower_bound_min: 0` (block=5d, n=1000, α=0.05)
- Deflated Sharpe: `deflated_sharpe_min: 0.5` (n_trials=1)
- `replay_parity_match_pct_min: 95.0`

### Equity provenance guard (L6)

- `require_real_equity: true` — synthetic equity rejected.
- `allowed_equity_sources: [real, real_no_trade]`.

### Strict checks

- **Replay-parity audit** — `BacktestResult.replay_parity_report` must be present; missing report = hard fail.
- **Latency audit** — `python -m hft_platform.alpha.latency_audit --strict` runs against the scorecard. 80 % tolerance violations on P95 for `place_order` / `update_order` / `cancel_order` = hard fail. Source: `latency_audit.py`.
- **Auto-kill (Slice D, merged #342)** — Gate D rejection writes a row with `gate='D'` to the kill ledger via `kill_ledger.append_kill()`. Default-enabled (`HFT_KILL_LEDGER_ENABLED=1`); set to `0` only in unit-test isolation.

---

## 9. Gate E — paper-trade governance

- `hft alpha canary_evaluate / canary_auto_evaluate`
- 1–7 day spans, drift detection, rejection-quality checks.
- Sources: `src/hft_platform/alpha/canary.py`, `paper_trade_batch.py`.

---

## 10. Gate F — Rust readiness (optional, taker-heavy alphas)

- Manifest declaration + parity tests + perf gate.
- Source: `src/hft_platform/alpha/promotion.py::_evaluate_gate_f`.

---

## 11. Canary → Shadow → Live (FROZEN)

- **Canary scheduler:** `src/hft_platform/alpha/canary_scheduler.py`.
- **Shadow runs:** use the locked production compose (`docker-compose.prod.locked.yml`, loop_v1 step L3a).
- **Live promotion is FROZEN.** The 30-day stabilization window locks the live registry to `r47_tmf_v1`. Enforcement points:
  - `docs/loop_v1_stabilization_charter.md` (charter + clock).
  - `.github/workflows/freeze-guard.yml` (CI block on new strategies).
  - `config/loops/` directory contains `r47_tmf_v1.yaml` only — adding another loop file is the trigger guard.
- **Post-freeze procedure:** amend `config/loops/<new_loop_id>.yaml` and follow `docs/runbooks/loop_v1_migration_l7.md`.

---

## 12. Replay-parity verification (Slice C)

1. Set `HFT_INTENT_RECORDER_ENABLED=1` for live / shadow runs that should produce reconstruction evidence.
2. Run `hft run --mode replay` (loop_v1 step L4) over a recorded session. Source: `src/hft_platform/replay/cli_runner.py`.
3. The engine emits an `IntentDiff` stream; `replay_parity.py` compares live vs. replay.
4. ≥ 95 % match required → Gate D blocks closed under strict otherwise.

Deep dive: `docs/runbooks/replay-parity-gate.md`.

---

## 13. Auto-kill ledger (Slice D, merged #342)

- **Schema:** `audit.alpha_kill_ledger` (ClickHouse — migration `src/hft_platform/migrations/clickhouse/20260505_001_create_alpha_kill_ledger.sql`) with `research/alphas/_kill_ledger.jsonl` offline fallback. The JSONL is git-ignored.
- **Gate values:** `pre_screen` (cheap-screen), `A`/`B`/`C`/`D`/`E`/`F`, `cluster`, `manual`.
- **Idempotency:** `kill_id = sha256(alpha_id ":" gate ":" stable_artifact_hash)`. Both sinks dedupe on `(alpha_id, kill_id)`; the same `KillRecord` written twice yields exactly one row in each sink.
- **Default:** `HFT_KILL_LEDGER_ENABLED=1`.
- **CLI surface:** `hft alpha kill <subcommand>` — `cli/_alpha.py:1032`.
- **Auto-trigger:** `cheap-screen` (with `--write-kill`), Gate-C blocking failures, Gate-D rejections each call `kill_ledger.append_kill()` inline. Source: `src/hft_platform/alpha/kill_ledger.py`.

### Cluster (`alpha cluster`, merged #342)

```bash
hft alpha cluster
```

Single-linkage agglomerative clustering on `1 − |ρ|` over recent screener / scorecard outputs at threshold `0.7`. Cluster representatives are kept; non-representatives may be auto-killed with `gate='cluster'`. Source: `src/hft_platform/alpha/cluster.py`; CLI at `cli/_alpha.py:1080`.

### DSL (Slice D, merged #342)

A minimal expression language for declaring screener formulas: parser, compiler, and formula context under `src/hft_platform/alpha/dsl/` (`parser.py`, `compiler.py`, `formula_context.py`). Used by the cheap screener to compose IC / turnover / cost-floor predicates without authoring full Python alpha modules.

---

## 14. CI gates an alpha must clear

| Make target | Purpose | Source |
|---|---|---|
| `make discipline-hft` | HFT-P004 AST rule — no `: float` on money fields in `contracts/order/execution/risk` | `Makefile:106`, `scripts/check_discipline.py` |
| `make latency-gate-ci` | `python -m hft_platform.alpha.latency_audit --strict` over every scorecard | `Makefile:124` |
| `make coverage-domain` | Per-package coverage floors (alpha package included as of #328) | `Makefile:223` |
| `make ci` | format-check + lint + typecheck + dependency-boundary + test-hygiene + coverage | `Makefile:209` |
| `make latency-audit` | Standalone Gate D 80 % tolerance check | `Makefile:276` |
| `freeze-guard.yml` | Blocks new strategies entering the live registry under L11 | `.github/workflows/freeze-guard.yml` |

---

## 15. Cross-references

- **Constitution:** `CLAUDE.md`, `.agent/rules/01-core-laws.md`.
- **Stabilization charter:** `docs/loop_v1_stabilization_charter.md`, `docs/loop_v1_stabilization_log.md`.
- **Deep-dive runbooks:** `docs/runbooks/replay-parity-gate.md`, `docs/runbooks/loop_v1_migration_l7.md`, `docs/runbooks/research-feature-promotion.md`, `docs/runbooks/forced_promotion.md`.
- **Reviewer skill (read-only):** `.agent/skills/validation-gate/`.
- **Agent team:** `.agent/teams/alpha-research/` (roles, hooks, rounds, shared-context template).
- **Project memory:**
  - `backtest_method_reliability.md` — choose the backtest method honestly.
  - `feedback_taifex_fee_structure.md` — retail cost reality.
  - `feedback_no_cross_candidate_kill_shortcuts.md` — every candidate gets its own scorecard.
  - `slice_c_replay_parity_gate.md` — Slice C rationale.
  - `slice_d_alpha_factory_mvp.md` — Slice D rationale (post-merge).

---

## 16. Pending merges — change-log

| PR | Status | Branch | Adds | Sections impacted |
|---|---|---|---|---|
| #342 Slice D | **MERGED 2026-05-06** (commit `a1451835`) | `slice-d/alpha-factory-mvp` | `alpha cheap-screen` (`screener.py`), `alpha cluster` (`cluster.py`), DSL (`dsl/`), kill ledger (`kill_ledger.py`) + auto-kill | §4b, §8, §13 |
| #340 Slice B | OPEN | `slice-b/maker-realism` | `inventory_mtm`, `cost_uncertainty`, strict `latency_audit`, q_hat queue calibration, on-session-end `FORCE_FLAT` | §7 (sub-gate count → 16 blocking, 12 modules), §8 (latency-audit advisory→blocking transition closed) |

When #340 merges, also:

- Verify the two new sub-gate modules exist under `src/hft_platform/alpha/_sub_gates/` (`inventory_mtm.py`, `cost_uncertainty.py`).
- Update the sub-gate inventory table in §7.
- Update the "16 with Slice B" forward-references in TL;DR line 5 and §7.
