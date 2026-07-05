# CLAUDE.md — HFT Platform

## Project Purpose

`hft_platform`: production high-frequency trading platform for Taiwan markets
(TAIFEX futures/options, TWSE) via Shioaji and Fubon brokers. Money-facing and
latency-sensitive: hot-path mistakes cause real financial loss. A separate
governed research program (`research/`) feeds alphas into production through
gates; research artifacts NEVER directly enable live trading.

## Tech Stack

- Python 3.12 (`uv`-managed), Rust/PyO3 hot-path kernels (`rust_core/`, maturin)
- Data: ClickHouse (durable store + WAL fallback), Redis (live state)
- Obs: Prometheus, Grafana, Alertmanager, Telegram bot
- Key libs: msgspec, structlog, orjson, numpy, hftbacktest, numba
- Pinned (do NOT bump without explicit approval): `shioaji==1.3.3`
  (1.5.3 = full Rust `_core` rewrite; migration in progress, see
  `docs/runbooks/shioaji-version-diff.md`), `prometheus_client<0.25`
  (0.25 corrupts MutexValue on /metrics)

## Architecture Overview

Exchange -> BrokerFacade -> Normalizer -> LOBEngine -> FeatureEngine ->
RingBufferBus -> StrategyRunner -> RiskEngine/GatewayService -> OrderAdapter ->
BrokerFacade -> Execution/Positions -> Recorder WAL/ClickHouse.

Core contracts: `OrderIntent -> RiskDecision -> OrderCommand -> FillEvent ->
PositionDelta`. Canonical event prices are scaled int x10000. Broker callbacks
run on broker threads and cross into the event loop ONLY via
`call_soon_threadsafe`. Recording never blocks the hot path (bounded queues,
explicit overflow policy). Full trace: `docs/architecture/pipeline-chains.md`.

## Retrieval First (mandatory reads before claims or edits)

1. `docs/MODULES_REFERENCE.md`
2. `.agent/rules/00-index.md`
3. `.agent/skills/00-index.md`
4. `.agent/memory/module_gotchas.md` when touching a listed module

Then open only task-relevant rules/skills/source. Missing referenced path:
report it and locate the canonical replacement with `rg --files`.

## Important Directories

| Path | What |
|---|---|
| `src/hft_platform/` | Platform code (see `docs/MODULES_REFERENCE.md`) |
| `rust_core/` | PyO3 hot-path kernels |
| `config/` | Runtime + research YAML; `config/base/brokers/` per-broker |
| `research/` | Alpha research factory (excluded from ruff/pytest gates) |
| `tests/` | unit / integration / spec / golden / benchmark |
| `.agent/` | rules, skills, memory (agent knowledge base) |
| `docs/` | architecture, runbooks, charters |
| `scripts/` | ops/CI/guard scripts (incl. `check_discipline.py`) |
| `src/hft_platform/migrations/clickhouse/` | ClickHouse DDL (schema source of truth) |

## Non-Negotiable Laws (hot path)

Hot path = ingestion, normalizer, LOB, feature engine, event bus, strategy
dispatch, risk, gateway, order/execution.

1. Allocator: no heap allocation per tick — preallocate, pool, ring-buffer, or Rust.
2. Cache: packed/cache-local data; no pointer chasing.
3. Async: no blocking IO or >1 ms sync compute on the event loop.
4. Precision: prices/accounting = scaled int x10000; no hot-path float price math.
   (Research ClickHouse raw scale is x1,000,000 — conversions must be explicit.)
5. Boundary: Python/Rust crossings avoid large copies; explicit FFI contracts.

Reject on sight: hot-path `datetime.now()`/`time.time()` (use
`timebase.now_ns()`), `print()` (use structlog), `requests`, `pandas` in loops,
broad silent exceptions, Rust `unwrap()` reachable from Python.

## Alpha Governance

Research -> Gates A/B/C/D/E/F -> Canary -> Shadow -> Live. Live registry is
FROZEN under loop_v1 L11, locked to `r47_tmf_v1`. Promotion is gated,
config-driven, reversible, latency-realistic. Canonical refs:
`docs/runbooks/alpha-development-workflow.md`, `research/README.md`,
`docs/loop_v1_stabilization_charter.md`,
`config/research/profiles/vm_ul6_strict.yaml`.

## Build / Test / Lint / Typecheck

| Task | Command |
|---|---|
| Install | `uv sync` (dev: `make dev`) |
| Rust build | `make build-rust` |
| Unit tests | `make test` |
| All tests | `make test-all` |
| One file / node (no coverage gate) | `make test-file FILE=...` / `make test-node NODE=...` |
| Lint / format check | `make lint` / `make format-check` |
| Typecheck | `make typecheck` |
| Discipline (AST gates) | `make discipline` |
| Import boundaries | `make dependency-boundary` |
| Everything quality | `make check` |
| Full local CI | `make ci` |
| Shioaji SDK surface guard | `make shioaji-guard` |
| Run sim | `uv run hft run sim` |
| Docker | `make start` / `make stop` / `make logs` |

Coverage: repo gate is 70% (`--cov-fail-under=70`); targets for NEW code are
>=80% line, hot path >=90%. Pytest runs with `--timeout=30`.

## Coding Conventions

- Type hints everywhere; mypy clean; ruff line length 120, target py312.
- Conventional commits: `feat: fix: perf: refactor: docs: test: chore: ci: alpha:`.
- No new `type: ignore` / `noqa` without a specific written justification.
- msgspec.Struct / `__slots__` / NamedTuple / packed arrays for hot data.
- Broker SDK imports ONLY inside `feed_adapter/<broker>/`. Platform code uses
  `BrokerProtocol`. SDK import failure is fail-closed (refuse startup).
- `contracts/` and `events.py` never import runtime services.
- Structured data gets structured parsers (msgspec/JSON/YAML libs), not regex.
- Secrets live in `.env`/env vars only; prefixes isolated (`SHIOAJI_*`,
  `HFT_FUBON_*`, `HFT_*`). Never in code, logs, CLI args, or commits.

## Testing Conventions

- `feat:`/`fix:` require focused tests; names describe behavior:
  `test_<behavior>_<scenario>`. Every test asserts something (gated by
  `make test-hygiene-check`).
- HFT-specific coverage: scaled ints, monotonic time, fail-closed behavior,
  state transitions, one-sided books, zero prices, edge books.
- No fixed sleeps; prefer events/polling (<=50 ms if unavoidable, explained).
- Golden tests (`tests/golden/`, shioaji surface golden) are regression
  contracts: regenerate only deliberately (`make shioaji-surface-regen`) with
  justification, never to "make CI pass".

## Risk Areas (highest-consequence surfaces)

1. Hot-path modules (see law list) — latency/money loss.
2. `contracts/`, `events.py`, `core/pricing.py`, `core/timebase.py` — every
   module depends on these; silent semantic drift is catastrophic.
3. Broker adapters (`feed_adapter/shioaji/`, `feed_adapter/fubon/`) — thread
   handoff, reconnect FSMs, SDK version drift (1.5.3 Rust rewrite).
4. Alpha governance state — live registry FROZEN under loop_v1 L11
   (`r47_tmf_v1`); promotion is gated/config-driven/reversible.
5. Recorder/WAL/ClickHouse — durability path; replay must stay idempotent.
6. Git state — local-only branches with no remote backup exist (2026-07-06:
   25 commits across 3 branches); treat unpushed commits as irreplaceable and
   re-verify exposure at session start (`.agent/memory/current-risks.md`).
7. Production host ops — engine restarts have known failure modes (session
   races, boot-latch); follow runbooks and `.agent/memory/failed-attempts.md`,
   never improvise restarts.

## Do NOT Edit Casually (require explicit instruction + strong review)

- `src/hft_platform/contracts/**`, `src/hft_platform/events.py`
- `src/hft_platform/core/timebase.py`, `src/hft_platform/core/pricing.py`
- `src/hft_platform/migrations/clickhouse/*.sql` (append new migrations; never
  rewrite applied ones)
- `config/symbols.yaml` (operator-regenerated; pool-mode engines never
  auto-rebuild it), `config/base/brokers/*.yaml`
- `config/research/profiles/vm_ul6_strict.yaml` and any frozen research
  profile/registry/manifest
- `research/experiments/**` (immutable evidence artifacts — append, don't mutate)
- `tests/golden/**` and SDK-surface goldens
- Pinned versions in `pyproject.toml`
- `.importlinter`, `scripts/check_discipline.py`, pre-commit config
  (enforcement infrastructure)
- `docker-compose.production.yml`, `docker-compose.prod.locked.yml`
- `config/settings.py` and `.env*` (never commit; never read secrets into output)

## Agent Rules

- Scope control: change only what was asked; preserve dirty user work — some
  working-tree changes belong to concurrent user work.
- No live trading, production-impacting, destructive filesystem, or destructive
  git operations without explicit request. Live-engine cutover is always manual.
- Role/authority boundaries and handoff format: see `AGENTS.md`.
- Read the matching `.agent/skills/*/SKILL.md` before hot-path, alpha, broker,
  config, Rust, ops, or testing work.

## Validation Requirements (match blast radius)

- Docs-only: verify every referenced path exists; review diff.
- Bug fix: focused regression test that fails before / passes after.
- Hot path / shared contract: targeted tests PLUS scaled-int, monotonic-time,
  fail-closed, state-transition checks; benchmark if latency-relevant.
- Broker/adapter: protocol conformance tests + `make shioaji-guard`.
- Anything merged: `make check` minimum; `make ci` for merge-level confidence.

## Honest Progress Reporting

- Never claim fixed/passing/complete without pasted command evidence.
- Report exact commands run AND explicitly list checks NOT run.
- Distinguish "code written" from "verified working".
- Failing tests: report the failure output verbatim; do not hedge or soften.
- Blocked: state the blocker and stop; a partial result reported as partial
  beats a complete-sounding guess.
- Research: verdicts are faithful (KILL / NEEDS-MORE-DAYS / INCONCLUSIVE);
  never relax pre-registered floors or gates to improve an outcome's look.
