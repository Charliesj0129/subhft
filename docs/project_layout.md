# Project Layout

This repo follows a `src/` layout. This map reflects the *actual* tree in this project.

## Top-Level Map

```
.agent/                Agent rules and prompts
.claude/               Claude/Codex configs
.github/               CI workflows
.benchmarks/           pytest-benchmark data
AGENTS.md / CLAUDE.md  Agent/assistant policies
README.md              Entry README
config/                Config files (base + env overlays)
docs/                  Documentation
scripts/               Utility scripts (latency, sim, diagnostics)
src/                   Main package: hft_platform/
rust_core/             Rust extension (PyO3)
rust/                  Rust strategy crate
tests/                 Unit / integration / benchmark
specs/                 Architecture specs
reports/               Generated reports (latency, profiling)
data/                  Local data (ClickHouse, artifacts)
.wal/                  WAL (jsonl)
ops.sh                 Ops / tuning script
```

---

## `src/hft_platform/` (Runtime)

- `cli.py`: CLI entry
- `main.py`: HFTSystem runtime
- `feed_adapter/`: shioaji + normalizer + lob
- `engine/`: event bus
- `strategy/` + `strategies/`: strategy SDK + implementations
- `risk/`: risk validators + fast gate
- `order/`: adapter + rate limiter + circuit breaker
- `execution/`: router + positions
- `recorder/`: WAL + ClickHouse
- `observability/`: metrics
- `ipc/`: shared memory ring buffer
- `contracts/`, `events.py`, `core/`, `utils/`

---

## Config Layout (`config/`)

- `config/base/`: defaults tracked in git
- `config/env/<mode>/`: sim/live overrides
- `config/env/<env>/`: dev/staging/prod overlay via `HFT_ENV`
- `config/symbols.list`: source of symbol universe
- `config/symbols.yaml`: generated
- `config/contracts.json`: broker contracts cache

---

## Extension Points

- New strategies: `src/hft_platform/strategies/`
- Strategy SDK: `src/hft_platform/strategy/`
- New risk rules: `src/hft_platform/risk/`
- New recorder sinks: `src/hft_platform/recorder/`
- New CLI commands: `src/hft_platform/cli.py`
- Hot path Rust: `rust_core/`

---

## Generated / Local Artifacts (safe to ignore)

- `.venv/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`
- `.env` (local secrets)
- `.wal/`, `data/`, `reports/`
- `target/`, `dist/`, `build/`
