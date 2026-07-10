# Agent System Institutionalization — 15-Point Design

- **Date**: 2026-07-10
- **Status**: APPROVED (Wave 1 implemented same day; Waves 2+ pending per-point activation)
- **Scope**: the whole development process — Agent System v2 (`CLAUDE.md` →
  `AGENTS.md` → `.agent/` rules/skills/memory), plus lightweight scripts/CI
  automation. No product/hot-path code.

## Context

Agent System v2 works: task intake is the mandatory entry point, delegation
runs through handoff packets with independent review, outcomes land in a
model-routing ledger, and commits pass a `--narrow-commit` allowlist gate.
But much of what makes it work is **oral tradition** — conventions carried by
session memory rather than enforceable institutions. Evidence gathered
2026-07-10:

- Governing-doc drift was caught by hand three separate times (stale runbook
  path in `40-ops.md`; two rounds of `docs/MODULES_REFERENCE.md` re-verification).
- Four generations of agent frameworks coexist in `.agent/` with no marker
  saying which is live.
- Agent-system edits (the ROI-first routing update) sat uncommitted across
  sessions, surviving only via memory handoff.
- ~37 local commits have no remote backup; 13 research validation dirs and
  ~20 test files were untracked.
- Widening probes, session wrap-up, and periodic audits existed only as
  intentions.

Each point below: **Evidence → Institution → Deliverable → Acceptance**.

## Theme A — Single source of truth for the knowledge base

### 1. `.agent/` lifecycle manifest — **DONE (b898352b)**
- Evidence: `agents/`, `commands/`, `pixiu/`, `rules/ecc/`, `teams/`,
  `workflows/opsx-*`, `contexts/`, `extensions/` coexist with v2; zero
  references from v2 docs for most of them.
- Institution: `.agent/00-MANIFEST.md` labels every subdirectory
  ACTIVE / DEPRECATED / ARCHIVE-CANDIDATE with a one-line judgement; new
  subdirectories require a row at creation; deletion/moves stay per-case
  user-approved.
- Acceptance: any cold-start agent can answer "which subsystem is live?" from
  one file; judgements cite reference-scan + tracked-state evidence.

### 2. Machine-checkable agent-docs consistency gate — **DONE (0aafd55e)**
- Evidence: three hand-caught drift incidents; `rg` without `--hidden`
  silently skipping `.agent/` hid one of them.
- Institution: `scripts/check_agent_docs.py` (`make agent-docs-check`, wired
  into `make check`) verifies referenced-path existence in governing docs,
  skills-dir ↔ `00-index.md` bidirectional alignment, and memory
  routing-table ↔ files alignment. Pre-existing drift lives in a ratchet
  baseline (`.agent/agent-docs-known-drift.txt`, 16 subjects at seed) that
  only shrinks; stale entries are flagged.
- Acceptance: planted drift turns the gate red (proven by unit test + live
  break-probe); current repo passes with the seeded baseline.

### 3. Governance change control
- Evidence: CLAUDE.md/AGENTS.md/rules changes are only discoverable by
  archaeology through `git log`.
- Institution: every governing-doc change commits as `docs(agents):` plus a
  one-line entry in a new `.agent/CHANGELOG.md` (date / files / why); changes
  that alter role authority or tier boundaries additionally require an ADR
  (`.agent/templates/ADR_TEMPLATE.md`).
- Acceptance: reading `.agent/CHANGELOG.md` reconstructs governance history
  without git archaeology.

## Theme B — Task pipeline (intake → delegate → review → commit)

### 4. Same-session commit rule for agent-system edits — **DONE (7ad864b1)**
- Evidence: the ROI-first 4-file diff survived only through session-memory
  handoff; a crash would have lost it.
- Institution: agent-system file edits are committed through the
  `--narrow-commit` gate in the same session that makes them; the wrap-up
  checklist (#13) audits for leftovers.
- Acceptance: `git status` shows no dirty agent-system files at session end.

### 5. Delegation archive (auditable packets + reports)
- Evidence: handoff packets and executor reports live only in dead session
  transcripts; ledger entries cannot be re-audited against their sources.
- Institution: each delegation stores its packet + final executor report at
  `.agent/memory/delegations/YYYY-MM-DD-<slug>.md`; the ledger entry links it.
- Acceptance: every new ledger entry resolves to an archived packet file.

### 6. Pre-registered widening probes — **DONE (93ddfb47)**
- Evidence: the two owed probes (Haiku multi-file mechanical; Sonnet ≥3-file
  cross-module) existed only as prose in old ledger entries.
- Institution: `model-routing.md ## Next probes` — entry conditions, success
  criteria, and failure handling written BEFORE the probe runs (research
  pre-registration discipline); outcomes convert to ledger entries.
- Acceptance: no class's validated scope widens without a pre-registered
  probe on record.

### 7. Checkpoint/resume convention for long tasks
- Evidence: long sessions compact; continuity depends on summary quality.
- Institution: task-intake gains a step — tasks expected to outlive a context
  window write a resumable block (done units / next step / verification
  state) to `.agent/memory/current_session.md` after each verifiable unit.
- Acceptance: a fresh session can resume a checkpointed task without re-derivation.

## Theme C — Verification and CI

### 8. Golden intake tasks (regression tests for governance itself)
- Evidence: nothing alerts when an AGENTS.md/task-intake edit breaks routing.
- Institution: repurpose `.agent/evals/` into 5–8 golden intake cases
  ("fix this skipped CLI test" → expected type/tier/route); run manually
  after any routing-relevant governance change; result recorded in the ledger.
- Acceptance: a routing-breaking edit is caught by a failed golden case
  before it misroutes a real task.

### 9. Validation matrix as a fill-in artifact
- Evidence: CLAUDE.md's Validation Requirements are prose; adherence is
  self-discipline.
- Institution: `commit-work` skill embeds a blast-radius checklist
  (docs-only / bugfix / hot-path / broker / merge) ticked before each commit;
  "checks NOT run" becomes a mandatory field.
- Acceptance: every commit report names its blast-radius row and lists
  skipped checks explicitly.

### 10. Research evidence commit cadence
- Evidence: 13 validation dirs + ~20 test files untracked; with no remote,
  untracked = zero backup.
- Institution: each research verdict (KILL / NEEDS-MORE-DAYS / RESCUED /
  INCONCLUSIVE) triggers an immediate narrow-gate commit of that candidate's
  `research/experiments/validations/**` + matching tests (append-only rules
  already apply); step added to `research-factory` skill.
- Acceptance: no verdict exists whose evidence is only on one disk.

## Theme D — Git and risk

### 11. Local backup institution (git bundle)
- Evidence: ~37 unpushed commits, no remote, single disk — the repo's largest
  single point of failure; push remains a human-approved red line.
- Institution: weekly (or every ~10 commits) `git bundle create` to a second
  location outside the repo; action + destination recorded in
  `current-risks.md` until a remote-backup decision lands. Destination
  requires Charlie's one-time approval before first run.
- Acceptance: a dated bundle exists whose commit range covers HEAD.

### 12. Branch-per-theme discipline
- Evidence: `docs/agent-knowledge-distillation` accumulated shioaji, ops,
  research, and agent-system commits — hard to review or roll back.
- Institution: new theme → new branch; `current_session.md` keeps a branch
  registry (purpose / expected lifetime); merges go through the existing
  review gates.
- Acceptance: no branch carries commits from more than one theme going forward.

## Theme E — Memory and learning loop

### 13. Executable session wrap-up
- Evidence: "update memory at session end" is prose convention, often skipped.
- Institution: extend `memory-update` skill into an explicit wrap-up
  checklist: `current_session.md` update → lessons routing → `current-risks`
  add/prune → `open-questions` in/out → dirty agent-system file audit (#4).
- Acceptance: session-end runs produce a checklist-complete memory state.

### 14. Dual-memory division of labor
- Evidence: repo memory and orchestrator-private memory have diverged
  (delegation ROI in repo; session state only private).
- Institution: `memory/README.md` gains a section: delegation outcomes and
  shareable lessons always land in repo memory (public-literature-only rule
  applies); private memory holds user preferences and cross-session context;
  wrap-up (#13) cross-checks both.
- Acceptance: no shareable fact exists only in private memory.

### 15. Periodic meta-audit
- Evidence: `.agent/reports/` holds one-off 2026-03 audits; never became a
  cadence.
- Institution: quarterly or every ~10 delegations, run a read-only audit
  variant over scoreboard trends, intervention rates, stale skills, and gate
  failures; report to `.agent/reports/` with at most 3 improvement actions,
  feeding back into this list.
- Acceptance: audit reports exist on cadence and each names ≤3 actions with
  owners.

## Rollout

| Wave | Points | Status |
|---|---|---|
| 1 | #1 (b898352b), #2 (0aafd55e), #4 (7ad864b1), #6 (93ddfb47), this spec | DONE 2026-07-10 |
| 2+ | #3, #5, #7, #8, #9, #10, #11, #12, #13, #14, #15 | Each activates on Charlie's explicit instruction; #11 additionally needs a backup destination decision |

Wave-1 verification evidence lives in the session record: gate runs
(staged-set == allowlist, exit 0 each), 10 passing behavior tests, live
break-probe with byte-exact restore, ruff/mypy clean on new files,
`make test-hygiene-check` clean.

## Non-goals

- No deletion or relocation of DEPRECATED `.agent/` generations (per-case
  user approval).
- No pre-commit config changes (enforcement infra is Do-NOT-Edit; the new
  gate rides `make check` only).
- No push, no remote creation, no live/production-facing changes.
