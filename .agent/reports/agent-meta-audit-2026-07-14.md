# Agent Meta-Audit — 2026-07-14 (first report)

Skill: `.agent/skills/agent-meta-audit/SKILL.md`. Author: orchestrator (Fable),
direct. Inputs read: `.agent/memory/model-routing.md` (full ledger),
`.agent/memory/delegations/`, `.agent/CHANGELOG.md`,
`.agent/agent-docs-known-drift.txt`, `make agent-docs-check` output,
`.agent/memory/current_session.md`, `.agent/memory/open-questions.md`, git log.

## Cadence

- Prior `agent-meta-audit-*.md` report: **none** (the 2026-03-22 files in
  `.agent/reports/` are src security audits from a pre-v2 generation).
- Ledger entries since: **10** (9 delegations + 1 golden-intake baseline run),
  2026-07-06 → 2026-07-13. Threshold (~10) met → audit runs. This report sets
  the baseline for future trend comparison.

## Scoreboard trend

First report → absolute baseline, no trend line yet. From
`model-routing.md` scoreboard + ledger:

| Class | Record | Interventions | Net-win | State |
|---|---|---|---|---|
| Haiku 4.5 · docs/mechanical | 2/2 SUCCESS | 0 | 0/2 (probes) | validated at single-known-target scope; widening probe P1 **owed since 2026-07-10, unrun** |
| Sonnet · Tier-2 code+test | 2/2 SUCCESS (+1 BLOCKED-BY-HARNESS, excluded) | 1 (report nudge) | 0/2 (probes) | widening probe P2 **owed since 2026-07-10, unrun** |
| Sonnet · Tier-1 docs verify | 0/1 (PARTIAL) | 2 (false-positive fixes) | 0/1 | needs a packet-fixed re-run before the class is trusted |
| Sonnet · Tier-2 alpha-research | 2/2 SUCCESS | 0 code fixes (1 review-caught overclaim) | **2/2** | only class with realized ROI (context-isolation + parallelism) |
| gpt-5.6-sol · Tier-3 plan/spec review | 0/1 FAIL | 1 (no response) | 0/1 | do not re-attempt with the same packet shape (ledger 2026-07-13) |

Observation: net-win has materialized **only** where delegation exploited
parallelism + context isolation on work the orchestrator had not already
loaded (alpha-research 2/2). Capability probes cost more than direct work by
design; ROI-first routing (#4) is pointing the right way.

## Intervention rate

8 non-blocked delegations; **4 needed some orchestrator correction (50%)**,
but **0 were code fixes** — every intervention was honesty/delivery-shaped:

- Report/verdict non-delivery: 2026-07-06 CLI test (idle background executor,
  SendMessage nudge) and 2026-07-13 review FAIL (~734K tokens, no verdict).
  **2 occurrences → promote-to-skill threshold reached** (ledger's own rule,
  `model-routing.md` line 33).
- Overclaim / false positives caught only by independent review: 2026-07-07
  docs verify (2 false positives, 1 survived review to meta-eval) and
  2026-07-10 T1-G ("byte-for-byte identical" claim refuted by a real JSON
  diff — 589→606 rows). **2 occurrences → promote-to-skill threshold reached.**

Also charged here: 2026-07-08 BLOCKED-BY-HARNESS (~120K tokens, 2 spawns) —
prevention (plan-mode check) already landed in task-intake §6.

## Stale skills / drift

- `make agent-docs-check` (2026-07-14): **0 errors, 12 tolerated known-drift,
  0 stale baseline entries** — gate green, exit 0.
- Ratchet direction: 16 unique subjects at the 2026-07-10 snapshot → 12 now
  (#10 fix, commit fe62fbe4). Shrinking = working. However, the 9 lines under
  "Genuine drift: stale references awaiting fix" are unchanged since
  2026-07-10 — no consumer of the ratchet has picked one off yet. Watch item,
  not yet a finding (4 days old).
- Delegation-archive schema compliance: the two 2026-07-10 alpha-research
  ledger entries carry **no `Archive:` field and no archive file**; only
  2026-07-13 complies. README forbids memory backfill, so this is prospective
  only: every future ledger entry must carry the field (schema already
  requires it; it was skipped the same day #5 landed).

## Gate failures

Window 2026-07-06 → 2026-07-13 (v2 inception → now). Each item = a gate that
fired late, was bypassed, or sat dormant-broken:

1. **Commit landed while a required gate was red**: 7d3b2475 shipped with
   agent-docs-check red (branch-name-in-backticks false positive); caught
   same session (`.agent/CHANGELOG.md` 2026-07-11 #12-fix entry). Root cause
   recorded: gate exit code read through a pipe. Prevention already in memory;
   not yet in any SKILL.md checklist.
2. **Scheduled gate red for ~2.5 months unnoticed**: replay-safety spec
   (verify-ce3) red since 2026-04-27, found+fixed only 2026-07-13 (46521afe;
   `current_session.md`). Nothing in the agent system looks at scheduled-CI
   status on any cadence — session-start reads cover git state and memory,
   not workflow health.
3. **Self-corrupting gate**: benchmark Darwin Gate's baseline auto-update was
   a one-way runner-speed ratchet; failed two consecutive pushes in lockstep
   ('+26–52%' on all 6 benchmarks, zero hot-path changes) before diagnosis
   (2aa48ef3, validated on the real failed artifact).
4. **Dormant-broken workflow**: deploy.yml unparseable (secrets in step-level
   `if:` → startup_failure, zero jobs) — a gate that never executed could not
   fail loudly; two more dormant defects (GHCR case bfe255d9, trivy
   ignore-unfixed 70845b3d) surfaced only on the first real execution. Note:
   deploy's step order is push-then-scan — the trivy gate never blocked
   publishing (recorded in `current_session.md`; design decision owed to
   Charlie, routed to open-questions if pursued).

Common shape of 2–4: **gates that don't run in front of the agent don't get
noticed by the agent.** The system audits its own docs/commits but has no
cadence hook for external gate health (scheduled workflows, deploy runs).

## Prior actions follow-up

None — first report.

## Actions (max 3: owner + done-condition)

1. **Promote the two 2-occurrence delegation lessons into skills** (they hit
   the ledger's own promote threshold).
   - (a) `small-model-handoff`: independent/adversarial **review** packets
     must be scoped to the actual diff + a named small rule set, with a hard
     report deadline; single bounded tasks run synchronous
     (`run_in_background: false`); background executors owe SendMessage
     delivery — no verdict means the delegation records FAIL and the
     orchestrator reviews directly (2026-07-13 evidence).
   - (b) `strict-code-review`: any executor claim of "identical /
     byte-for-byte / unchanged" on a large artifact is accepted only with a
     real diff command + output in evidence (2026-07-07 + 2026-07-10).
   - Ledger lessons then replaced with pointers per `memory-update`.
   - Owner: orchestrator (this session, workstream C).
   - Done: both SKILL.md files updated; `make agent-docs-check` green;
     ledger pointer-swap committed through the narrow gate.
2. **Bind the role contracts to the harness** so the boundaries that failed
   soft (reviewer non-delivery; plan-mode token burn) are tool-enforced:
   `.claude/agents/hft-{executor,reviewer,test-writer,docs}.md` (reviewer
   gets no Edit/Write tools) + `.claude/settings.json` ask-rules on
   Do-NOT-Edit paths and destructive git.
   - Owner: orchestrator (this session, workstream B).
   - Done: 4 agent defs + settings committed; one hft-reviewer smoke spawn
     returns a correctly-formatted read-only verdict.
3. **Give scheduled/external gate health a cadence hook**: add a
   session-start check to the `read-only-audit` skill — `gh run list` over
   scheduled workflows + last deploy run, red runs reported before planning
   any change (prevents another 2.5-month silent-red).
   - Owner: orchestrator (this session, workstream C).
   - Done: `read-only-audit` SKILL.md updated with the exact command(s);
     first execution logged in the session record.

Findings without an action slot (carried, not dropped): P1/P2 widening probes
still owed — run on the next REAL matching tasks, not manufactured ones
(pre-registered in `model-routing.md`); archive-field compliance is
prospective-mandatory; trivy scan-after-push ordering is Charlie's call.
