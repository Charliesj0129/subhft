# Research Refinement Route Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-closed, route-aware archive iteration and prevent evidence backfill work from being queued for candidates on other advancement routes.

**Architecture:** Keep this slice in `research/factory.py` and implement pure artifact projection helpers before CLI wiring. Candidate advancement is authoritative for route membership; readiness is authoritative for evidence command families and metrics. All identity or schema disagreement produces a blocked artifact and non-zero CLI result.

**Tech Stack:** Python 3.12, argparse, JSON artifacts, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-06-14-research-refinement-route-correctness-design.md`

---

## File Map

- Modify `research/factory.py`: artifact validation, archive/iteration projections, queue filtering, CLI commands and parser wiring.
- Modify `tests/unit/test_research_factory.py`: pure projection, fail-closed, CLI and parser regression tests.
- Modify `docs/goals/candidate_research_refinement_loop.md`: record the implemented route-aware command and artifact names without changing goal policy.

### Task 1: Archive and iteration projections

**Files:**
- Modify: `tests/unit/test_research_factory.py`
- Modify: `research/factory.py`

- [ ] **Step 1: Write failing happy-path tests**

Add fixtures for an advancement payload containing four `archive_candidate` rows and one
`sample_expansion_candidate` row. Test that the archive projection:

```python
archive, iteration = factory._research_refinement_iteration_payload(
    advancement,
    iteration_index=1,
    archive_output_path=Path("/tmp/archive.json"),
)

assert archive["decision"] == "archive_recommended"
assert archive["destructive"] is False
assert archive["candidate_group"] == ["failed_a", "failed_b", "failed_c", "failed_d"]
assert archive["excluded_candidates"] == [
    {"candidate": "t1f", "advancement_status": "sample_expansion_candidate"}
]
assert iteration["selected_route"] == "archive_candidate_set"
assert iteration["recommended_research_route"] == "expand_sample"
assert iteration["status"] == "completed"
```

- [ ] **Step 2: Run tests and confirm RED**

Run:

```bash
uv run pytest --no-cov tests/unit/test_research_factory.py \
  -k 'refinement_iteration or archive_decision' -q
```

Expected: fail because `_research_refinement_iteration_payload` is absent.

- [ ] **Step 3: Implement minimal pure helpers**

Add helpers near candidate advancement code:

```python
_VALID_RESEARCH_ROUTES = frozenset(_ADVANCEMENT_STATUS_ROUTE.values())

def _research_refinement_iteration_payload(
    advancement: dict[str, Any],
    *,
    iteration_index: int,
    archive_output_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ...
```

Validate the advancement schema, candidate identities, target group and route/status
mapping. Build `research.candidate_archive_decision.v1` and
`research.refinement_iteration.v1` without writing files. Reuse
`_research_candidate_advancement_route()` on non-target rows to project the next route.

- [ ] **Step 4: Run tests and confirm GREEN**

Run the focused command from Step 2. Expected: pass.

- [ ] **Step 5: Write fail-closed tests**

Cover unsupported schema, duplicate identity, empty group, missing target, status/route
mismatch, unsupported non-archive route and non-positive iteration index. Assert stable
error strings and blocked iteration status.

- [ ] **Step 6: Run fail-closed tests and confirm RED**

Expected: at least one new test fails until every invariant is implemented.

- [ ] **Step 7: Complete validation logic and confirm GREEN**

Return blocked artifacts without inventing a replacement route. Run all Task 1 tests.

### Task 2: Refinement iteration CLI

**Files:**
- Modify: `tests/unit/test_research_factory.py`
- Modify: `research/factory.py`

- [ ] **Step 1: Write parser and command tests**

Test:

```python
args = factory.build_parser().parse_args([
    "refinement-iteration",
    "--iteration-index", "1",
    "--archive-out", "archive.json",
    "--out", "iteration.json",
])
assert args.func is factory.cmd_refinement_iteration
```

Add a command test using a temporary governed research root. Assert both JSON files are
written, T1-F is excluded from archive, and the return code is zero. Add a blocked command
test that returns one while still writing the iteration error artifact.

- [ ] **Step 2: Run tests and confirm RED**

Run:

```bash
uv run pytest --no-cov tests/unit/test_research_factory.py \
  -k 'parser_exposes_refinement or refinement_iteration_command' -q
```

Expected: parser rejects the new command.

- [ ] **Step 3: Implement command and parser**

Add `cmd_refinement_iteration()`. Build current readiness and advancement through existing
audit helpers, call the pure projection, write archive output only for a completed archive
route, always write the iteration artifact, and return non-zero when blocked.

Expose `refinement-iteration` through the existing `research.factory` argparse parser.
Do not modify `research/__main__.py`; its current uncommitted data-pipeline work is outside
this slice and the canonical readiness entrypoint is `python -m research.factory`.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the Task 2 test selection. Expected: pass.

### Task 3: Advancement-aware evidence queue

**Files:**
- Modify: `tests/unit/test_research_factory.py`
- Modify: `research/factory.py`

- [ ] **Step 1: Replace queue expectations with route-aware tests**

Pass readiness and advancement payloads to
`_research_readiness_backfill_queue_payload()`. Test one evidence-backfill candidate,
one archive candidate and one sample-expansion candidate. Assert only the evidence
candidate is queued and the others have skip reason
`advancement_route_not_evidence_backfill`.

- [ ] **Step 2: Add mismatch tests and confirm RED**

Cover readiness-only identity, advancement-only identity and duplicate advancement
identity. Expected: blocked queue artifact with stable integrity errors and no partial
queue.

- [ ] **Step 3: Implement queue validation and filtering**

Change signature to:

```python
def _research_readiness_backfill_queue_payload(
    readiness_summary: dict[str, Any],
    advancement: dict[str, Any],
) -> dict[str, Any]:
    ...
```

Add `status` and `errors` to the queue artifact. Update
`cmd_readiness_backfill_queue()` to build advancement first and return one for blocked
artifacts.

- [ ] **Step 4: Run focused queue tests and confirm GREEN**

Run:

```bash
uv run pytest --no-cov tests/unit/test_research_factory.py \
  -k 'readiness_backfill_queue' -q
```

Expected: pass.

### Task 4: Documentation and verification

**Files:**
- Modify: `docs/goals/candidate_research_refinement_loop.md`

- [ ] **Step 1: Document implemented command and artifacts**

Add a concise implementation note naming `refinement-iteration`,
`readiness_candidate_archive_decision.json`, route-aware queue behavior, and the deferred
literature-refresh scheduler.

- [ ] **Step 2: Run full focused test module**

```bash
uv run pytest --no-cov tests/unit/test_research_factory.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run touched-file Ruff**

```bash
uv run ruff check research/factory.py tests/unit/test_research_factory.py
```

Expected: no new findings. If whole-file Ruff reports only the two pre-existing C901
findings at `_audit_alpha_contract` and `_audit_data_governance`, verify changed-line
lint separately and report that limitation.

- [ ] **Step 4: Run artifact smoke commands**

```bash
uv run python -m research.factory refinement-iteration \
  --iteration-index 1 \
  --archive-out /tmp/readiness_candidate_archive_decision.json \
  --out /tmp/readiness_refinement_iteration.json

uv run python -m research.factory readiness-backfill-queue \
  --out /tmp/readiness_backfill_queue.json
```

Expected current-state results:

- selected route: `archive_candidate_set`;
- four archive recommendations;
- T1-F excluded and unchanged;
- next route: `expand_sample`;
- backfill queue contains no archive or sample-expansion candidates.

- [ ] **Step 5: Inspect generated artifacts and diff**

Use `jq` to verify schema, candidate groups, next route, queue counts and errors. Run
`git diff --check` and review only intentional files.
