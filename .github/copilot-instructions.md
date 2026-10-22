# Copilot instructions

Project rules for every agent live in `AGENTS.md` (repo root); read it first.
Detail is in `.agent/rules/` and `.agent/skills/`.

Laws for the latency-critical path:

1. No heap allocation per tick; preallocate, pool, or use Rust.
2. Never block the asyncio event loop: no `time.sleep`, no synchronous IO.
3. Prices and accounting values are scaled integers (x10000); no float price math on the hot path, and no `Decimal` there either.
4. Prefer numpy vectorization or Rust for math-heavy work.
5. Python/Rust calls avoid large copies (buffers, not lists).

Style: Python 3.12 type hints, `structlog` (no `print`), `pytest`; idiomatic Rust with PyO3.
`./ops.sh` handles setup, tuning, and testing.
