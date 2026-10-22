# Contributing to HFT Platform

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Docker & Docker Compose (for ClickHouse, Redis, monitoring stack)
- Rust toolchain (optional, for `rust_core` development)

## Local Setup

```bash
# 1. Clone and install
git clone <repo-url> && cd hft_platform
uv sync                    # or: pip install -e .

# 2. Configure environment
cp .env.example .env       # edit with your credentials
# Never commit .env — it is gitignored

# 3. Start infrastructure
docker compose up -d clickhouse redis

# 4. Build Rust extensions (optional)
uv run maturin develop --manifest-path rust_core/Cargo.toml

# 5. Run in sim mode
uv run hft run sim
```

## Development Workflow

### Before You Code

1. Read `AGENTS.md` for the architecture overview, the five hot-path laws, and the red lines
2. Check `docs/guides/getting-started.md` for detailed onboarding
3. Know the five laws (no allocation per tick, no float for prices, no blocking the loop, ...); they live in `AGENTS.md` only

### Making Changes

```bash
# Run checks before committing
make lint          # ruff check src/ tests/
make typecheck     # mypy
make test          # pytest (unit tests)
make ci            # all of the above
```

### Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(strategy): add Hawkes-process MM strategy
fix(normalizer): handle one-sided LOB snapshots
perf(lob): migrate compute_stats to Rust
test(risk): add StormGuard state transition tests
```

### Testing Requirements

- New code must have >= 80% line coverage
- Hot-path code (`normalizer`, `lob_engine`, `risk`) must have >= 90% coverage
- Test naming: `test_<behavior>_<scenario>` (e.g., `test_rejects_order_when_halt`)
- Every test must contain at least one `assert`

### Code Quality Rules

- **No `float` for prices** — use scaled integers (x10000)
- **No `print()`** — use `structlog`
- **No `datetime.now()`** — use `timebase.now_ns()`
- **No blocking IO on hot path** — use async or thread pool
- **`__slots__` on hot-path dataclasses**
- **Files < 800 lines** — extract if growing larger

## Architecture

See `docs/architecture/current-architecture.md` for the canonical architecture reference.

### Runtime Pipeline

```
Exchange -> BrokerFacade -> Normalizer -> LOBEngine -> FeatureEngine
  -> RingBufferBus -> StrategyRunner -> RiskEngine -> OrderAdapter -> BrokerFacade
```

### Key Directories

| Directory | Purpose |
|-----------|---------|
| `src/hft_platform/` | Core platform code |
| `rust_core/` | Rust extensions (PyO3) |
| `config/` | YAML configuration |
| `tests/` | pytest test suites |
| `research/` | Alpha research (offline only) |
| `docs/` | Documentation (start at `docs/README.md`) |
| `scripts/` | Ops, CI-gate and one-off tooling (catalog: `scripts/README.md`) |
| `.agent/`, `.claude/` | Agent rules, skills, memory and Claude Code hooks/settings |
| `.github/` | Workflows, CODEOWNERS, issue forms and the PR template |
| `outputs/` | Tracked research evidence referenced by `research/factory.py` and tests; runtime report output also lands here (see `docs/outputs_and_artifacts.md`) |
| `ops.sh`, `ops/` | Host setup/tuning script and systemd/health-check helpers used on the production host |
| `certs/` | Mount point for CA certificates (`CA_CERT_DIR`, mounted read-only in compose); contents are gitignored |
| `.ci/` | Coverage and large-file baseline data files (currently not read by any workflow or script; verify before removing) |
| `ROADMAP.md` | Long-term operations roadmap and gate milestones |

Do not move top-level paths without updating every reference: compose files,
CI workflows, the Makefile and `AGENTS.md` point at them directly.

## Docker

```bash
# Local development stack
make start                                    # or: docker compose up -d
make stop

# Production host: do NOT use the commands above.
# `up -d` recreates the engine container and destroys its writable layer.
# Follow docs/runbooks/deployment.md (manual, per-batch approval).
```

## Questions?

- Check `docs/` for detailed guides
- Read `AGENTS.md` for coding standards and red lines; `.agent/rules/` holds the on-demand detail
- AI agents (Claude Code, Codex, Copilot) read the same `AGENTS.md`
- Security issues: follow `SECURITY.md` (private report, never a public issue)
