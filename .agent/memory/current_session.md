# Current Session State

## Last Updated

- **Date**: 2026-07-14
- **Session**: agent-capability uplift (提升專案 agent 能力; plan-approved,
  3 workstreams, all local commits on main, NOT pushed).
  A: first agent-meta-audit report at
  .agent/reports/agent-meta-audit-2026-07-14.md (commit 3bd15c41) — 3
  actions, all executed this session. B (action 2): harness bindings
  (commit 696729c8) — .claude/agents/ hft-executor / hft-reviewer
  (read-only tools) / hft-test-writer / hft-docs + .claude/settings.json
  ask-rules (Do-NOT-Edit paths, destructive git) + deny-rules (.env*,
  config/settings.py) + AGENTS.md Harness Bindings section + task-intake
  §6; golden-intake 8/8 PASS. C (actions 1+3): twice-rule promotion into
  small-model-handoff (independent review packets) + strict-code-review
  (identical-claims need real diff) + read-only-audit step 2 external
  gate health.
  FINDING from the new gate-health step's first run: nightly scheduled CI
  red 5/5 nights (07-09→07-13); after the 07-13 fix batch the surviving
  red leg is make perf-gate-feature-rust (feature_engine_parity_mismatch
  _rate 1.0 in one leg ×3 reps, run 29280356169) plus stale_event_skipped
  spam with age_ms ≈ 1.78e12 (epoch-zero timestamps vs wall clock in the
  drill). NOT touched — Tier-3-adjacent, out of session scope; Charlie
  decides.
  OWED: (1) hft-reviewer smoke spawn — agent defs register at session
  start, run early NEXT session (meta-audit action 2 done-condition);
  (2) ledger lesson pointer-swap in model-routing.md deferred: file
  carries the shioaji session's uncommitted 2026-07-13 entry, must not
  be staged (golden-intake 8/8 line appended there, also unstaged, rides
  with that pending commit); (3) meta-audit carried findings: P1/P2
  widening probes still owed on next REAL matching tasks; trivy
  push-then-scan ordering decision = Charlie.
  Branch-per-theme deliberately deviated (commits on main): concurrent
  dirty shioaji-1.5.6 tree + harness reads .claude/ from the working
  tree; documented in the session report.
- **Prior session (2026-07-13)**: scheduled-CI fix batch (user-ordered: push / legacy /
  修復 scheduled-CI / retire docs branch). Fix commits on main, PUSHED
  with approval: 4242c7b0 (gitleaks allowlist — six placeholder values,
  each reviewed at source; full-history scan now 'no leaks found'),
  46521afe (replay-safety spec aligned with 588ebfbf strict-mode
  quarantine contract; verify-ce3 8 passed — was red since 2026-04-27),
  f86bf944 (recorder drill summary here-docs de-indented; bash needs the
  terminator at column 0), 4a1d73d6 (deploy.yml parseable again: guard
  output replaces secrets-in-step-if; boolean dry_run comparisons),
  2aa48ef3 (Darwin Gate comparisons normalized by median runner-speed
  ratio + catastrophic cap — the baseline auto-update had turned into a
  one-way speed ratchet failing both pushes; validated on the real
  failed artifact, 1.40x shift → PASS). The 7 untracked
  tools-root pdq scripts moved to research/tools/legacy via
  `python -m research.factory converge-tools` (user picked legacy);
  local research-audit-strict now 0 errors / 0 warnings.
  docs/agent-knowledge-distillation RETIRED (tip cf40f68b verified in
  origin/main, deleted local + origin on explicit request).
  CD — Deploy end-to-end (follow-on): once CI turned green the workflow
  executed for real for the first time and surfaced two dormant defects,
  fixed one-at-a-time with a run between: bfe255d9 (github.repository
  preserves owner case, OCI names must be lowercase — IMAGE_NAME
  lowercased via GITHUB_ENV in both jobs; after this the image built and
  PUSHED to GHCR for the first time) and 70845b3d (trivy gate red on 35
  HIGH/CRITICAL debian-12.14 base CVEs, ALL with no fixed version —
  ignore-unfixed:true keeps the fixable-CVE signal; note the step order
  is push-then-scan, the gate never blocked publishing). Evidence: CD —
  Deploy run 29239980470 SUCCESS — full chain push → CI → build → GHCR
  push → trivy → no-op deploy steps is green.
  OWED (Charlie, one click): production environment required-reviewer
  rule (Settings → Environments) — API creation attempt was
  permission-denied; until configured, deploy.yml builds/pushes images
  on main CI success but cannot touch any host (no DEPLOY_*/STAGING_*
  secrets exist — verified via API, names only).
- **Prior session (2026-07-12)**: post-merge fix batch ("修復這些" on the
  merge report's unfixed findings). Six commits, rebased over
  benchmark-bot dc98d877 and pushed 2026-07-13 as: 47dce271 (17
  disk-only research modules imported by tracked code), 22a94ae0
  (data_pipeline -> package; candidate_loop + 10 pdq tools allowlisted
  in factory.py audit), bb1863f4 (.gitkeep skeletons — 13 fresh-clone
  agent-docs errors), cef40bc5 (canary evaluator under uv run;
  research-audit-strict enforcing again), 35414721
  (pdq_tsi15_decomposition_audit — dynamic load_module BASE_TOOL,
  invisible to import analysis), a794df5c (memory wrap-up).
  Clean-worktree evidence: research-audit-strict 0/0, agent-docs-check
  0 errors, tests/unit/research 695 passed / 1 skipped, make ci exit 0
  — 14071 passed / 19 skipped, coverage 87.78%. CodeQL: success.
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

- Branch: primary worktree on `main`; `docs/agent-knowledge-distillation`
  retired 2026-07-13 (see registry below).
- Working tree: Charlie's concurrent shioaji 1.5.6 quote-only work is
  IN FLIGHT — ~17 modified tracked files (quote_connection_pool,
  market_data, bootstrap, system, health + their tests, Makefile, docs)
  plus untracked `Shioaji-1.5.6/`, `shioaji-v1.5.6-Linux-x86_64/`,
  `docs/superpowers/{plans,specs}/2026-07-13-shioaji-156-quote-only*`,
  `.claude/settings.json`, `.understand-anything/`. Preserve
  byte-identical; stage narrowly around them.

## Branch registry (rule: `.agent/rules/30-git.md` §Branch discipline)

One branch = one theme; update this table when creating or retiring a branch.

| Branch | Theme / purpose | Expected lifetime |
|---|---|---|
| `research-flow/edge-evidence-parity-hardening` | edge-evidence/§7 parity hardening (pushed, synced) | until merged or superseded |
| `research/replay-parity-field-set` | `OrderIntent.session_phase` §7 groundwork (pushed, synced) | until merged or superseded |
| `main` | default branch (rollout merge a1e2d0f2 landed 2026-07-11) | permanent |

Retired 2026-07-11 (user-approved; every tip verified contained in remote
refs before deletion): `worktree-agent-a6f3b09645464cf0d` (tip 98c609af,
worktree removed), `fix/platform-reduce-only-phantom-latch` (tip e7c8cc97,
PR #360 MERGED, worktree removed), `chore/shioaji-153-validation-harness`
(tip e3a0c200, #371 CLOSED).

Retired 2026-07-13 (explicit user request): `docs/agent-knowledge-distillation`
(tip cf40f68b verified contained in origin/main, zero unpushed commits;
deleted locally and on origin). Theme history: Agent System v2 +
institutionalization waves + governed cleanup; merged via a1e2d0f2.
