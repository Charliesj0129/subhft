# Current Session State

## Last Updated

- **Date**: 2026-07-11
- **Session**: project cleanup (user-approved scope) — dead `.agent/`
  generations removed per manifest audit (tag pre-cleanup-2026-07-11),
  three branches/worktrees retired (#360 MERGED, #371/#376 CLOSED —
  verified via gh), orchestrator private memory pruned to
  development-relevant entries. Prior session: "全部啟動" activation
  COMPLETE — all 15 institutionalization points landed; the spec's rollout
  table carries every commit hash.

## Status

- [x] Wave 1 (2026-07-10): spec ddce6a24 + #1 b898352b + #2 0aafd55e +
      #4 7ad864b1 + #6 93ddfb47.
- [x] Wave 2+ (2026-07-10/11): #3 ba5b0247, #5 99c9b0c0, #7 dc7d958c,
      #8 be745886, #9 ae933463, #10 fe62fbe4+ea5cfeed, #11 1a973302,
      #12 7d3b2475+ba646cef, #13 707b07fc, #14 4a613b88, #15 c4261c67.
- Verification: every commit through the `--narrow-commit` gate
  (staged-set == allowlist); `make agent-docs-check` green at each landing
  (one late catch: 7d3b2475 shipped red because a pipe masked the exit
  code — fixed same session in ba646cef); #10b 131 research tests green;
  #11 5 behavior tests + ruff + mypy green; `make check` exit=0 after the
  wrap-up commit (lint, typecheck, discipline, dependency-boundary,
  test-hygiene, agent-docs). NOT run: `make ci` (no merge in this session).

## Blockers

- #11 first bundle run: destination decision (Charlie) — see
  open-questions.md.
- Unchanged from 07-08: prod deploy of 433be777, prod back-to-live.
  (#371/#376 close decisions RESOLVED — both CLOSED on GitHub, verified
  2026-07-11; fresh SDK PR still owed when the 1.5.5 migration resumes.)

## Context

- Branch: `docs/agent-knowledge-distillation` (ahead of origin by the
  2026-07-11 cleanup commits; push awaits per-operation approval).
- Working tree: 7 M research files + `.claude/settings.json` +
  `.understand-anything/` are Charlie's concurrent work — preserve
  byte-identical. The untracked research validation/test backlog was
  committed by #10b (ea5cfeed).

## Branch registry (rule: `.agent/rules/30-git.md` §Branch discipline)

One branch = one theme; update this table when creating or retiring a branch.

| Branch | Theme / purpose | Expected lifetime |
|---|---|---|
| `docs/agent-knowledge-distillation` | Agent System v2 + institutionalization waves + governed cleanup. Pre-rule commits also carry shioaji/ops/research work — grandfathered; that mix is the evidence that created this rule | until rollout merges; new themes branch fresh from here on |
| `research-flow/edge-evidence-parity-hardening` | edge-evidence/§7 parity hardening (pushed, synced) | until merged or superseded |
| `research/replay-parity-field-set` | `OrderIntent.session_phase` §7 groundwork (pushed, synced) | until merged or superseded |
| `main` | default branch (behind origin/main by 17) | permanent |

Retired 2026-07-11 (user-approved; every tip verified contained in remote
refs before deletion): `worktree-agent-a6f3b09645464cf0d` (tip 98c609af,
worktree removed), `fix/platform-reduce-only-phantom-latch` (tip e7c8cc97,
PR #360 MERGED, worktree removed), `chore/shioaji-153-validation-harness`
(tip e3a0c200, #371 CLOSED).
