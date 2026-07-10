# .agent Governance CHANGELOG

One line per governance change, newest first: `date · files · why (commit)`.
Governing docs covered: `CLAUDE.md`, `AGENTS.md`, `.agent/rules/`,
`.agent/skills/`, `.agent/evals/`, `.agent/templates/`, `.agent/00-MANIFEST.md`.
Rule: `.agent/rules/60-agent-workflow-governance.md` §Governance change control.
The entry for the change being committed carries no hash (unknown at write
time); recover it from `git log` by date + files. History before 2026-07-06
(pre-v2) lives in git log only.

- 2026-07-10 · evals/golden-intake-tasks.md (new), evals/00-index.md, 00-MANIFEST.md, task-intake example · #8 golden intake tasks: 8 pinned routing cases run after routing-relevant governance changes; fixed the task-intake example that still said "delegate by default" (pre-ROI drift the cases exist to catch)
- 2026-07-10 · task-intake skill (new §8), memory/current_session.md · #7 checkpoint/resume: tasks expected to outlive a context window maintain a resumable block in current_session.md after each verifiable unit (dc7d958c)
- 2026-07-10 · memory/delegations/ (new), memory/README.md, model-routing.md, task-intake + small-model-handoff skills · #5 delegation archive: packet + executor report + review verdict stored verbatim per delegation, linked from the ledger (99c9b0c0)
- 2026-07-10 · 60-agent-workflow-governance.md, CHANGELOG.md, templates/ADR_TEMPLATE.md, 00-MANIFEST.md · institutionalization #3: governance change control — CHANGELOG entry + ADR requirement for authority/tier changes (ba5b0247)
- 2026-07-10 · docs/superpowers/specs/2026-07-10-agent-system-institutionalization-design.md · 15-point institutionalization spec approved, wave 1 implemented same day (ddce6a24)
- 2026-07-10 · scripts/check_agent_docs.py, Makefile, .agent/agent-docs-known-drift.txt, tests · #2 agent-docs consistency gate wired into `make check` (0aafd55e)
- 2026-07-10 · .agent/00-MANIFEST.md · #1 lifecycle manifest: every .agent/ subdir labeled ACTIVE/DEPRECATED/ARCHIVE-CANDIDATE (b898352b)
- 2026-07-10 · .agent/memory/model-routing.md · #6 pre-registered widening probes P1 (Haiku multi-file mechanical) / P2 (Sonnet >=3-file cross-module) (93ddfb47)
- 2026-07-10 · AGENTS.md, model-routing.md, task-intake + small-model-handoff skills · #4 ROI-first delegation (direct-by-default, trigger-gated delegation, cheapest-capable model table); cleared the cross-session uncommitted-governance debt (7ad864b1)
- 2026-07-08 · CLAUDE.md, AGENTS.md, task-intake skill · task-intake wired as the mandatory v2 entry point for every natural-language task (cf32f5b1)
- 2026-07-07 · .agent/rules/40-ops.md · stale ops change-control runbook path fixed — first end-to-end Haiku delegation + narrow-gate commit (74d95e06)
- 2026-07-07 · scripts/check_git_preconditions.sh · `--narrow-commit` SAFE-WITH-CARE gate: staged-set==allowlist replaces clean-tree requirement for path-scoped commits (e6273d15)
- 2026-07-06 · AGENTS.md · v2 refinement: model-agnostic orchestrator wording, task-type routing table, 12-field packet template (1c6d35dd)
- 2026-07-06 · CLAUDE.md, AGENTS.md, 8 meta-skills, .agent/memory/ base · Agent System v2 landed (knowledge distillation) (83bccb05)
