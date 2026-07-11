# Current Session State

## Last Updated

- **Date**: 2026-07-12
- **Session**: post-merge fix batch (user-ordered "修復這些" on the merge
  report's unfixed findings). Five local commits on main, UNPUSHED (push
  needs per-operation approval): aa62cff6 (17 disk-only research modules
  imported by tracked code), 1a4c26e8 (data_pipeline -> package,
  candidate_loop + 10 pdq tools allowlisted in factory.py audit),
  c9036b1a (.gitkeep skeletons for skill-referenced local dirs — fixes
  the 13 fresh-clone agent-docs-check errors), 82aab8a9 (canary-deploy
  uses `uv run`; ci.yml research-audit-strict continue-on-error removed),
  a833d4a0 (pdq_tsi15_decomposition_audit — dynamic load_module BASE_TOOL,
  invisible to import analysis). Clean-worktree evidence at a833d4a0:
  research-audit-strict 0 errors/0 warnings, agent-docs-check 0 errors,
  tests/unit/research 695 passed / 1 skipped. CodeQL on the 07-11 push:
  success. New findings recorded in open-questions.md: scheduled-CI red
  (gitleaks/recorder-drills/benchmark) + deploy.yml zero-jobs failure.
- **Prior session (2026-07-11)**: project cleanup rounds 1+2 (user-approved scope).
  Round 1: dead `.agent/` generations removed per manifest audit (tag
  pre-cleanup-2026-07-11), three branches/worktrees retired (#360 MERGED,
  #371/#376 CLOSED — verified via gh), orchestrator private memory pruned.
  Round 2 (larger scope): `.claude/commands/` residue (31 files), legacy
  `rust_strategy` crate, 8 orphaned scripts (audit in commit 304a1e63's
  message), specs/ relocation, untracked root junk (~30MB), arxiv literal
  dir merged into `arxiv_papers/`, all 13 stashes exported to
  `~/hft_stash_archive/2026-07-11/` then dropped, local main ff-synced to
  origin. Tags kept (archive/* prune deferred until after the first #11
  bundle run). Rollout merge (same day, user-requested): docs/
  agent-knowledge-distillation merged into main via merge commit a1e2d0f2
  (+ uv.lock specifier fixup 68c42cf3). Convergence commits landed first on
  the branch: b655a2db (Charlie's in-flight research-factory work set,
  approved; includes force-added research/data_pipeline.py) and cf40f68b
  (6 manifest-referenced evidence artifacts force-added from gitignored
  outputs// docs-artifacts paths). 2 conflicts in the §7 replay-parity gate
  resolved to the branch side (blob-verified strict successor of main's
  PR #365). make ci exit 0 on the merged tree BEFORE main advanced.
  Prior session: "全部啟動" activation COMPLETE — all 15
  institutionalization points landed.

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
- Rollout-merge CI (2026-07-11): `make ci` exit 0 — 14071 passed /
  19 skipped, coverage 87.77% — on the merge tree in a CLEAN worktree
  (fresh uv sync + maturin), which surfaced 3 fresh-clone-only defects the
  dirty primary worktree masked: an unformatted committed test file, the
  half-committed paper-index alias set, and gitignored-but-imported
  research/data_pipeline.py + 6 evidence artifacts. All fixed on-branch
  before the merge landed.

## Blockers

- #11 first bundle run: destination decision (Charlie) — see
  open-questions.md.
- Unchanged from 07-08: prod deploy of 433be777, prod back-to-live.
  (#371/#376 close decisions RESOLVED — both CLOSED on GitHub, verified
  2026-07-11; fresh SDK PR still owed when the 1.5.5 migration resumes.)

## Context

- Branch: `docs/agent-knowledge-distillation` — MERGED into main
  2026-07-11 (a1e2d0f2); main + branch pushed to origin same day with
  approval. Primary worktree switched to main at the end of the merge
  session; the docs branch is retirable on Charlie's explicit request.
- Working tree: `.claude/settings.json` + `.understand-anything/`
  (untracked) are Charlie's concurrent work — preserve byte-identical.
  The formerly-dirty 7 research files were committed with approval in
  b655a2db as part of the convergence.

## Branch registry (rule: `.agent/rules/30-git.md` §Branch discipline)

One branch = one theme; update this table when creating or retiring a branch.

| Branch | Theme / purpose | Expected lifetime |
|---|---|---|
| `docs/agent-knowledge-distillation` | Agent System v2 + institutionalization waves + governed cleanup. Pre-rule commits also carry shioaji/ops/research work — grandfathered; that mix is the evidence that created this rule | MERGED to main 2026-07-11 (a1e2d0f2); primary worktree on main since then — retirable on explicit request |
| `research-flow/edge-evidence-parity-hardening` | edge-evidence/§7 parity hardening (pushed, synced) | until merged or superseded |
| `research/replay-parity-field-set` | `OrderIntent.session_phase` §7 groundwork (pushed, synced) | until merged or superseded |
| `main` | default branch (rollout merge a1e2d0f2 landed 2026-07-11) | permanent |

Retired 2026-07-11 (user-approved; every tip verified contained in remote
refs before deletion): `worktree-agent-a6f3b09645464cf0d` (tip 98c609af,
worktree removed), `fix/platform-reduce-only-phantom-latch` (tip e7c8cc97,
PR #360 MERGED, worktree removed), `chore/shioaji-153-validation-harness`
(tip e3a0c200, #371 CLOSED).
