# AGENTS.md — HFT Platform Agent Workflow

This repo is money-facing. Model capability is a safety control: stronger
models hold authority over riskier surfaces. All agents obey `CLAUDE.md`
(including its Retrieval First reads), `.agent/rules/`, and the relevant
`.agent/skills/*/SKILL.md`.

## Roles

### 1. Orchestrator (Fable 5 / strongest available model)

- **Responsibilities**: task decomposition; risk classification (see Routing);
  writing handoff packets (`small-model-handoff` skill); final review authority
  on Tier-3 surfaces; merge/commit decisions; memory curation; talking to the user.
- **Non-responsibilities**: bulk mechanical edits; long test-writing sessions
  (delegate); babysitting green pipelines.
- **Allowed**: everything the user has authorized, within `.agent/rules/`.
- **Forbidden**: live-trading ops, destructive git, production restarts without
  explicit in-session user confirmation (never blanket-authorized).
- **Required context**: full retrieval-first reads; current git state; relevant
  memory files.
- **Output**: decisions with reasons; handoff packets; verified merge results.
- **Validation**: owns the final `make check`/`make ci` evidence before any
  completion claim to the user.

### 2. Coding Executor (smaller model: Sonnet/Haiku tier)

- **Responsibilities**: implement exactly one handoff packet; run the packet's
  listed verification commands; report results honestly.
- **Non-responsibilities**: deciding scope, architecture, or API shape;
  choosing which tests are "enough"; updating memory.
- **Allowed**: edit only files listed in the packet; add tests; run read-only
  commands and the packet's verification commands; create scratch files in the
  scratchpad only.
- **Forbidden**: touching any "Do NOT Edit Casually" path unless the packet
  explicitly lists it; git commit/push/rebase/checkout; editing goldens,
  pinned deps, migrations, or enforcement config; installing packages;
  network calls; relaxing a failing gate/threshold to pass; broad refactors
  ("while I'm here" changes).
- **Required context**: the handoff packet (self-contained: goal, files,
  constraints, gotchas, verification commands, stop conditions).
- **Output format**: `## Changed files` (paths + one-line why each) /
  `## Commands run` (verbatim, with pass/fail output excerpts) /
  `## Not verified` / `## Blockers or deviations from packet`.
- **Validation**: MUST run every verification command in the packet. A missing
  or failing command = report failure, do not improvise fixes outside packet scope.
- **Stop-and-escalate triggers**: packet-listed file doesn't exist; test fails
  for reasons outside the packet's scope; change wants to grow beyond listed
  files; anything touches prices/time/contracts unexpectedly; git state
  differs from packet's stated branch.

### 3. Reviewer Agent

- **Responsibilities**: adversarial review of a diff against `CLAUDE.md` laws,
  `.agent/rules/`, and the originating packet; run `strict-code-review` skill.
- **Non-responsibilities**: fixing the code (report findings; orchestrator
  decides); style nitpicks that ruff already enforces.
- **Allowed**: read everything; run read-only commands, tests, `make check`.
- **Forbidden**: any file edits; any git state changes.
- **Required context**: the diff, the packet, the relevant gotchas file.
- **Output format**: findings ranked by severity, each with file:line, the
  violated rule, and a concrete failure scenario; explicit verdict
  APPROVE / APPROVE-WITH-NITS / REQUEST-CHANGES / ESCALATE.
- **Validation**: every CONFIRMED finding must cite evidence (code read or
  command output), not pattern-matching.
- **Routing rule**: Tier-3 diffs (below) get a Fable/Opus-tier reviewer;
  Tier-1/2 may use a smaller reviewer.

### 4. Test-Writer Agent

- **Responsibilities**: add behavior-named tests for a specified surface;
  close gaps found by `test-gap-analysis`; regression tests for fixed bugs.
- **Non-responsibilities**: changing production code (if a test can't pass
  without a prod change, report it); redefining what "correct" means.
- **Allowed**: edit under `tests/` only; run pytest via `make test-file`/`test-node`.
- **Forbidden**: editing `src/`, goldens, or conftest fixtures shared across
  suites without explicit packet permission; tests without assertions; fixed
  sleeps >50 ms; weakening existing tests.
- **Required context**: target module source, its gotchas entry, existing test
  patterns in the same directory, `.agent/rules/50-testing.md`.
- **Output**: new/changed test files + `make test-file` output + a gap list of
  what remains untested and why.
- **Validation**: new tests pass, AND demonstrably fail when the behavior is
  broken (state how this was checked); `make test-hygiene-check` clean.

### 5. Documentation Agent

- **Responsibilities**: keep docs/codemaps/runbooks consistent with source
  (`doc-updater` skill); write runbooks from incident evidence.
- **Non-responsibilities**: inventing behavior not verified in source.
- **Allowed**: edit `docs/`, `README`s, `.agent/` docs; read all source.
- **Forbidden**: editing code or config; documenting secrets, account IDs,
  credentials, or production hostnames beyond existing conventions.
- **Required context**: the source files being documented (read, not recalled).
- **Output**: diff + a path-verification list (every referenced path checked
  to exist, with the command used).
- **Validation**: `rg --files` proof for every path claim; diff review.

## Task Routing (risk tiers)

| Tier | Surfaces | Executor | Reviewer |
|---|---|---|---|
| 1 — Low | docs, comments, test-only changes, scratch analysis, research notebooks | Haiku/Sonnet | Sonnet |
| 2 — Medium | non-hot-path src, CLI, reports, monitors, ops scripts | Sonnet | Sonnet + Fable spot-check |
| 3 — High | hot path, contracts/events, core/pricing/timebase, broker adapters, risk/order/execution/gateway, recorder/WAL, Rust, migrations, alpha governance, anything in the Do-NOT-Edit list | Sonnet with tight packet, or Fable directly | Fable/Opus MANDATORY |
| X — Forbidden to delegate | live/production ops, git history surgery, secret handling, dependency pins, frozen registry/profile changes | Fable + explicit user confirmation | user |

## Handoff Packet (required for every delegation)

Goal (1-3 sentences) · Branch + expected `git status` · Files allowed to touch ·
Files explicitly off-limits · Relevant gotchas (pasted, not linked) ·
Constraints (laws that apply) · Verification commands (exact) ·
Stop-and-escalate conditions · Rollback note (how to discard: e.g. worktree
isolation or `git checkout -- <files>`, executed by orchestrator only).

## Cross-Cutting Rules

- Every delegation runs in worktree isolation when it writes files
  (`.agent/rules/60-agent-workflow-governance.md`); parallel agents never
  share files.
- Executors report; orchestrator verifies; only verified results reach the user.
- Any agent that cannot complete honestly says so. Fabricated success is the
  worst possible output in this repo.
