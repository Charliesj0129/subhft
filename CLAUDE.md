# CLAUDE.md

This file provides project-specific guidance for Claude Code when working in this repository.

## Project Summary

- High-performance event-driven trading platform with Shioaji integration and ClickHouse recording.
- Core pipeline: Market Data -> Normalizer -> LOB -> Strategy -> Risk -> Order Adapter -> Broker.
- Code lives in src/hft_platform; configs live in config/.

## Key Paths

- src/hft_platform/: core services, adapters, strategy, risk, execution, recorder.
- config/: base config, env overrides, symbols sources.
- docs/: docs and architecture references.
- tests/: unit and integration tests.
- agents/, commands/, contexts/, hooks/, rules/, skills/: repo-level Claude Code workflow assets.

## Common Commands

- make dev: install deps (uv) and prepare local env.
- make start: build symbols and start docker compose.
- make sync-symbols: fetch broker contracts and rebuild config/symbols.yaml.
- python -m hft_platform config preview: show symbols count and sample.
- python -m hft_platform config validate: validate symbols and config.
- make test: run unit tests.

## Symbols Workflow

- config/symbols.list is the single source of truth.
- make sync-symbols writes config/symbols.yaml and config/contracts.json.
- Use SYMBOLS_CONFIG=config/symbols.yaml for runtime.
- Do not hand-edit config/symbols.yaml unless explicitly required.

## Data Flow Verification

- docker compose ps
- curl http://localhost:9090/metrics
- ClickHouse checks:
  - SELECT count() FROM hft.market_data
  - SELECT symbol, count() FROM hft.market_data GROUP BY symbol ORDER BY count() DESC LIMIT 10

## Rules

- Do not commit secrets. .env is local only; keep .env.example for templates.
- config/contracts.json is generated and should not be committed by default.
- When symbols.list changes, regenerate symbols.yaml with make sync-symbols.
- Prefer existing make targets and CLI commands over ad hoc scripts.

## Hooks and MCP

- hooks/hooks.json contains optional warnings and session snapshots.
- mcp-configs/mcp-servers.json contains template MCP server definitions.
