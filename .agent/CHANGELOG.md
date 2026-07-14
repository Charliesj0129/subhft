# .agent Governance CHANGELOG

One line per governance change, newest first: `date · files · why (commit)`.
Governing docs covered: `CLAUDE.md`, `AGENTS.md`, `.agent/rules/`,
`.agent/skills/`, `.agent/evals/`, `.agent/templates/`, `.agent/00-MANIFEST.md`.
Rule: `.agent/rules/60-agent-workflow-governance.md` §Governance change control.
The entry for the change being committed carries no hash (unknown at write
time); recover it from `git log` by date + files. History before 2026-07-06
(pre-v2) lives in git log only.

- 2026-07-14 · .claude/hooks/ 4 scripts + tests/unit/test_agent_hooks.py + .claude/settings.json hooks block + AGENTS.md hooks paragraph + commit-work/small-model-handoff marker lines · v3 W1: existing gates lowered to the tool-interception layer (spec docs/superpowers/specs/2026-07-14-agent-system-v3-design.md); probe verdict: hook input carries agent_type on subagent calls; scope_guard + git_guard live-fire verified in-session; authority unchanged, no ADR
- 2026-07-14 · small-model-handoff skill (Independent review packets section), strict-code-review skill (step 0f + checklist: identical-claims need a real diff), read-only-audit skill (step 2 external gate health + output/checklist) · meta-audit 2026-07-14 actions 1+3: twice-rule lesson promotion (verdict/report delivery, ledger 2026-07-06+2026-07-13; identical-claim overclaims, ledger 2026-07-07+2026-07-10) + scheduled-gate cadence hook; the hook's first run found nightly scheduled CI red 5/5 nights, surviving leg = perf-gate-feature-rust parity mismatch 1.0
- 2026-07-14 · .claude/agents/ hft-executor + hft-reviewer + hft-test-writer + hft-docs (new), .claude/settings.json, AGENTS.md (Harness Bindings section), task-intake skill §6 · meta-audit 2026-07-14 action 2: role contracts bound to the harness — reviewer tool-enforced read-only, Do-NOT-Edit + destructive-git ask rules, secrets deny rules; authority/tier/routing tables unchanged (no ADR)
- 2026-07-11 · 00-MANIFEST.md, .claude/commands/ remainder (deleted, 31 files: 29 dangling symlinks into the removed .agent/commands/ + code-review-team.md + debug-team.md) · round-2 user-approved cleanup: residue of the same-day generation removal below
- 2026-07-11 · 00-MANIFEST.md, rules/ecc (deleted), contexts (deleted), workflows (deleted), teams framework (deleted; rounds/ evidence kept), untracked dead generations (agents/commands/pixiu/logs/mcp/extensions/project_context.json/ecc_hooks.json), .claude/commands/alpha-research.md (deleted), research-factory skill + research/README.md + .gitignore reference fixes · user-approved dead-tool cleanup per manifest audit; tag pre-cleanup-2026-07-11
- 2026-07-11 · spec rollout table, memory wrap-up (current_session, open-questions, successful-patterns, current-risks) · wave 2+ marked DONE with commit hashes; §11 stale "no remote / ~37 unpushed" evidence corrected (origin exists since 2026-07-08); #11 destination decision routed to open-questions
- 2026-07-11 · skills/agent-meta-audit (new), skills/00-index.md · #15 periodic meta-audit: quarterly or every ~10 delegations, audit scoreboard trends / intervention rate / stale skills / gate failures → dated report in .agent/reports/ with ≤3 owner-assigned actions
- 2026-07-11 · memory-update skill · #13 executable session wrap-up: 6-step ordered checklist (current_session → lessons → risks → open-questions → uncommitted agent-system sweep → dual-memory cross-check); skips must be justified (4a613b88 = #14 it builds on)
- 2026-07-11 · memory/README.md · #14 dual-memory division of labor: repo memory = model-agnostic project facts; orchestrator private memory = user prefs + cross-session context; wrap-up cross-checks both for strays
- 2026-07-11 · 30-git.md · fix #12 false positive: branch name in backticks read as a path claim by agent-docs-check; branch names in governing docs stay un-backticked (7d3b2475 shipped with the gate red — caught same session)
- 2026-07-11 · 30-git.md, memory/current_session.md · #12 branch-per-theme discipline: one branch = one theme, registry (purpose + expected lifetime) lives in current_session.md; the mixed-theme distillation branch is grandfathered as the rule's evidence
- 2026-07-10 · research-factory skill, agent-docs-known-drift.txt · #10 verdict evidence commit cadence (every verdict → immediate narrow-gate alpha: commit of its evidence); fixed the stale .agent/agents/* roles table — ratchet baseline shrinks 16→12
- 2026-07-10 · commit-work skill · #9 validation matrix as fill-in artifact: blast-radius row ticked before every commit; "Checks NOT run" mandatory in every commit report
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
