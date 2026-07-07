# Model Routing (tiers + observed delegation outcomes)

Record here: the operative tier table (mirrors AGENTS.md) and OBSERVED
outcomes — which delegations succeeded/failed by surface and why; packet
lessons. Do NOT record: generic model claims; single anecdotes (wait for a
2nd occurrence before writing a pattern).

## Scoreboard (input to AGENTS.md demotion/promotion rules)

| Model | Task type | Attempts | Successes | Interventions |
|---|---|---|---|---|
| Haiku 4.5 | docs/mechanical | 2 | 2 | 0 |
| Sonnet | Tier-2 code+test | 2 | 2 | 1 (report nudge; 0 this run) |
| Sonnet | Tier-1 docs verify | 1 | 0 (1 PARTIAL) | 2 (false-positive fixes) |

## Record schema (write after EVERY delegation, success or not)

```
### <date> · Tier <n> · <task type> · <surface> · <model> · <SUCCESS|PARTIAL|FAIL|ESCALATED>
Interventions: <n> · Cost: <tokens/time if known> · Net win vs doing directly: <y/n/unclear>
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
