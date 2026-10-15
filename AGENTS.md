# AGENTS.md — HFT Platform

`hft_platform`: production high-frequency trading platform for Taiwan markets
(TAIFEX futures/options, TWSE) via Shioaji and Fubon. Money-facing and
latency-sensitive: a hot-path mistake is real financial loss. A governed
research program (`research/`) feeds alphas to production through gates;
research artifacts never enable live trading directly.

This file is the single source of project rules for every agent (Claude Code,
Codex, Copilot). Claude-only harness notes live in `CLAUDE.md`. Everything not
stated here is your judgment: read the source, run the checks, and decide.

## Architecture

```
  Exchange
     |  broker thread  -- SDK callbacks land here, NOT on the loop
     v
  [BrokerFacade] --call_soon_threadsafe--> [bounded raw queue]
                                                   |
  == HOT PATH ==============================       |  no alloc/tick,
                                                   v  no blocking IO
      [Normalizer] --int x10000--> [LOBEngine] --> [FeatureEngine]
                                                          |
                                                          v
                                                   [RingBufferBus]
                                                          |
                                                          v
                                                   [StrategyRunner]
                                                          |  OrderIntent
                                                          v
                                    [RiskEngine] --> [GatewayService]
                                                          |  OrderCommand
                                                          v
                                    [OrderAdapter] --> [BrokerFacade]
                                                          |  FillEvent
                                                          v
                                              [Execution / Positions]
  =========================================================|==========
                                                           |  put_nowait
  -- OFF HOT PATH (never blocks the above) --              v
                                                     [RecorderService]
                                                           |
                                                      [Batcher]
                                                        /      \
                                               ClickHouse       WAL
                                                             (fallback;
                                                        replay idempotent)
```

Contract chain — every stage crosses one of these, nothing else:
`OrderIntent -> RiskDecision -> OrderCommand -> FillEvent -> PositionDelta`.

- Event prices are scaled int **x10000**. Research ClickHouse raw data is
  **x1,000,000** (and option strikes differ again); convert explicitly.
- Broker callbacks run on broker threads and enter the loop only through
  `call_soon_threadsafe`.
- Book depth is variable, 0-5 levels; never assume L5. Filter depth-N features
  on `length(bids_price)` (`.agent/rules/70-research-data.md`).
- Layout: `src/hft_platform/` (modules: `docs/MODULES_REFERENCE.md`),
  `rust_core/` (PyO3 kernels), `config/`, `research/`, `tests/`, `scripts/`,
  `docs/`, `.agent/` (rules, skills, memory). ClickHouse DDL source of truth:
  `src/hft_platform/migrations/clickhouse/`.
- Pinned (bump only with explicit approval): `shioaji==1.5.6`
  (`docs/runbooks/shioaji-version-diff.md`), `prometheus_client<0.25`. The repo
  pin and the production runtime can diverge; verify the running process.

## Commands

| Task | Command |
|---|---|
| Install / dev | `uv sync` / `make dev` |
| Rust build | `make build-rust` |
| Tests | `make test` (unit), `make test-all`, `make test-file FILE=...`, `make test-node NODE=...` |
| Quality | `make lint`, `make format-check`, `make typecheck`, `make discipline`, `make dependency-boundary` |
| Everything quality / full local CI | `make check` / `make ci` |
| Shioaji SDK surface guard | `make shioaji-guard` |
| Agent docs / roadmap guards | `make agent-docs-check`, `make roadmap-delivery-check` |
| Sim run / local stack | `uv run hft run sim` / `make start`, `make stop`, `make logs` |

Fail-closed gates: pre-commit (`ruff`, `ruff-format`, `hft-discipline`), CI
(format, lint, discipline, dependency-boundary, typecheck, coverage), Semgrep,
CodeQL. New discipline IDs use `HFT-{D|A|P|S}{NNN}`.

Gate: 70% coverage (`--cov-fail-under=70`); new code >= 80%, hot path >= 90%.
Pytest runs with `--timeout=30`. `make help` lists everything else.

## Laws (hot path)

Hot path = ingestion, normalizer, LOB, feature engine, event bus, strategy
dispatch, risk, gateway, order/execution.

1. **Allocator** — no heap allocation per tick; preallocate, pool, or ring-buffer (Rust is paused, see below).
2. **Cache** — packed, cache-local data (SoA, arrays, `__slots__`, `msgspec.Struct`); no pointer chasing.
3. **Async** — no blocking IO or >1 ms synchronous compute on the event loop.
4. **Precision Law (Law 4)** — prices and accounting values are scaled int x10000; no hot-path float price math.
5. **Boundary** — Python/Rust crossings of the existing kernels avoid large copies; explicit FFI contracts.

**Rust is paused (2026-10-10).** Do not start new Rust kernels or ports. Existing `rust_core/` stays maintained: it keeps building, its parity tests keep passing, and production still imports it. Performance work happens in Python/numba first.

Reject on sight: hot-path `datetime.now()`/`time.time()` (use
`timebase.now_ns()`), `print()` (use structlog), `requests`, `pandas` in loops,
`Decimal` on the hot path, default-mutable hot-path dataclasses, broad silent exceptions, Rust `unwrap()` reachable
from Python, exceptions as control flow.

Architecture invariants:

- `contracts/` and `events.py` never import runtime services. Broker SDK
  imports live only in `feed_adapter/<broker>/`; platform code uses
  `BrokerProtocol`. SDK import failure is fail-closed (refuse startup).
- New event-loop stages use bounded queues with an explicit overflow policy.
  Recording never blocks the hot path; ClickHouse failure falls back to WAL and
  replay stays idempotent.
- HALT blocks new orders; cancels stay allowed. Exposure maps declare max
  cardinality and eviction (default cap 10,000: evict zero-balance first, else
  reject with `ExposureLimitError`).
- Keep the exchange/source timestamp alongside the local `timebase.now_ns()` stamp. Verify a changed flow with queue depth, latency histograms, and WAL/ClickHouse ingestion.
- Structured data gets structured parsers (msgspec/JSON/YAML), not regex.
- Architecture-affecting changes say where they enter the flow in
  `docs/architecture/pipeline-chains.md` and update the relevant docs and
  boundary tests. Crashes on order/risk/execution paths surface to the
  supervisor and metrics.
- Security: broker APIs keep TLS verification on; production ClickHouse needs
  auth; Prometheus/Grafana/Alertmanager ports stay firewalled; images run
  non-root with pinned versions; scrub structlog fields; never commit data/WAL
  exports or local reports with sensitive content.
- Secrets live in `.env`/env vars only, prefix-isolated (`SHIOAJI_*`,
  `HFT_FUBON_*`, `HFT_*`). Never in code, logs, CLI args, chat, or commits.
- Conventional commits (`feat: fix: perf: refactor: docs: test: chore: ci: alpha:`);
  ruff line length 120, py312; no new `type: ignore`/`noqa` without a written reason.

## Red lines — only on an explicit, per-operation user request

- **Live trading**: `HFT_ORDER_MODE=live`, `uv run hft run live`. Engine
  cutover is always manual. `HFT_ORDER_MODE=sim` does not gate dispatch;
  only `disabled` stops orders.
- **Production host (THESHOW)**: read-only by default: `SELECT`, logs, `ps`,
  rsync of `.wal`. Never `DROP`/`DELETE`, `rm -rf`, `docker system prune`, git
  writes, `up -d`, or `docker compose restart` there. Deploys follow
  `.agent/rules/41-deployment.md` (D1-D10): per-batch authorization, back up
  first, `stop` -> wait 60 s -> `start`, name the pass criteria first. Broker
  cap is 5 sessions. Every prod-host action: read `41-deployment.md` first.
- **Git**: push, merge, rebase, reset, clean, stash drop/clear, `branch -D`,
  `commit --amend`, history edits, and any `.gitignore` edit. This repo
  (`Charliesj0129/subhft`) is PUBLIC; a push is publication.
- **Secrets**: `.env*` and `config/settings.py` are never read into output or committed.
- **Frozen state**: the live registry is frozen to `r47_tmf_v1` (loop_v1 L11);
  dependency pins; frozen research profiles/manifests; golden regeneration
  (`make shioaji-surface-regen` only deliberately, never to make CI pass).
- **Do-NOT-Edit without explicit instruction + strong review** (the
  `permissions.ask` list in `.claude/settings.json` enforces most of this; pre-commit
  config, SDK-surface goldens, and `.gitignore` are covered by this rule only):
  `src/hft_platform/contracts/**`, `events.py`, `core/timebase.py`,
  `core/pricing.py`, `migrations/clickhouse/*.sql` (append only),
  `config/symbols.yaml`, `config/base/brokers/*.yaml`,
  `config/research/profiles/vm_ul6_strict.yaml`, `research/experiments/**`
  (append, never mutate), `tests/golden/**` and SDK-surface goldens,
  `pyproject.toml` pins, `.importlinter`, `scripts/check_discipline.py`,
  pre-commit config, `.gitignore`, `docker-compose.production.yml`,
  `docker-compose.prod.locked.yml`.

## Granted without asking

Reading any file except secrets; running local tests, lint, typecheck,
`make check`/`make ci`, and fixing failures you caused, then rerunning;
creating branches and worktrees; committing on a feature branch. The user's
working tree may hold concurrent work: do not touch dirty files you did not
change, and prefer a new worktree (`~/hft_worktrees/<theme>`) for anything
larger than a small edit. One branch per theme.

## Done means (by blast radius)

| Change | Done when |
|---|---|
| Docs only | every referenced path exists; no invented behavior; `make agent-docs-check` passes when governing docs changed |
| Bug fix | a focused regression test fails before the fix and passes after |
| Hot path / contracts | targeted tests plus scaled-int, monotonic-time, fail-closed, state-transition cases; benchmark if latency-relevant |
| Broker / adapter | protocol conformance tests plus `make shioaji-guard` |
| Test-only | new tests pass, fail when the behavior is broken (say how you checked), `make test-hygiene-check` clean |
| Anything merged | `make check` minimum, `make ci` for merge confidence |

Tests: `feat:`/`fix:` need focused tests named `test_<behavior>_<scenario>`;
every test asserts; no fixed sleeps (<= 50 ms if unavoidable, explained).
Golden tests are regression contracts.

## Reporting

Never claim fixed, passing, or complete without pasted command output, and
list the checks you did not run. A partial result reported as partial beats a
confident guess. Scale this to the task, but never drop `[VALIDATION]` or `[RISK]`:

```
[STATUS]      one line: where the work stands
[DONE]        what changed, per file; only VERIFIED items
[VALIDATION]  | check | command | result |   (exact commands, real results)
[NOT RUN]     checks skipped or impossible here, and why
[RISK]        what could still be wrong, and its blast radius
[NEXT]        the single next action
[VERDICT]     SHIP / HOLD / BLOCKED, with the reason
```

Status words are literal: `VERIFIED`, `WRITTEN` (exists, not run),
`NOT VERIFIED`, `FAILED` (paste the output), `BLOCKED`.

Explain flows, state machines, races, layouts, and before/after fixes with a
fenced pure-ASCII diagram (<= 80 columns) using real identifiers from the
source; for a defect, mark where the path dies (`X`, `(never reached)`).
Do not draw what one sentence already says.

## Where to look (open when the situation arises)

| Situation | Read |
|---|---|
| Locate a module | `docs/MODULES_REFERENCE.md` |
| Editing a module with known traps | `.agent/memory/module_gotchas.md` |
| Any prod-host action | `.agent/rules/41-deployment.md`, `docs/runbooks/deployment.md` |
| Git hygiene, parallel agents, governance edits | `.agent/rules/30-git.md`, `.agent/rules/60-agent-workflow-governance.md` |
| Unattended routines | `.agent/rules/65-unattended-autonomy.md` |
| ClickHouse research data, book depth, export | `.agent/rules/70-research-data.md` |
| Changing data flow | `docs/architecture/pipeline-chains.md` |
| Shioaji SDK behavior | `docs/runbooks/shioaji-version-diff.md` |
| Alpha lifecycle | `docs/runbooks/alpha-development-workflow.md`, `research/README.md` |
| Live-affecting config change | `docs/operations/change-control.md` |
| Known risks, past failed attempts | `.agent/memory/current-risks.md`, `.agent/memory/failed-attempts.md` |
| Task-specific procedure | the matching skill in `.agent/skills/` (the description says when) |

If a referenced path is missing, say so and find the replacement with `rg --files`.

## Multi-agent work

Strong models hold authority over riskier surfaces. The orchestrator owns
tier classification, all git, memory writes, final review, and anything on the
red-lines list. Delegate only when it pays: parallel independent sub-tasks,
large read-only exploration, bulk mechanical edits, or an independent review.
Small serial work is done directly. Procedure, packet template, and venues:
`delegation` skill.

| Tier | Surfaces | Executor | Reviewer |
|---|---|---|---|
| 1 Low | docs, comments, test-only, scratch analysis | Haiku/Sonnet | Sonnet |
| 2 Medium | non-hot-path src, CLI, reports, ops scripts | Sonnet | Sonnet + orchestrator spot-check |
| 3 High | hot path, contracts, pricing/timebase, broker adapters, risk/order/gateway, recorder/WAL, Rust, migrations, alpha governance, Do-NOT-Edit paths | tight packet, or orchestrator directly | orchestrator-class, mandatory |
| X | live/prod ops, history surgery, secrets, pins, frozen registry | orchestrator + explicit user confirmation | user |

Roles: Coding Executor (one packet, listed files only, no git), Reviewer
(read-only, severity-ranked findings, explicit verdict), Test-Writer (`tests/`
only), Documentation (docs only). Executors report in four tables (changed
files / commands run / not verified / blockers); empty sections say `(none)`.
Executor self-reports are context, not evidence: the orchestrator verifies
before anything reaches the user. Two failed delegations on one task: stop and
ask. Unattended routines are read-only; escalating one needs an ADR first.

## Research governance

Research -> Gates A-F -> Canary -> Shadow -> Live. Promotion is gated,
config-driven, reversible, and latency-realistic. Verdicts are faithful
(KILL / NEEDS-MORE-DAYS / INCONCLUSIVE); never relax a pre-registered floor or
gate to improve how a result looks; every PnL claim names its backtest method.
`research/` may use float for offline metrics; live accounting may not.
