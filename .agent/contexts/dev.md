# Development Context

Mode: Active development
Focus: HFT Platform implementation, hot-path safety, focused verification

## Behavior
- Start from `AGENTS.md`, `docs/MODULES_REFERENCE.md`, and the relevant `.agent/skills/*/SKILL.md`.
- Retrieve nearby code and contracts before editing; do not rely on memory for module boundaries.
- Keep changes narrow and preserve hot-path laws: no float prices, no blocking IO, no avoidable allocations.
- Run the smallest meaningful test or lint target after changes.

## Priorities
1. Preserve trading safety and fail-closed behavior
2. Preserve latency and allocation discipline
3. Keep module contracts stable
4. Verify with targeted tests

## Tools to favor
- `rg` / `rg --files` for retrieval
- `uv run pytest` or focused `make` targets for verification
- HFT skills: `hft-hot-path-dev`, `hft-strategy-dev`, `hft-execution`, `hft-recorder`, `hft-test-hft`
