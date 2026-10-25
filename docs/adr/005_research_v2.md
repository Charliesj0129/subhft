# ADR-005: Research v2 — one judge, many generators

## Status
Accepted (2026-10-23). Builds on ADR-003 and ADR-004. Changes no gate threshold or floor.

## Context
The research program grew several independent programs (paper alphas, combinatorial mining,
the LLM candidate loop, calibration), each with its own data reader, day boundary, split, cost
model, metrics and verdict logic. The shared gates in `src/hft_platform/alpha/` report only a
boolean (`SubGateResult.passed` in `src/hft_platform/alpha/_sub_gates/registry.py`), so a
gate cannot say "no edge", "not enough sample" and "not applicable at this stage" apart. Some
gates pass by default when their input is missing; others fail for structural reasons
(ADR-003, ADR-004) and the failure is written as a KILL. Configuration falls back silently
(cwd-relative paths, prefix-matched latency ids), artifacts do not record what produced them,
tests can write fixtures into the real audit ledger, and most research tests do not run in CI.
`src/hft_platform` also imports `research` in a few places.

The result is verdicts that cannot be trusted or reproduced. The fix is structural, not a
threshold change.

## Decision

### 1. Layers
```
 generators: paper | mining | candidate_loop | manual   (emit candidates; never KILL)
        |
 data_pipeline: one ClickHouse reader -> dedup -> sessions -> scaled prices
        |
 backtest: one simulator + one stats core (day-axis series)
        |
 alpha gates -> GateResult{outcome, measured, threshold, n}
        |
 alpha.outcomes.derive_verdict  (called once per run)
        |
 evidence writer -> private evidence root -> promotion -> Shadow exit parity -> Live
```
Generators may rank and shortlist; only the judge writes a verdict, and a KILL is written only
through the `KillRecord.from_verdict()` constructor in
`src/hft_platform/alpha/kill_ledger.py`.

### 2. Outcome states
Every gate result carries one of `PASS`, `FAIL`, `INSUFFICIENT_SAMPLE`, `NOT_APPLICABLE`,
`ERROR`. `passed` stays for compatibility and is derived from the outcome; a contradiction
between the two is an error. A gate whose input is missing reports `ERROR` (or
`NOT_APPLICABLE` where the profile says so), never `PASS`.

### 3. Verdict rules v1
A pure, versioned function, `derive_verdict` in `src/hft_platform/alpha/outcomes.py`:

```
any blocking gate ERROR                    -> run INVALID (rerun; no verdict)
blocking FAIL, powered                     -> KILL
blocking INSUFFICIENT_SAMPLE, reachable    -> NEEDS-MORE-DAYS (+ required n)
blocking INSUFFICIENT_SAMPLE, unreachable  -> INCONCLUSIVE (unreachable sample)
blocking FAIL, underpowered                -> INCONCLUSIVE
NOT_APPLICABLE allowed only where the profile declares the gate deferrable;
  it becomes an obligation of the named stage (replay_parity -> shadow_exit)
otherwise                                  -> PASS
```
"Powered" for an estimate gate (Sharpe, edge, IC, win rate) means the confidence interval
excludes the threshold; for a property gate (drawdown, dominance, concentration) it means the
minimum-sample gate passed. "Reachable" means the required sample can be provided by the
measurement unit (ADR-004). Only a PASS proceeds to promotion.

### 4. Profiles
`config/research/profiles/vm_ul6_strict.yaml` stays byte-identical. A successor strict profile
carries the same thresholds and blocking list, fixes broken references (latency profile id,
Gate F while Rust is paused, cost and rebate assumptions) and declares `replay_parity`
deferrable to `shadow_exit` (ADR-003). A test asserts the successor's thresholds and blocking
list equal the original's. When a profile and a gate default disagree, the profile wins.

### 5. Provenance (RunStamp)
Every run records: commit, dirty flag, a hash of every loaded module (ignored local code is
invisible to a commit hash), dataset sha256, resolved-config sha256, profile id, verdict-rules
version; later also protocol hash, engine version and calibration state. A defects registry (a new
`config/research/defects.yaml`, stage 2) names each fixed defect by commit and component, so stale
results are computed, not remembered; marking a run stale appends an event and never edits
evidence.

### 6. Evidence root
Code stays in this public repository; evidence and data do not.

```
$HFT_RESEARCH_EVIDENCE_ROOT   private git repository, outside this repo
  runs/<yyyy>/<run_id>/       manifest + report
  ledger/verdicts.jsonl       append-only; status derives from the latest event
  protocols/<id>.yaml         pre-registrations, referenced by sha256
$HFT_RESEARCH_DATA_ROOT       not in git
```
A governed run requires a clean tree, a configured evidence root outside the repository and a
resolvable profile; otherwise it may run only as exploratory, which is labelled and never
counts as promotion evidence. Runs do not mutate the repository (no file moves, no rewriting
of alpha sources). Existing `research/experiments/**` is frozen in place and read through a
read-only adapter that quarantines fixture rows. The ClickHouse kill ledger becomes a
rebuildable index.

### 7. Boundaries and code location
`hft_platform` does not import `research`; a ratchet test modelled on
`tests/unit/test_cli_research_boundary.py` enforces it with an explicit allowlist that fails on
stale entries. Gate honesty is fixed inside `src/hft_platform/alpha/` first. Later, only
research-only orchestration moves into already-tracked `research/` subtrees
(`research/registry/`, `research/data_pipeline/`, `research/backtest/`,
`research/combinatorial/`). Modules the runtime or CI imports stay in `src`:
`replay_parity` (reached by `hft run --mode replay` through
`src/hft_platform/replay/cli_runner.py`), canary, discovery, `latency_*` (CI runs
`hft_platform.alpha.latency_audit`) and strategy specs. The Gate C runtime is injected from
`research/backtest/gate_c_runtime.py` instead of imported by `src`.

The signal monitor TUI (ADR-001) loads alpha implementations by path on purpose; that is not
a boundary violation and stays allowlisted.

### 8. Sequencing and retirement
Three stages, one branch and PR per step, each stoppable:

1. **Honest verdicts, stop pollution**: test isolation of audit sinks, no default deleters of
   evidence, outcome states and the single verdict path, the successor profile applied to
   Gate C, fail-closed configuration, RunStamp v0, the boundary test.
2. **Reproducible and in CI**: a research CI job, the evidence root, stale-result detection,
   the rolled maker series (ADR-004), the Shadow-exit parity check (ADR-003).
3. **Convergence**: one data reader, one simulator and result type, one stats core, programs
   reduced to generators, partial relocation, retirement of dead code and duplicated docs.

A legacy evaluation path is deleted only after the replacement reproduces its results on
frozen fixtures, or the differences are listed and explained.

## Consequences
- Pros: a KILL means a powered failure; "not enough days" and "not yet applicable" are visible
  states; any number can be traced to code, data and config; research code is tested in CI.
- Cons: once the successor profile is applied, gates that were advisory become blocking, so
  candidates that used to "pass" will receive honest non-PASS verdicts. Evidence lives in two
  places during the transition. The work spans many PRs that touch alpha governance (Tier 3).
- Not decided here: the daily/panel research track, and whether to archive or delete retired
  programs (decided case by case at the convergence stage).
