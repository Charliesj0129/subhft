# AGENTS.md — HFT Platform Agent Workflow

This repo is money-facing. Model capability is a safety control: stronger
models hold authority over riskier surfaces. All agents obey `CLAUDE.md`
(including its Retrieval First reads), `.agent/rules/`, and the relevant
`.agent/skills/*/SKILL.md`.

## Roles

### 1. Orchestrator (Opus-class or stronger)

- **Responsibilities**: task intake (`task-intake` skill — every
  natural-language task enters through it); task decomposition; risk-tier +
  task-type classification (see Routing); ROI-first routing
  (direct/delegate/fan-out/stop with a logged reason); writing handoff packets
  (`small-model-handoff` skill); review of ALL Tier-2/3 diffs; ALL git
  planning and execution; memory curation; talking to the user.
- **Non-responsibilities**: bulk mechanical edits; long test-writing sessions
  (delegate); babysitting green pipelines.
- **Never delegates**: tier classification; final review; any git command;
  Tier-X work; memory file writes (executors report facts, orchestrator routes
  them); Do-NOT-Edit paths.
- **Allowed**: everything the user has authorized, within `.agent/rules/`.
- **Forbidden**: live-trading ops, destructive git, production restarts without
  explicit in-session user confirmation (never blanket-authorized).
- **Must stop and ask the user**: Remote-Write/Destructive git (push, merge,
  rebase, reset, clean, stash-drop, force, branch-delete, history edits);
  live/production ops; dependency pin changes; frozen registry/profile
  changes; golden regeneration; Do-NOT-Edit paths; unknown or user-owned
  dirty files in the blast radius; the task's true tier turns out higher than
  requested; two failed delegation attempts on one task; conflicting
  instructions between governing files; irreversibility discovered mid-task.
- **Required context**: full retrieval-first reads; current git state; relevant
  memory files.
- **Output**: decisions with reasons; handoff packets; verified merge results.
- **Validation**: owns the final `make check`/`make ci` evidence before any
  completion claim to the user. Executor self-reports are context, never
  evidence — review per `strict-code-review` Step 0.

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
- **Routing rule**: Tier-3 diffs (below) get an orchestrator-class reviewer;
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

Entry point: the `task-intake` skill converts a natural-language task into a
classification against these tables; the tables below stay authoritative.

| Tier | Surfaces | Executor | Reviewer |
|---|---|---|---|
| 1 — Low | docs, comments, test-only changes, scratch analysis, research notebooks | Haiku/Sonnet | Sonnet |
| 2 — Medium | non-hot-path src, CLI, reports, monitors, ops scripts | Sonnet | Sonnet + orchestrator-class spot-check |
| 3 — High | hot path, contracts/events, core/pricing/timebase, broker adapters, risk/order/execution/gateway, recorder/WAL, Rust, migrations, alpha governance, anything in the Do-NOT-Edit list | Sonnet with tight packet, or orchestrator directly | orchestrator-class MANDATORY |
| X — Forbidden to delegate | live/production ops, git history surgery, secret handling, dependency pins, frozen registry/profile changes | orchestrator + explicit user confirmation | user |

## Task Routing (task types)

| Task type | Route |
|---|---|
| Orchestration, tier classification, architecture decisions, incident response | Orchestrator only |
| Tier-3 implementation | Orchestrator, or Sonnet under a function/line-exact packet |
| Tier-2 code+test (start <=3 files; widen only with scoreboard evidence) | Sonnet |
| Test-writing (after orchestrator runs `test-gap-analysis`) | Sonnet |
| Bounded read-only investigation (cited claims, zero edits) | Sonnet / Explore |
| Gate cleanup (lint/mypy/format debt, non-hot-path, zero behavior change) | Sonnet |
| Docs, comments, mechanical edits, counting, formatting, enumeration checks | Haiku — only if executable purely by following commands + rules; any design choice makes it a Sonnet+ task |
| Tier-1 review | Sonnet |
| Git planning (sequences, rollback plans) | Orchestrator only (plan is not execution) |
| Git execution | Orchestrator only; smaller models NEVER |
| Push, merge, rebase, reset, clean, stash-drop, force-anything, branch-delete; live/prod ops; pins; frozen profiles; golden regen; `.gitignore` | Human approval, per operation |
| Secrets | Never handled in any model output |

The "or orchestrator directly" options above are ROI defaults, not free
choices: per `## ROI-First Delegation`, direct is the default for small /
low-risk work (log a `direct reason`); delegate/fan-out only when a trigger
fires.

Routing updates from outcomes (scoreboard in `.agent/memory/model-routing.md`):

- One failure → record it and fix the packet; bad-packet failures never demote
  the model.
- Two failures of the same task-class/model pair → demote that class one level
  (Haiku→Sonnet, Sonnet→Orchestrator) until a deliberately re-run probe passes.
- Widening a class's scope requires two clean successes at current scope plus
  one harder probe.

## ROI-First Delegation

Delegation must pay for itself. A subagent starts cold and its output is
re-reviewed by the orchestrator, so for small serial tasks delegation costs
MORE than doing it directly (see the net-win column in
`.agent/memory/model-routing.md`). Route by ROI, not by reflex.

**Default is direct.** A task is done directly — with a logged one-line
`direct reason` — when it is small / single-file / low-risk and the
orchestrator already holds enough context, and always for Tier-X, review, and
git.

**Consider a subagent only when an ROI trigger fires:**
- 3+ independent sub-tasks runnable in parallel → **fan-out** (saving: parallelism)
- large read-only exploration whose full context should stay out of the
  orchestrator → **delegate** (saving: context isolation)
- bulk same-shape mechanical edits large enough to amortize packet+review →
  **delegate** (saving: cheaper model)
- long-running test-writing / repeated fixes / batch validation → **delegate**
  (saving: long-running labor)
- work needing independent or adversarial review → **delegate** (saving: review
  separation)

**Model assignment (cheapest capable — this is where cost actually drops):**

| Work | Model |
|---|---|
| docs, counting, path verification, simple mechanical edits | Haiku |
| bounded code+test, test writing, read-only investigation, medium mechanical | Sonnet |
| orchestration, routing, review, git control, final acceptance | Opus (never delegated) |

Every task's intake announces one of {direct, delegate, fan-out, stop}, its ROI
reason, and — if delegating — the expected cost-saving type. Record the
realized net win in `.agent/memory/model-routing.md` after each delegation.

## Done Definitions (acceptance by task type)

| Task type | Done means |
|---|---|
| Docs-only | every referenced path proven to exist (`rg --files`); no invented behavior; drift marked [DRIFT]; diff reviewed |
| Test-only | new tests pass; break-probe shows they fail when the behavior is broken; `make test-hygiene-check` clean; no existing test weakened |
| Code+test | Test-only criteria + baseline-vs-after suite comparison; lint/format clean; typecheck clean in changed files; every hunk matched against the packet |
| Gate cleanup | gate red→green with zero behavior change; no new `type: ignore`/`noqa`; error count only shrinks |
| Investigation | every claim cited (file:line or pasted output); uncertainty explicit; `git status` unchanged |
| Git planning | written sequence + `branch-safety-check` output + per-step rollback + human-approval points marked; nothing executed |
| Memory update | routed per `.agent/memory/README.md`; no duplicates; absolute dates; no secrets; stale entries corrected, not appended-around |

No "passing/fixed/done" claim without pasted command output plus an explicit
list of checks NOT run.

## Handoff Packet (required for every delegation)

Canonical fill-in template: `.agent/skills/small-model-handoff/SKILL.md`.
Required fields:

1. Goal (1-3 sentences) + task type + tier + assigned model
2. Branch + expected `git status` + baseline facts (e.g. current test counts)
3. Files allowed to touch
4. Files explicitly off-limits (Do-NOT-Edit list pasted for Tier 2+)
5. Relevant gotchas (pasted, not linked; whole packet <= ~150 lines)
6. Constraints (laws that apply)
7. Verification commands (exact), each worded with the pre-existing-red escape
   hatch: "expect clean in files you changed; failures elsewhere = stop and
   report, do not fix"
8. Stop-and-escalate conditions (minimum set in the skill template)
9. Rollback note (executed by orchestrator only)
10. Execution venue: worktree by default; main tree when the built venv/test
    suite is required (allowlist + orchestrator snapshots + checkout rollback)
11. Precedence clause: general rule beats enumerated list; enumerations
    generated by a pasted command
12. Report contract + budget: 4-section report as final message; orchestrator
    reviews independently regardless; exceeding budget is a stop-condition

## Cross-Cutting Rules

- Every delegation runs in worktree isolation when it writes files
  (`.agent/rules/60-agent-workflow-governance.md`), except the main-tree venue
  in `small-model-handoff` when verification requires the built venv/test
  suite; parallel agents never share files.
- Executors report; orchestrator verifies; only verified results reach the user.
- Any agent that cannot complete honestly says so. Fabricated success is the
  worst possible output in this repo.
