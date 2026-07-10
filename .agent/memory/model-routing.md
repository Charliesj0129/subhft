# Model Routing (tiers + observed delegation outcomes)

Record here: the operative tier table (mirrors AGENTS.md) and OBSERVED
outcomes — which delegations succeeded/failed by surface and why; packet
lessons. Do NOT record: generic model claims; single anecdotes (wait for a
2nd occurrence before writing a pattern).

## Scoreboard (input to AGENTS.md demotion/promotion rules)

| Model | Task type | Attempts | Successes | Interventions | Net-win |
|---|---|---|---|---|---|
| Haiku 4.5 | docs/mechanical | 2 | 2 | 0 | 0/2 (capability probes) |
| Sonnet | Tier-2 code+test | 2 | 2 | 1 (report nudge; 0 this run) | 0/2 (capability probes) |
| Sonnet | Tier-1 docs verify | 1 | 0 (1 PARTIAL) | 2 (false-positive fixes) | 0/1 |
| Sonnet | Tier-2 alpha-research (append-only research/) | 2 | 2 | 0 (1 review-caught narrative overclaim, no code fix needed) | 2/2 (context-isolation, parallelism) |

Net-win = delegations that were cheaper/faster than doing it directly (not
capability probes). ROI-first routing (AGENTS.md `## ROI-First Delegation`)
exists to raise this column: delegate only when a trigger fires.

## Record schema (write after EVERY delegation, success or not)

```
### <date> · Tier <n> · <task type> · <surface> · <model> · <SUCCESS|PARTIAL|FAIL|ESCALATED>
Interventions: <n> · Cost: <tokens/time if known>
ROI: net-win <yes|no|unclear> · realized-saving <cheaper-model|parallelism|context-isolation|long-running-labor|review-separation|none> · would-delegate-again <y/n>
Archive: .agent/memory/delegations/<YYYY-MM-DD>-<slug>.md (packet + executor report + review verdict, verbatim)
Lessons: <=3 bullets, only if new.
```

Update the scoreboard in the same edit. Bad-packet failures: fix the packet,
don't demote the model. Lessons appearing twice → promote into the relevant
SKILL.md and replace with a pointer (see `memory-update` skill).

## Tier table (authoritative copy in AGENTS.md)
- Tier 1 (docs/comments/test-only/scratch): Haiku/Sonnet executor, Sonnet review.
- Tier 2 (non-hot-path src, CLI, reports, ops scripts): Sonnet executor,
  Sonnet review + Fable spot-check.
- Tier 3 (hot path, contracts/events, pricing/timebase, broker adapters,
  risk/order/execution/gateway, recorder/WAL, Rust, migrations, alpha
  governance, Do-NOT-Edit list): tight packet or Fable directly;
  Fable/Opus review MANDATORY.
- Tier X (live/prod ops, git surgery, secrets, dependency pins, frozen
  registry/profiles): never delegated; Fable + explicit user confirmation.

## Next probes (pre-registered — run as written, then convert to a ledger entry)

Discipline borrowed from research pre-registration: entry conditions, success
criteria, and failure handling are written BEFORE the probe runs and are not
adjusted afterward to flatter an outcome. A probe that cannot run as written
is re-registered, not bent.

### P1 · Haiku 4.5 · docs/mechanical · widen single-known-target → multi-file find-and-fix-ALL
- Why owed: class is 2/2 clean at single-known-target scope (2026-07-06,
  2026-07-07); the AGENTS.md widening rule requires one harder probe before
  the class's validated scope grows.
- Entry: a REAL (not manufactured) mechanical task spanning >=2 files, or a
  find-and-fix-ALL sweep where the executor must enumerate targets itself from
  a pasted command; Tier-1 surfaces only.
- Success: every target found (orchestrator holds a privately pre-computed
  target list as the answer key); zero scope drift; zero interventions;
  report contract honored.
- Failure handling: one failure → packet lesson only, scope unchanged; second
  failure at this scope → demote the class (Haiku→Sonnet) per AGENTS.md until
  a deliberately re-run probe passes. Bad-packet failures never demote.

### P2 · Sonnet · Tier-2 code+test · widen <=2-file single-function → multi-file / cross-module
- Why owed: class is 2/2 clean at <=2-file single-function scope (2026-07-06,
  2026-07-08); the intended harder probe (governor CLI wiring, 2026-07-08)
  ended BLOCKED-BY-HARNESS (plan mode), so the widening probe is still unrun.
- Entry: a REAL Tier-2 change touching >=3 files or crossing a module boundary
  with real callers; non-hot-path; session verified NOT in plan mode before
  spawn; full 12-field packet; break-probe validation planned at intake.
- Success: behavior lands with focused tests; break-probe fails on the buggy
  baseline and passes after; zero unauthorized files touched; at most one
  intervention and it is not a code fix.
- Failure handling: same two-failure demotion rule (Sonnet→orchestrator for
  this class); BLOCKED-BY-HARNESS records separately and never counts against
  the class.

## Observed outcomes
(Each entry: date, tier, surface, executor model, outcome, packet lesson.)

### 2026-07-06 · Tier 1 · docs/mechanical · MODULES_REFERENCE.md count re-verification · Haiku 4.5 · SUCCESS
Interventions: 0 · Cost: ~69K tokens / 3 min · Net win vs doing directly: no (capability probe)
Pilot delegation via small-model-handoff → worktree-isolated executor →
strict-code-review. Executor corrected 17 numeric claims; every number matched
the orchestrator's independently pre-computed ground truth; scope held (1 file,
prose untouched, no git commands, zero escalations); ~69K tokens / 81 tool
uses / ~3 min. Review verdict APPROVE with no diff findings.
Packet lessons:
- The packet's hand-typed "rows to check" enumeration omitted one row (`core`);
  the executor correctly let the general rule ("every row with a bold count")
  win and disclosed the extra edit. → Generate enumerations from commands, and
  state precedence explicitly: general rule beats enumerated list.
- Giving exact count COMMANDS (not answers) worked: deterministic for the
  executor, still independently checkable by the reviewer. Reuse this shape for
  any mechanical-verification task.
- One data point only — do not generalize to Tier-2 code tasks yet; next pilot
  should be a Tier-2 non-hot-path code+test change (Sonnet executor).

### 2026-07-06 · Tier 2 · code+test · CLI un-skip live→sim downgrade regression test · Sonnet · SUCCESS
Interventions: 1 (report-delivery nudge) · Cost: not measured · Net win vs doing directly: unclear (capability probe)
Real recorded debt: `test_cmd_run_downgrades_live` skipped since the
prometheus-mock era, leaving the fail-safe live→sim credential downgrade with
zero regression coverage. Packet prescribed an extraction-only refactor in
`cli/_run.py` + helper-level tests. Executor delivered exactly that: 2 files,
byte-identical warning string, 3 behavior-named tests, single-seam
monkeypatch, no sys.modules stubbing. Verification: 22 passed/0 skipped on
the file; full `make test` 14013 passed, coverage 87.77%; ruff/format/
hygiene/discipline/boundary all green; orchestrator break-probe confirmed the
new tests fail when the downgrade condition is broken. Executor correctly
ESCALATED (not fixed) 2 pre-existing mypy `unused-ignore` errors in off-limits
shioaji files — exactly the stop-condition behavior the packet asked for.
Packet lessons:
- Worktree isolation conflicts with tasks that must RUN the test suite: a
  fresh worktree lacks the built venv/Rust artifacts. Pattern that worked:
  main-tree execution + 2-file allowlist + orchestrator before/after
  `git status --porcelain` snapshot + hash-verified rollback plan. (Skill
  updated with this caveat.)
- Background-spawned executors can go idle without delivering their report;
  nudge via SendMessage, but never block review on the self-report — the
  orchestrator's independent diff/validation found the same facts first.
- "Expect clean" verification commands need a pre-existing-red escape hatch:
  the packet's "make typecheck (expect clean)" was unsatisfiable on a branch
  with unrelated debt; the stop-condition ("errors in files you did not
  change") saved it. Word future packets that way from the start.

### 2026-07-07 · Tier 1 · docs verify · MODULES_REFERENCE.md class/file-identifier re-verification · Sonnet · PARTIAL
Interventions: 2 (orchestrator removed 1 false-positive marker; a 2nd false positive survived review, removed post-run by meta-evaluator) · Cost: ~148K tokens / 38 tool uses / ~13 min · Net win vs doing directly: unclear (capability probe)
Second pilot on this doc after the 2026-07-06 Haiku count pass. Packet: verify
every col2/col3 class & file identifier against src/, append additive
`[DRIFT: nearest-actual]` markers, never rewrite prose or counts; main-tree
venue, 1-file allowlist, orchestrator hash-snapshot + checkout rollback.
Executor marked 34 tokens; 32 correct and evidence-backed — all ten "stale
.pyc, source deleted" claims verified true; nearest-names accurate;
case-mismatch (AlertmanagerBridge) and function-based-module (heartbeat.py,
_renderer.py, _tui.py, facts.py) distinctions all right; correctly resolved
`LoadGenerator`/`ShadowRunner` to load_generator.py/shadow_runner.py as PRESENT
and `scenario_rules` as absent. Diff purely additive (18 in-place line appends
+ provenance comment), table intact (38 rows still NF=5), zero count/prose
edits, no git commands, and it correctly flagged 2 concurrent-user files
(shioaji session_runtime + its test) without touching them. TWO false
positives, both live Rust `#[pyclass]` identifiers with no Python class/file —
the SAME pattern it correctly kept for `RustPositionTracker`:
`LobFeatureKernelV1` (rust_core/src/feature.rs, registered lib.rs:59, pulled in
via `getattr(_rust_core, "LobFeatureKernelV1")` at feature/engine.py:32),
caught and removed by orchestrator review; and `ShmSnapshotTable`
(rust_core/src/shm_snapshot.rs:42, registered lib.rs:61, imported at
ipc/shm_snapshot.py:57), MISSED by review even though the registration sits two
lines below the lib.rs:59 line the review itself cited — caught post-run by
meta-evaluation against hidden ground truth, removed 2026-07-07 (final 32).
Graded PARTIAL: chain ran end-to-end with scope/git discipline intact, but
review did not catch all errors. Orchestrator did NOT commit
(`check_git_preconditions.sh --pre-merge` BLOCKED on the always-present dirty
user tree — gate/authority contradiction, see open-questions); committed
post-run by Fable via path-scoped staging with explicit user approval.
Lessons:
- Rust/PyO3 repos: the packet must state that identifiers defined as
  `#[pyclass]` in rust_core and imported via `getattr(_rust_core, "Name")` are
  PRESENT, not drift, and list the getattr seam. Sonnet applied this to one
  such token (RustPositionTracker) but not the structurally-identical
  LobFeatureKernelV1/ShmSnapshotTable — give the rule explicitly so it is
  applied uniformly.
- Pre-compute the orchestrator answer key with the SAME "present if class OR
  file" rule handed to the executor, covering ALL definition sources
  (rust_core included). A stricter class-only key falsely flagged
  LoadGenerator/ShadowRunner, and a src/hft_platform-only review scope let the
  second Rust-backed false positive through. A mismatched or under-scoped key
  wastes review cycles and misses errors.
- Additive `[DRIFT: nearest-name]` marking gives a trivially reviewable diff,
  but cannot annotate a stale row that has no backtick identifier (the
  `scripts/` row) — those need a separate note, not an inline marker.

### 2026-07-07 · Tier 1 · docs/mechanical · .agent/rules/40-ops.md stale runbook-path fix · Haiku 4.5 · SUCCESS
Interventions: 0 · Cost: ~33K subagent tokens / 7 tool uses / ~37s · Net win vs doing directly: no (one-line edit; run as a capability + full-cycle probe)
Genuine drift found by a docs-consistency scan: `.agent/rules/40-ops.md:7`
pointed live-impacting config work at `docs/ops_change_control.md`, which does
not exist — the runbook moved to `docs/operations/change-control.md` (confirmed
by `docs/README.md:41` and the doc title). Packet gave the exact target path +
read-only verification commands; orchestrator pre-computed the byte-exact
post-edit blob hash (`055e6a69…`) as the answer key. Executor changed exactly
line 7, scope held (1 file, 39 dirty user files byte-identical, no git, no
touch of the `.claude/` mirror copies), all verification green. Review: blob
hash matched the answer key exactly; APPROVE. Committed locally via the new
`--narrow-commit` SAFE-WITH-CARE gate (commit 74d95e06; not pushed).
Lessons:
- Second clean Haiku docs/mechanical run (now 2/2, 0 interventions). Per the
  AGENTS.md promotion rule this class now has its two clean successes at
  current scope; the next probe to widen it should be harder (multi-file or
  find-and-fix-ALL mechanical, not a single known-target replacement).
- First end-to-end use of the `--narrow-commit` gate (added e6273d15) resolved
  the 2026-07-07 gate/authority deadlock: staged-set==ALLOWED_PATHS with the
  dirty tree demoted to an informational warning let the orchestrator complete
  a real local commit and exit 0. Promote to successful-patterns on a 2nd use.
- `.agent/` is a normal dir, but `rg` skips it without `--hidden` (dot-prefixed);
  a drift scan of agent-rules files that omits `--hidden`/an explicit path
  silently misses references — it hid this very drift on the first pass.

### 2026-07-08 · Tier 2 · code+test · CLI `_safe_write` empty-dirname crash fix · Sonnet · SUCCESS
Interventions: 0 · Cost: ~54K subagent tokens / 17 tool uses / ~86s · Net win vs doing directly: no (small ~4-line fix; run as a full-cycle capability probe)
Real latent defect on a preferred surface: `cli/_utils.py:_safe_write` did
`os.makedirs(os.path.dirname(path), exist_ok=True)`, which raises
`FileNotFoundError: ''` when `path` is a bare filename (dirname == ""). The
helper's only test coverage (`test_cli_smoke.py`) MOCKED `_safe_write`, so the
real function was uncovered. Packet: main-tree venue, 2-file allowlist
(src fix + new `tests/unit/test_cli_safe_write.py`), orchestrator pre-edit hash
snapshot (965213bd) + `git checkout` rollback, defect + required behavior
specified, escape-hatch-worded verification. Executor delivered exactly the
minimal guard (`dirname = os.path.dirname(path); if dirname: os.makedirs(...)`,
signature unchanged, with-dir behavior byte-identical) + 3 behavior-named
`tmp_path`-isolated tests (bare filename via `monkeypatch.chdir`, nested-parent
creation, existing-dir overwrite). Independent review: scope-diff showed ONLY
the 2 files changed vs the 39-file dirty baseline (mtimes confirmed no user file
touched); re-ran all verification (new 3 passed, smoke 5 passed, ruff
check+format clean on both files, mypy clean 120 files, hygiene clean);
break-probe via `git checkout` to the committed buggy version made the
bare-filename test fail with the exact `FileNotFoundError: ''`, then cp-restored
the fixed snapshot byte-exact (hash 39842bdc re-verified). `make format-check`
red was entirely pre-existing (7 committed-debt files + 1 user-dirty
`test_research_factory.py`, none an executor file) — executor correctly applied
the escape hatch. Committed locally via `--narrow-commit` gate (commit 4388f24e,
exit 0, staged set == 2 allowlist files; not pushed); dirty tree preserved at 39.
Lessons:
- Second clean Sonnet Tier-2 code+test success (now 2/2 at current scope; the
  one historical intervention was a report-delivery nudge, not a code fix). Per
  the AGENTS.md promotion rule this class has its two clean successes at current
  scope — the next probe to widen it should be HARDER: multi-file (>2) or a
  cross-module behavioral change with real callers, not another single-function
  localized fix.
- Running the executor SYNCHRONOUSLY (`run_in_background:false`) for one tight
  task eliminated the prior run's idle-executor / missing-report problem — the
  4-section report arrived with the tool result. Reserve background+nudge for
  parallel or long-running executors.
- Break-probe shortcut for a single-file bugfix where the pre-fix code == the
  committed baseline: snapshot the fixed file, `git checkout -- <file>` to get
  the buggy version for free (no hand-editing → zero transcription risk), run
  the new test (expect fail), then cp the snapshot back and re-verify the blob
  hash. Clean and reusable.

### 2026-07-08 · Tier 2 · code+test · governor CLI wiring (plan Task 5) · Sonnet → orchestrator-direct · BLOCKED-BY-HARNESS (not a model failure)
Interventions: n/a (executor never got to edit) · Cost: 2 spawns ≈120K subagent tokens wasted
Intended as the class's harder widening probe (cross-module CLI wiring vs
prior single-function fixes). Both Sonnet spawns were blocked: the SESSION was
still in plan mode and subagents inherit it as system-enforced — packet-level
"EXECUTE mode" wording and Agent-tool `mode: acceptEdits` did NOT override it.
Both executors behaved correctly (read-only pre-flight, wrote a plan, stopped;
zero unauthorized edits) — so this records NO failure against Sonnet Tier-2
code+test (scoreboard unchanged at 2/2; widening probe still owed).
Orchestrator implemented directly per plan Task 5: TDD red
("invalid choice: 'governor'") → green (3 new + 8 existing = 11 passed; full
candidate_loop suite 354 passed; ruff check+format clean). Commit 483f7cba via
--narrow-commit (staged set == 2 files; 32-file dirty user tree preserved).
Lessons:
- Check session permission mode BEFORE spawning executors: in plan mode,
  subagents are read-only regardless of packet wording or Agent-tool mode
  param. Exit plan mode first, then delegate.
- A blocked-by-harness outcome must not demote the class; record it separately
  from capability failures.

### 2026-07-10 · Tier 2 · alpha-research code+test · T1-G normal_2_5×extreme_low_imbalance_reversal×30m sub-cell OOS-extension rescue attempt · Sonnet · SUCCESS
Interventions: 0 code fixes; 1 review-caught narrative overclaim (see Lessons) ·
Cost: 1 background spawn, single pass, no escalation
ROI: net-win yes · realized-saving context-isolation (650-line diagnostic.py +
2 sibling pipelines the orchestrator had not read this session, parallel with
an unrelated second delegation) · would-delegate-again yes
Packet: append-only allowlist (new JSON outputs via existing frozen CLIs'
--months/--in-path/--out-path flags, one new small analysis script, one new
test file), main-tree venue (research/data/ gitignored, worktree would lack
raw L2), explicit instruction that a data-starved null finding is a complete
valid answer, not something to force. Executor's own first step correctly
traced the plan's cited "N=17 IS / N=2-4 OOS" claim to its exact JSON source
before touching anything.
Independent orchestrator re-verification (script re-run, pytest re-run, mypy
re-run, git-status diff against pre-spawn file listing) matched the self-
report on every substantive number: target cell stuck at N=20 full-sample /
N=2 OOS-dated after exhausting every available contract-month; G6 contributed
zero decision rows (TMFG6 only 6 dates on disk, TXFG6 1-424 ticks/day, below
the pipeline's own min_ticks_per_window=20 gate); rescue rule mechanically
returns "RESCUED" but the executor correctly flagged this as a threshold
artifact (no N-floor on the OOS stage) and reported the honest verdict as
still-data-starved rather than taking the rule's literal output at face value.
Zero off-limits files touched (diagnostic.py/regime_review.py/etc. untouched;
only ran via their existing CLIs).
Lessons:
- One overclaim survived self-report but not orchestrator re-verification: the
  executor said the new D6/E6/F6/G6 diagnostic run was "byte-for-byte
  identical" to the prior D6/E6/F6 run. It was not — 17 more decision rows
  appeared (589->606, TXFF6 200->217) because more F6 raw data had landed on
  disk since the prior run, not because of G6 (which genuinely added 0 rows,
  that part was correct). The orchestrator's independent JSON diff caught
  this; it did not change the target-cell verdict (confirmed identical: N=20,
  mean 27.55 across all three iteration files) but is a reminder that
  "identical" claims about large JSON artifacts need a real diff, not a
  by-inspection read, before they're forwarded to the user.

### 2026-07-10 · Tier 2 · alpha-research code+test · of_vwap_dev_luxalgo vwap_fade bear/high-vol regime OOS test · Sonnet · SUCCESS
Interventions: 0 · Cost: 1 background spawn, single pass (ran the confirmatory
script 3x itself for stability, ~110s on the slowest run due to unrelated
host CPU contention from a live production process — noted, not a bug)
ROI: net-win yes · realized-saving context-isolation (indicator.py +
of_vwap_backtest.py + of_vwap_qa_backtest.py the orchestrator had not read
this session, run in parallel with the unrelated T1-G delegation above) ·
would-delegate-again yes
Packet: append-only allowlist, main-tree venue (research/data/ gitignored),
explicit genuineness-guard requirement (must not force a "bear"/"high-vol"
label onto data that isn't actually bearish/volatile — a null finding is a
complete valid answer). Executor built the guard as a real statistical check
(bottom-quartile actually negative, top-quartile actually >=1.25x median),
with two of its 11 tests specifically asserting the guard refuses to mislabel
all-bullish/flat-dispersion synthetic data.
Independent orchestrator re-verification (script re-run x1 with byte-identical
output vs the self-report, pytest re-run, cross-check of every reported
number against the existing of_vwap_qa_5m_day.json baseline via direct JSON
read, mypy-scope claim checked against pyproject.toml's [tool.mypy] files
list) matched the self-report exactly on every number — zero discrepancies,
unlike the T1-G delegation above. git status / file-listing diff confirmed no
off-limits file touched.
Substantive finding (see [[of_vwap_dev_luxalgo_backtest_2026_06_09]] for
full detail): a genuine bear regime exists in this data (previously assumed
absent across this whole research program) but the candidate's edge is
regime-specific to the bull tape it was found in — flips to -49pt/trade in
the bear window, no robust standalone edge in high-vol either. This was the
program's last live, unrefuted lead; now closed.
Lessons:
- Second consecutive clean parallel alpha-research delegation (now 2/2 at
  this scope, one with zero discrepancies, one with a caught-but-non-fatal
  narrative overclaim) — the packet shape (append-only allowlist + main-tree
  venue for gitignored data + explicit "a null finding is a valid complete
  answer" instruction + reuse-existing-frozen-functions-verbatim) is working
  and worth reusing as the template for future alpha-research delegations
  rather than re-deriving packet structure each time.
- Running two independent, unrelated alpha-research delegations in parallel
  (this one + the T1-G sub-cell rescue) cost no extra orchestrator overhead
  beyond writing two packets instead of one, and both landed clean — parallel
  dispatch is the right default when the tracks are genuinely independent,
  not just running them serially through the same context.
- Executor correctly distinguished a rule's mechanical output ("RESCUED") from
  its statistical meaning (N=2 on the confirming stage) and reported the
  latter as the real verdict — this is the behavior CLAUDE.md's honest-
  progress-reporting rule asks for, and it happened without being explicitly
  re-prompted for it (the packet asked for it once, generically). Worth
  keeping this framing in future packets for the same class of task ("a
  rule passing is not the same as a rule being meaningful — report which one
  you're claiming").
