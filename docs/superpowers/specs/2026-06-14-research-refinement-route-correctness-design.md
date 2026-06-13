# Research Refinement Route Correctness Design

Date: 2026-06-14
Status: approved for planning
Branch: `research-flow/edge-evidence-parity-hardening`
Goal: `docs/goals/candidate_research_refinement_loop.md`

## Objective

Make one refinement iteration execute exactly one selected research route, produce a
non-destructive archive decision when the selected route is `archive_candidate_set`,
and prevent evidence backfill work from being queued for candidates already classified
for archival.

This slice does not implement the every-third-iteration literature refresh rule.

## Current Problem

The candidate advancement artifact correctly classifies four candidates as
`archive_candidate` and T1-F as `sample_expansion_candidate`. However, the current
backfill queue independently reads readiness rows and creates OOS and parity evidence
jobs for all five candidates. This causes eight unnecessary operator tasks for candidates
whose selected disposition is archival.

The goal document also requires each iteration to select one route, process one candidate
or group, produce the corresponding artifact, and emit the next route. The current
commands produce readiness and advancement projections, but no iteration artifact or
archive decision artifact completes that contract.

## Scope

### Included

- Add a route-aware refinement iteration projection and CLI command.
- Require exactly one selected route per iteration.
- Add a non-destructive archive decision artifact for an archive candidate group.
- Keep non-target candidates unchanged, including T1-F as a sample-expansion candidate.
- Make the evidence backfill queue advancement-aware.
- Queue evidence work only for candidates classified as
  `evidence_backfill_candidate` in the matching advancement artifact.
- Fail closed on malformed, stale, or inconsistent artifacts.
- Add focused unit tests and CLI artifact smoke verification.

### Excluded

- Literature search or every-third-iteration `literature_refresh` scheduling.
- Deleting or moving candidate source, validation summaries, evidence, or experiment logs.
- Changing edge floors, cost models, validation gates, position sizing, production
  trading, broker adapters, risk, session, or force-flat behavior.
- General decomposition of `research/factory.py`.

## Architecture

Keep the implementation inside the existing research factory boundary for this slice.
The new orchestration is a pure projection over the existing readiness and advancement
payloads. CLI commands gather current audit state, call the projection helpers, and write
JSON artifacts through the existing `_write_json` helper.

The advancement artifact is the authority for route membership. Readiness remains the
authority for blockers, metrics, summary paths, and available evidence command families.
No command infers route membership directly from a subset of readiness blockers.

## Route Selection Contract

The iteration projection consumes a valid
`research.readiness_candidate_advancement.v1` payload and selects its existing
`recommended_research_route`. It must validate that:

- the route is one of the routes defined by the goal document;
- `recommended_candidate_group` is a non-empty list for a group route;
- every target candidate exists exactly once in `candidates`;
- every target candidate's `advancement_status` maps back to the selected route;
- no non-target candidate is reported as changed by this iteration.

Any failed invariant produces a blocked iteration artifact and a non-zero CLI result.
The implementation must not guess a replacement route.

## Iteration Artifact

Add schema `research.refinement_iteration.v1` with these fields:

- `generated_at`
- `schema`
- `iteration_index`
- `status`: `completed` or `blocked`
- `selected_route`
- `candidate`
- `candidate_group`
- `literature_refresh_triggered`: always `false` in this slice
- `input_artifacts`
- `artifact_produced`
- `candidate_status_changes`
- `ready_for_paper_updates`
- `summary`
- `recommended_research_route`
- `validation_results`
- `unresolved_gaps`
- `next_action`
- `errors`

The CLI command is `refinement-iteration`. It accepts `--iteration-index` as a positive
integer, plus optional `--out` and `--archive-out` paths. The default outputs are
`research/reports/readiness_refinement_iteration.json` and
`research/reports/readiness_candidate_archive_decision.json`.

For this slice, the command supports `archive_candidate_set`. Other valid routes produce
a blocked artifact with `route_not_implemented_in_this_slice` rather than silently doing
partial work.

## Archive Decision Artifact

When the selected route is `archive_candidate_set`, emit
`research.candidate_archive_decision.v1` containing:

- `generated_at`
- `schema`
- `decision`: `archive_recommended`
- `destructive`: `false`
- `candidate_group`
- one candidate record per target with:
  - `candidate`
  - `previous_advancement_status`
  - `recommended_status`
  - `primary_reason`
  - `blocking_factors`
  - `supporting_metrics`
  - `risk_flags`
  - `summary_path`
  - `spec_path`
  - `retained_artifacts`
- `excluded_candidates`
- `validation_results`
- `errors`

`retained_artifacts` explicitly records that source, spec, validation summaries, metrics,
evidence, and experiment logs are preserved. The command does not edit candidate source
or historical artifacts.

The iteration artifact's `artifact_produced` references the resolved archive output path.

## Candidate Isolation

Only candidates in the selected archive group receive a status change from
`archive_candidate` to `archive_recommended`. Candidates outside the target group are
listed in `excluded_candidates` with their current advancement status and no status
change.

With the current artifacts, T1-F must remain `sample_expansion_candidate` and must not
appear in the archive candidate group.

## Next Route Projection

After an archive decision is produced, compute the next route from the advancement rows
that are not in the archived target group. This is an artifact projection only; it does
not mutate the source advancement artifact.

The iteration artifact records:

- `selected_route`: the route processed in this iteration;
- `candidate_status_changes`: target candidates changing from `archive_candidate` to
  `archive_recommended` in the decision projection;
- `recommended_research_route`: the route selected from the remaining active candidates;
- `next_action`: the action matching that next route.

With the current candidate set, processing the four-candidate archive group must emit
`expand_sample` as the next route for T1-F. If no active candidates remain, use
`archive_candidate_set_complete` as the terminal next action and leave
`recommended_research_route` empty.

## Backfill Queue Contract

Change the backfill queue projection to consume both readiness and advancement payloads.
For each readiness candidate:

1. Find exactly one matching advancement row.
2. Queue command families only when `advancement_status` is
   `evidence_backfill_candidate`.
3. Otherwise append a skipped record with reason
   `advancement_route_not_evidence_backfill` and include its advancement status.

Missing, duplicate, or mismatched candidate identities are artifact-integrity errors.
The queue command writes a blocked artifact and exits non-zero rather than producing a
partial queue.

This means archive, sample-expansion, hypothesis-review, parity-repair, artifact-repair,
and ready-for-paper candidates cannot receive evidence backfill tasks from this command.

## Failure Handling

All new behavior is fail closed:

- Unsupported schema: blocked artifact, non-zero exit.
- Non-positive or non-integer iteration index: argument error, non-zero exit.
- Empty selected group: blocked artifact, non-zero exit.
- Candidate missing from advancement rows: blocked artifact, non-zero exit.
- Duplicate candidate identity: blocked artifact, non-zero exit.
- Status-to-route mismatch: blocked artifact, non-zero exit.
- Readiness/advancement candidate-set mismatch: blocked queue artifact, non-zero exit.
- Existing report output: overwrite only the generated report path; never overwrite
  source, spec, validation, or evidence artifacts.

Errors must be stable machine-readable strings suitable for tests and operator tooling.

## Testing

Use TDD for each behavior change.

Focused tests must cover:

- archive route emits exactly one completed iteration artifact;
- archive decision contains exactly the selected archive group;
- T1-F remains excluded and unchanged;
- archive decision is explicitly non-destructive and preserves artifact references;
- malformed or inconsistent advancement payload blocks closed;
- unsupported non-archive route blocks with a stable error;
- evidence queue includes only `evidence_backfill_candidate` rows;
- archive and sample-expansion candidates are skipped with explicit reasons;
- readiness/advancement identity mismatch blocks the queue;
- parser exposes the new command and arguments;
- CLI writes both iteration and archive decision artifacts.

Verification commands:

```bash
uv run pytest --no-cov tests/unit/test_research_factory.py -q
uv run ruff check research/__main__.py research/factory.py tests/unit/test_research_factory.py
uv run python -m research.factory refinement-iteration \
  --iteration-index 1 \
  --archive-out /tmp/readiness_candidate_archive_decision.json \
  --out /tmp/readiness_refinement_iteration.json
uv run python -m research.factory readiness-backfill-queue \
  --out /tmp/readiness_backfill_queue.json
```

The existing two Ruff complexity findings in untouched legacy audit functions are not
part of this slice. New or modified functions must pass Ruff; verification must report
the pre-existing findings accurately if whole-file Ruff remains red.

## Acceptance Criteria

- One iteration emits exactly one selected route.
- Current artifacts produce `archive_candidate_set` for the four failed candidates.
- T1-F remains a sample-expansion candidate and is excluded from archive decisions.
- The completed archive iteration emits `expand_sample` as the next route for T1-F.
- Archive output is recommendation-only and preserves all existing artifacts.
- Evidence backfill queue contains no archive or sample-expansion candidates.
- Artifact mismatches fail closed with non-zero command status.
- Focused unit tests pass.
- No production/runtime trading behavior changes.
