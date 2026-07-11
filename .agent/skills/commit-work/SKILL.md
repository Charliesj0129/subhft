---
name: commit-work
description: "Create high-quality git commits: review/stage intended changes, split into logical commits, and write clear commit messages (including Conventional Commits). Use when the user asks to commit, craft a commit message, stage changes, or split work into multiple commits."
---

# Commit work

## Goal
Make commits that are easy to review and safe to ship:
- only intended changes are included
- commits are logically scoped (split when needed)
- commit messages describe what changed and why

## Inputs to ask for (if missing)
- Single commit or multiple commits? (If unsure: default to multiple small commits when there are unrelated changes.)
- Commit style: Conventional Commits are required.
- Any rules: max subject length, required scopes.

## Workflow (checklist)
1) Inspect the working tree before staging
   - `git status`
   - `git diff` (unstaged)
   - If many changes: `git diff --stat`
2) Decide commit boundaries (split if needed)
   - Split by: feature vs refactor, backend vs frontend, formatting vs logic, tests vs prod code, dependency bumps vs behavior changes.
   - If changes are mixed in one file, plan to use patch staging.
3) Stage only what belongs in the next commit
   - Prefer patch staging for mixed changes: `git add -p`
   - To unstage a hunk/file: `git restore --staged -p` or `git restore --staged <path>`
4) Review what will actually be committed
   - `git diff --cached`
   - Sanity checks:
     - no secrets or tokens
     - no accidental debug logging
     - no unrelated formatting churn
5) Describe the staged change in 1-2 sentences (before writing the message)
   - "What changed?" + "Why?"
   - If you cannot describe it cleanly, the commit is probably too big or mixed; go back to step 2.
6) Write the commit message
   - Use Conventional Commits (required):
     - `type(scope): short summary`
     - blank line
     - body (what/why, not implementation diary)
     - footer (BREAKING CHANGE) if needed
   - Prefer an editor for multi-line messages: `git commit -v`
   - Use `references/commit-message-template.md` if helpful.
7) Fill in the blast-radius validation row (required — not prose, a filled row)
   Name which row the staged change is, tick its checks BEFORE committing
   (source: CLAUDE.md §Validation Requirements). A commit spanning rows takes
   the strictest applicable row.

   | Row | Required before commit |
   |---|---|
   | docs-only | every referenced path exists (`rg --files`); diff review; `make agent-docs-check` if agent docs touched |
   | bug fix | focused regression test that fails before / passes after (break-probe) |
   | code+test (non-hot-path) | focused tests green; ruff/format clean; mypy clean in changed files |
   | hot path / shared contract | targeted tests PLUS scaled-int, monotonic-time, fail-closed, state-transition checks; benchmark if latency-relevant |
   | broker/adapter | protocol conformance tests + `make shioaji-guard` |
   | research evidence | verdict artifacts append-only; `alpha:` type; pre-registered floors untouched |
   | merge-level | `make check` minimum; `make ci` for merge confidence |

8) Repeat for the next commit until the working tree is clean (or intentionally dirty)

## Deliverable
Provide:
- the final commit message(s)
- a short summary per commit (what/why)
- the commands used to stage/review (at minimum: `git diff --cached`, plus any tests run)
- the blast-radius row used per commit, and — MANDATORY, never omitted — an
  explicit **"Checks NOT run"** list (empty is stated as "none"); a commit
  report without it is incomplete
