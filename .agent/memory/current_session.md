# Current Session State

## Last Updated

- **Date**: 2026-07-10 (third wave)
- **Session**: "全部啟動" — activate ALL wave-2+ institutionalization points
  (#3, #5, #7–#15) from the approved 15-point spec
  (`docs/superpowers/specs/2026-07-10-agent-system-institutionalization-design.md`).

## Resumable block (task-intake §8 — delete when the activation completes)

- **Done units**: #3 governance change control (ba5b0247); #5 delegation
  archive (99c9b0c0); #7 checkpoint/resume (dc7d958c); #8 golden intake
  tasks (be745886); #9 commit-work validation matrix (ae933463);
  #10a verdict cadence (fe62fbe4); #10b research evidence backlog
  (ea5cfeed, 61 files); #11 bundle tooling (1a973302, first run blocked on
  destination); #12 branch-per-theme rule + registry (this commit).
- **Next step**: #13 wrap-up checklist (memory-update skill), #14 dual-memory
  division of labor (memory/README.md), #15 quarterly meta-audit, spec
  rollout update (mark points DONE + fix stale no-remote/37-unpushed
  evidence), memory + final report.
- **Verification state**: `make agent-docs-check` green after every commit so
  far; every commit passed `--narrow-commit` with staged-set == allowlist;
  #10b: 131 research tests green (3.63s); #11: 5 tests + ruff + mypy green;
  `make check` / `make ci` not yet run (planned: once, at the end).
- **Standing constraints**: no push (human-approved per op); #11 first bundle
  needs a destination from Charlie; research M-files stay byte-identical.

## Status

- [x] Wave 1 (same day, earlier session): spec ddce6a24 + #1 manifest
      b898352b + #2 agent-docs gate 0aafd55e + #4 ROI-debt 7ad864b1 +
      #6 probes 93ddfb47.
- [ ] Wave 2+ activation in flight (see resumable block).

## Blockers

- #11 first bundle run: destination decision (Charlie).
- Unchanged from 07-08: #371/#376 close decisions, prod deploy of 433be777,
  prod back-to-live.

## Context

- Branch: `docs/agent-knowledge-distillation` (upstream exists; 14 commits
  ahead of origin as of the #12 commit — push awaits approval).
- Working tree: 7 M research files + `.claude/settings.json` +
  `.understand-anything/` are Charlie's concurrent work — preserve
  byte-identical. The untracked research validation/test backlog was
  committed by #10b (ea5cfeed).

## Branch registry (rule: `.agent/rules/30-git.md` §Branch discipline)

One branch = one theme; update this table when creating or retiring a branch.

| Branch | Theme / purpose | Expected lifetime |
|---|---|---|
| `docs/agent-knowledge-distillation` | Agent System v2 + institutionalization waves. Pre-rule commits also carry shioaji/ops/research work — grandfathered; that mix is the evidence that created this rule | until rollout merges; new themes branch fresh from here on |
| `chore/shioaji-153-validation-harness` | shioaji 1.5.x validation lineage; diverged from origin same-name (PR #371's older lineage) — never force-push | until #371 end-state (see current-risks.md) |
| `research-flow/edge-evidence-parity-hardening` | edge-evidence/§7 parity hardening (pushed, synced) | until merged or superseded |
| `research/replay-parity-field-set` | `OrderIntent.session_phase` §7 groundwork (pushed, synced) | until merged or superseded |
| `fix/platform-reduce-only-phantom-latch` | phantom reduce-only latch fix (worktree; PR #360 deployed) | retire after PR closure confirmed |
| `worktree-agent-a6f3b09645464cf0d` | ephemeral agent worktree (benchmark baseline) | clean up; never push |
| `main` | default branch (behind origin/main by 17) | permanent |
