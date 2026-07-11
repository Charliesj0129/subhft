# Open Questions (unresolved decisions)

Record here: unresolved decisions with what blocks them and who decides
(user/evidence/time). Do NOT record: questions answerable by reading source —
answer those instead. Move resolved items to architecture-decisions.md or
failed-attempts.md.

## Disk-only research modules — RESOLVED 2026-07-12 (fix batch, user-ordered)
The sweep ran: every module tracked CODE imports is now committed
(18 modules incl. dynamic load_module targets; see
architecture-decisions.md "Research modules imported by tracked code").
The "19 alphas.*.impl" importers turned out to be research/archive/**
snapshots of killed alphas referencing deliberately-deleted impls —
harmless, left as-is. REMAINDER (decides: USER): 7 untracked non-core
scripts at research/tools root (pdq_full_routing_audit,
pdq_liquidity_recovery_exit_audit, pdq_supertrend_backtest,
pdq_supertrend_ga_search, pdq_supertrend_gate_ga_search, pdq_visual_atlas,
pdq_wrongway_decomposition) — imported by nothing tracked, but they trip
make research-audit-strict LOCALLY on the primary machine (clean clones
green); commit-into-CORE_TOOL_FILES / move-to-legacy / delete is Charlie's
call. Markdown-plan-referenced modules (t1_regime_partition trio,
tools/fixtures) stay untracked deliberately.

## Scheduled-CI red + deploy.yml startup failure (opened 2026-07-12)
Nightly scheduled CI on main fails in 3 jobs (push runs green):
(a) Security Scan at "Secret scanning (gitleaks)" — generic-api-key hits
in tests/docs (test_credential_scrubber.py, test_order_adapter_dedup.py,
runbooks, plan docs); look like placeholder keys needing a reviewed
.gitleaks.toml allowlist, but each finding needs eyes before allowlisting.
(b) Recorder CK + WAL Drills (schedule-only job) fails in the CE2/CE3
drill step — uninvestigated. (c) Benchmark Darwin Gate regression check
fails on schedule, passes on push — likely runner variance/baseline.
Separately .github/workflows/deploy.yml ends "failure" with ZERO jobs in
~0s on every main push (startup_failure; its workflow_run/environment
config never starts). Decides: USER (security adjudication for (a),
investigation priority for (b)-(d)).

## MODULES_REFERENCE.md description staleness (narrowed 2026-07-07)
COUNTS RESOLVED 2026-07-06; CLASS/FILE IDENTIFIERS RESOLVED 2026-07-07
(Sonnet pilot, see model-routing.md): every col2/col3 class & file token
re-verified against src/; 32 stale tokens marked `[DRIFT: nearest-actual]`
inline (the 2026-04-01 scan predated large renames — options/reports/tca/
backtest class lists — and deleted feature/execution/data_quality/config/core
sources; 2 initial false positives on Rust `#[pyclass]` identifiers were
removed in review/meta-review, outcome PARTIAL). Doc provenance comment
updated to say identifiers verified, prose not.
REMAINING (all defer to USER — curation/design, not mechanical):
(a) Responsibility PROSE beyond identifiers still un-re-verified (e.g.
"5 mixins", "7-step dispatch", "IC=+0.116", service/queue counts);
(b) whether to REGENERATE the class lists from source vs keep the 32 [DRIFT]
annotations (a doc this drifted arguably wants regeneration, which is a
curation choice, not a mechanical pass);
(c) undocumented packages healing/ infra/ replay/ and the stale `scripts/`
row (references a src/hft_platform/scripts dir that does not exist; no backtick
token there to annotate). Decides: USER.

## git-bundle backup destination (opened 2026-07-11)
Institutionalization #11 tooling landed (`make git-bundle-backup DEST=...`,
commit 1a973302) but the FIRST RUN is blocked: the destination must be an
existing directory outside the repo that Charlie controls (second disk /
mount — never a synced or public location). One-time approval, then runs
record themselves in current-risks.md. Decides: USER.
Downstream (user decision 2026-07-11 round-2 cleanup): the 12 `archive/*`
tags each hold unpushed lineages (1–1917 commits) and stay untouched until
the first bundle run captures all refs; prune them in a later round.

## OrderIntent §7 parity producer fields (opened 2026-06-03)
Live parity for session/risk/force-flat dimensions needs a producer-side
OrderIntent change plus a future `hft.order_intents` ClickHouse migration —
ruled blocked_by_scope. Decides: USER. Partial fix landed
(`session_phase` stamped in runner phase filter).

## shioaji upgrade end-state — RESOLVED 2026-07-08: retarget to 1.5.5
Charlie decided to retarget the held 1.5.3 dual-version migration to 1.5.5
(dependabot #376 supersedes the old 1.5.4 question). Evidence: surface diff
1.5.3→1.5.5 = SAFE (0 breaking; 10 timeout defaults 5000→30000 ms), so #371's
adapter work carries over unchanged — see the retarget assessment in
docs/runbooks/shioaji-version-diff.md. REMAINING (execution, not decision):
re-run validation harness + soaks against 1.5.5; pin change stays
human-approved after harness green. The diverged local
chore/shioaji-153-validation-harness ref was retired 2026-07-11 (#371 and
#376 both CLOSED on GitHub; all its commits contained in pushed refs).

## Old-PC `.hft-runtime` heartbeat PermissionError (opened 2026-06-03, carried 2026-07-11)
Minor ops debt from the old-PC upgrade: heartbeat file writes fail with
PermissionError until `.hft-runtime` is chown'd 1000:1000 on the host.
One-line host fix, needs hands on the box. Decides: USER (host access).
