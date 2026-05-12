# AGENTS.md Onboarding Rewrite - Design

**Date:** 2026-05-12
**Author:** Codex (brainstorming session)
**Target file:** `AGENTS.md`

## Goal

Rewrite `AGENTS.md` as a hybrid onboarding guide for agents working in
`hft_platform`: a compact safety contract at the top, followed by practical
routing tables that point agents to the canonical project docs, `.agent/rules`,
and `.agent/skills`.

The file should help a new agent start safely without duplicating the full
contents of `docs/MODULES_REFERENCE.md`, `.agent/rules/`, or `.agent/skills/`.

## User Decisions

- Rewrite style: **Hybrid**.
- Preferred shape: compact top-level rules, then deeper references and
  task-specific routing tables.
- `AGENTS.md` should act as a command center and router, not a full manual.

## Current State

`AGENTS.md` is short and mostly generated. It currently says agents must read:

- `docs/AI_DEVELOPER_CHEAT_SHEET.md`
- `docs/MODULES_REFERENCE.md`

During context exploration, `docs/MODULES_REFERENCE.md` existed, but
`docs/AI_DEVELOPER_CHEAT_SHEET.md` did not. The rewrite should remove or replace
that broken required path.

Relevant existing sources:

- `docs/MODULES_REFERENCE.md` - compressed module map and hot-path overview.
- `.agent/rules/00-index.md` - rule index.
- `.agent/rules/01-core-laws.md` - allocator, cache, async, precision, and FFI
  boundary laws.
- `.agent/rules/10-hft-performance.md` - hot-path performance checklist.
- `.agent/skills/00-index.md` - task-to-skill routing.
- `docs/architecture/` - canonical architecture source when design questions
  arise.
- `.agent/memory/module_gotchas.md` - code-level gotchas when present/relevant.

## Proposed Structure

### 1. Mission and Retrieval Rule

Open with a short bilingual statement that this is a Python HFT platform and
that agents must use retrieval-led reasoning. Agents should inspect local docs,
rules, skills, and relevant source before making claims or edits.

### 2. Mandatory First Reads

Replace the missing cheat-sheet requirement with real, current files:

- Always read `docs/MODULES_REFERENCE.md`.
- Always read `.agent/rules/00-index.md`.
- Always read `.agent/skills/00-index.md`.
- Read `docs/architecture/` docs when touching or questioning architecture.
- Read `.agent/memory/module_gotchas.md` when changing modules covered there,
  if the file exists.

This section should be strict but not excessive. It should avoid requiring every
agent to load every rule and skill body on every task.

### 3. Hard Laws

Summarize the five laws from `.agent/rules/01-core-laws.md`:

- No hot-path heap allocations.
- Prefer cache-local packed data.
- No blocking IO or >1 ms compute on the event loop.
- Use scaled integer prices and balances, not floats.
- Keep Python/Rust boundaries zero-copy where possible.

The section should be a summary only and link back to the rule file as the
source of truth.

### 4. Task Routing

Add a concise table that maps common task types to the relevant skill/rule entry:

| Task | First references |
| --- | --- |
| Market data, normalizer, LOB, feature engine | `.agent/skills/hft-market-data/SKILL.md`, `.agent/skills/hft-hot-path-dev/SKILL.md` |
| Strategy changes | `.agent/skills/hft-strategy-dev/SKILL.md`, `.agent/skills/hft-strategy-sdk/SKILL.md` |
| Alpha research and promotion gates | `.agent/skills/hft-alpha-research/SKILL.md`, `.agent/skills/research-factory/SKILL.md` |
| Execution, fills, positions, TCA | `.agent/skills/hft-execution/SKILL.md` |
| Recorder, WAL, ClickHouse | `.agent/skills/hft-recorder/SKILL.md`, `.agent/skills/clickhouse-io/SKILL.md` |
| Ops, sessions, alerts, health | `.agent/skills/hft-ops/SKILL.md`, `.agent/skills/troubleshoot-metrics/SKILL.md` |
| Rust/PyO3 changes | `.agent/skills/rust-pro/SKILL.md`, `.agent/skills/hft-rust-exports/SKILL.md` |
| Tests and verification | `.agent/rules/50-testing.md`, `.agent/skills/hft-test-hft/SKILL.md`, `.agent/skills/python-testing-patterns/SKILL.md` |
| Docs and codemaps | `.agent/skills/doc-updater/SKILL.md`, `docs/MODULES_REFERENCE.md` |
| Architecture decisions | `docs/architecture/`, `.agent/skills/hft-architect/SKILL.md`, `.agent/rules/25-architecture-governance.md` |

The final table can be adjusted during implementation to match exact local
skill availability and naming.

### 5. Workflow Expectations

State operational expectations:

- Read relevant skill files before implementation.
- Keep changes scoped to the requested task.
- Do not refactor unrelated modules.
- Treat hot-path changes as high risk.
- Use `rg`/local source inspection before asserting behavior.
- Preserve user work in a dirty tree.
- Prefer focused verification tied to changed behavior.

### 6. Testing and Verification

Keep this high-level and reference-driven:

- Use targeted unit tests for narrow changes.
- Add or update regression tests for bug fixes.
- Use HFT-specific tests for scaled-int prices, monotonic time, fail-closed
  behavior, and state transitions.
- Broaden verification for shared contracts or hot-path changes.

This section should not hard-code a universal command unless the repo has a
single canonical command for all changes.

### 7. Safety and Git Hygiene

Include guardrails:

- Do not expose secrets or credentials in logs, docs, tests, or commits.
- Do not run production-impacting commands without explicit user intent.
- Do not revert or overwrite unrelated user changes.
- Commit only intentional files when asked to commit.
- Avoid destructive git commands unless explicitly requested.

## Information Architecture

`AGENTS.md` should remain a thin entry point:

- **In file:** mandatory behavior, summary rules, routing table.
- **Out of file:** full module maps, architecture documents, detailed rules,
  skill bodies, and runbooks.

This reduces drift and keeps `AGENTS.md` readable while still making the first
agent turn safer.

## Error Handling

The rewritten instructions should tell agents what to do when a referenced file
is missing:

- State the missing path briefly.
- Search nearby docs with `rg --files`.
- Use the closest current canonical source.
- Avoid inventing project rules from memory.

This directly addresses the current broken `docs/AI_DEVELOPER_CHEAT_SHEET.md`
reference.

## Testing / Validation for the Rewrite

Because this is a documentation change, verification should be documentation
focused:

- `sed -n '1,240p' AGENTS.md` to inspect final readability.
- `rg --files` checks for every path referenced by `AGENTS.md`.
- `git diff -- AGENTS.md` to confirm the rewrite is scoped.
- Optional: markdown lint if a project-local command exists.

## Out of Scope

- Editing `.agent/rules/`, `.agent/skills/`, or `docs/architecture/`.
- Creating the missing `docs/AI_DEVELOPER_CHEAT_SHEET.md`.
- Changing code, tests, or runtime behavior.
- Reorganizing the agent skill system.

## Implementation Notes

The implementation should replace the generated short file with a maintained
onboarding document. Preserve bilingual clarity where useful, because the
existing file is Chinese-first with English labels.

After this design is approved by the user, the next step is to create an
implementation plan using the writing-plans workflow before editing
`AGENTS.md`.
