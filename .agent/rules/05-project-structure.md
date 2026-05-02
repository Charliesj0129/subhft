# Project Structure & Lifecycle Hooks

## Layout

- `src/` — Python source.
- `rust_core/` — PyO3 Rust extensions (hot-path kernels).
- `config/` — YAML/JSON config.
- `.agent/` — AI brain (rules, skills, memory).

## Lifecycle Hooks (MUST EXECUTE)

### On Session Start

Trigger: new conversation or "Hello".
1. Read `.agent/memory/codebase_map.md`, `.agent/memory/module_gotchas.md`.
2. Read `.agent/memory/current_session.md` (if exists) and last 20 lines of `.agent/memory/lessons_learned.md`.
3. Report: "Restored session from [Date]. Last goal was: [Goal]."
4. Check if any prior `blockers` are still relevant.

### On Session End

Trigger: "Pause", "Stop", "Wrap up", or "Save".
1. Update `.agent/memory/current_session.md` with date, goal, status, next steps.
2. If any bugs fixed, perf insight, or arch decision this session → append to `.agent/memory/lessons_learned.md` using `## [TYPE] Title (YYYY-MM)` where TYPE ∈ `[BUG]`, `[PERF]`, `[ARCH]`, `[GOTCHA]`.
3. Call `session-manager` skill to save state.

### On Significant Fix

Trigger: after any `fix:` or `perf:` commit.
1. If the fix reveals a systemic issue or reusable insight, append entry to `.agent/memory/lessons_learned.md` with Context, Fix, Rule, and commit hashes.

### On Pre-Commit

Trigger: "commit" or "PR".
1. `ruff check .`
2. `pytest` (relevant files only).
