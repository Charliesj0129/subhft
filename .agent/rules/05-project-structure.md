# Project Structure & Lifecycle Hooks

## Layout
- `src/`: Source code (Python).
- `rust_core/`: Performance critical extensions (Rust).
- `config/`: Configuration files (YAML/JSON).
- `.agent/`: AI Brain (Rules, Skills, Memory).

## Lifecycle Hooks (MUST EXECUTE)

### 1. On Session Start
**Trigger**: When the user starts a new conversation or says "Hello".
**Action**:
1. Read `.agent/memory/current_session.md` (if exists).
2. Read `.agent/memory/lessons_learned.md` (last 20 lines).
3. Report status: "Restored session from [Date]. Last goal was: [Goal]."
4. Check if any `blockers` from last session are still relevant.

### 2. On Session End
**Trigger**: When the user says "Pause", "Stop", "Wrap up", or "Save".
**Action**:
1. **MANDATORY**: Update `.agent/memory/current_session.md` with:
   - Date, goal summary, status (completed/in-progress/blocked), next steps.
2. **ALWAYS** review the session for new lessons:
   - Were any bugs fixed? Any surprising behavior discovered?
   - Any performance insights or architectural decisions made?
   - If yes, append to `.agent/memory/lessons_learned.md` using the format:
     `## [TYPE] Title (YYYY-MM)` where TYPE is `[BUG]`, `[PERF]`, `[ARCH]`, or `[GOTCHA]`.
3. Call `session-manager` skill to save state.

### 3. On Significant Fix
**Trigger**: After any `fix:` or `perf:` commit is created.
**Action**:
1. Evaluate whether the fix reveals a systemic issue or reusable insight.
2. If yes, append a new entry to `.agent/memory/lessons_learned.md`.
3. Include: Context, Fix, Rule, and relevant Commit hashes.

### 4. On Pre-Commit
**Trigger**: When asked to "commit" or "PR".
**Action**:
1. Run `ruff check .`
2. Run `pytest` (relevant files only).
