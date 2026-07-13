# Open Questions (unresolved decisions)

Record here: unresolved decisions with what blocks them and who decides
(user/evidence/time). Do NOT record: questions answerable by reading source —
answer those instead. Move resolved items to architecture-decisions.md or
failed-attempts.md.

## Disk-only research modules — RESOLVED 2026-07-12/13 (fix batch, user-ordered)
The sweep ran: every module tracked CODE imports is now committed
(18 modules incl. dynamic load_module targets; see
architecture-decisions.md "Research modules imported by tracked code").
The "19 alphas.*.impl" importers turned out to be research/archive/**
snapshots of killed alphas referencing deliberately-deleted impls —
harmless, left as-is. REMAINDER RESOLVED 2026-07-13: user picked
move-to-legacy for the 7 untracked non-core tools-root scripts;
`python -m research.factory converge-tools` moved them to
research/tools/legacy/ and local research-audit-strict is 0/0.
Markdown-plan-referenced modules (t1_regime_partition trio,
tools/fixtures) stay untracked deliberately.

## Scheduled-CI red + deploy.yml startup failure — RESOLVED 2026-07-13 (user-ordered fix)
All four legs closed (commits 4242c7b0 / 46521afe / f86bf944 / 4a1d73d6):
(a) gitleaks — six findings adjudicated one-by-one, all placeholders/
identifiers, allowlisted by exact value; full-history scan 'no leaks
found'. (b) Recorder drills — two defects: stale replay-safety spec
asserting pre-588ebfbf skip behavior (now asserts the quarantine
contract; verify-ce3 8 passed) + summary-step here-docs with indented
terminators. (c) Benchmark Darwin Gate — NOT self-resolved after all:
the baseline auto-update on green pushes is a one-way runner-speed
ratchet (all 6 benchmarks '+26-52%' in lockstep on the next two pushes
with zero hot-path changes). Fixed in 2aa48ef3: comparisons normalized
by the median current/baseline ratio (runner shift cancels), plus a
+200% unnormalized catastrophic cap; validated against the real failed
artifact (1.40x shift → PASS). (d) deploy.yml —
secrets context in step-level `if:` made the file unparseable
(startup_failure, zero jobs); replaced with guard-step output + boolean
dry_run fixes.

## Production environment protection not configured (opened 2026-07-13)
deploy.yml's production job relies on a required-reviewer rule
(Settings → Environments → production) that does NOT exist server-side;
the agent's API creation attempt was permission-denied. Until configured,
deploy.yml auto-builds/pushes GHCR images on main CI success but cannot
touch any host: NO DEPLOY_*/STAGING_* secrets exist (verified 2026-07-13
via API, names only). Configure the environment BEFORE ever adding
deploy secrets. Decides: USER (one click, repo admin).

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
