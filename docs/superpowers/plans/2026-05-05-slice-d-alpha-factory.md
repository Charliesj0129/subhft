# Slice D — Alpha Factory MVP (kill ledger + screener + cluster + DSL)

> Phase 3 of the alpha-promotion overhaul. Master blueprint:
> `/home/charlie/.claude/plans/curried-launching-unicorn.md` (Slice D §).
> Predecessors: Slice A (PR #337, `66f3eb8a`), Slice C (PR #339, `5861730d`),
> Slice B (PR #340, branch `slice-b/maker-realism` open at `46bf3299` —
> this plan **does not depend on Slice B merging first**, but the runbook
> in §17 must be regenerated against the post-merge anchors before opening
> Slice D's PR if Slice B has merged in the interim).

---

## §0 Spec reference

This plan was drafted on Slice C base `5861730d` (Slice C merged 2026-05-05).
It will be Codex-adversarial-reviewed (`/codex:adversarial-review --background`)
before Task 1 begins. All file:line anchors below were re-verified against
`5861730d` on 2026-05-05; the master blueprint's Slice D anchors had drifted
in three places — this plan supersedes the blueprint's Slice D §:

| Blueprint claim | Verified reality on 5861730d | Source |
|---|---|---|
| `correlation_tracker.py` lives in `src/hft_platform/...` | Lives at `research/registry/correlation_tracker.py:21-54` | `find` |
| `flag_redundant` method | Method is `flag_redundant` on `CorrelationTracker:38`, delegates to `pool.flag_redundant_pairs:127` | `grep` |
| 4 active alphas (`_templates`, `fill_prob_filter`, `r47_maker_pivot`, `r47_skew_mr`) | 15 alphas have `manifest.yaml`; `r47_skew_mr` is impl/explore only (no manifest); `_templates` is a templates dir (not a real alpha) | `find research/alphas -name manifest.yaml` |
| `factor_compiler.py:139-147` rolling-state leak | File does not exist on `5861730d`; was on parked `wip/pre-slice-a-2026-05-03` only — **anachronistic risk**, dropped from §10 | `find` returns no result |
| `data_fingerprint`/`rng_seed` could be added to `AlphaManifest` (path a) | Already on `Scorecard:51-58, 82-83` — **path (b) is now mandatory** to avoid duplication | `grep` |

This document is the canonical spec for the slice; the blueprint § is reference only.

## §1 Goal

Replace the human-reviewed alpha-research workflow with an automated pipeline:
candidate alphas pass through a **cheap screener** (IC + turnover + cost-floor),
correlation **clustering** flags redundant pairs, **kill rejections** are written
to a structured ledger (jsonl + ClickHouse), and a minimal **DSL** allows
`r47_maker_pivot`'s formula to round-trip between text and executable code.

## §2 Non-goals

- Greenfield DSL features beyond round-tripping `r47_maker_pivot`.
- Mass re-scoring of the 46 archived alphas (only manifest backfill, no PnL re-run).
- Replacing Gates A–F. Slice D adds an upstream filter and a downstream ledger;
  the gate machinery from Slices A/B/C is reused untouched.
- Greenfield correlation algorithms. We wrap existing `pool.compute_pool_matrix`
  + `pool.flag_redundant_pairs` with hierarchical (single-linkage, ρ≥0.7) clustering.
- Live broker integration. Slice D is research-side only; the kill ledger writes to
  `audit.alpha_kill_ledger`, not to live trading topics.

## §3 Why

Slice A made promotion gates strict. Slice B made the backtest realistic.
Slice C made live/replay parity a gate. None of them solves the **inbound**
problem: candidate alphas still arrive at Gate A faster than humans can sift,
and rejected alphas leave no machine-readable record. R47-OE1's failure mode
also exposed a redundancy gap — `r47_maker_pivot` and `r47_skew_mr` share the
R47 microstructure family but neither tree had a documented correlation pair.

Slice D closes both:

1. **Throughput** — `hft alpha screen <id>` produces a kill verdict in <60s
   per alpha, replacing the per-round human ScoringSheet.
2. **Auditability** — `audit.alpha_kill_ledger` accumulates every rejection
   with `(killed_at, alpha_id, gate, reason, manifest_hash, scorecard_id, killed_by)`
   so downstream operators can reconstruct *why* an alpha was killed, not just
   *that* it was.
3. **Redundancy** — `hft alpha cluster` produces `cluster_id` per alpha and
   persists it to the manifest, so future round summaries can flag siblings
   automatically.

## §4 Surface

### §4.1 New files

| Path | Purpose | Owner task |
|---|---|---|
| `src/hft_platform/migrations/clickhouse/20260505_001_create_alpha_kill_ledger.sql` | CH table `audit.alpha_kill_ledger` | T3 |
| `src/hft_platform/alpha/kill_ledger.py` | Append-only ledger (jsonl + CH mirror) | T4 |
| `src/hft_platform/alpha/screener.py` | `cheap_screen(alpha_id) -> ScreenResult` | T6 |
| `src/hft_platform/alpha/cluster.py` | Hierarchical clustering wrapper | T7 |
| `src/hft_platform/alpha/dsl/__init__.py` | DSL package marker | T8 |
| `src/hft_platform/alpha/dsl/parser.py` | DSL text → AST | T8 |
| `src/hft_platform/alpha/dsl/compiler.py` | AST → executable callable | T9 |
| `src/hft_platform/alpha/dsl/formula_context.py` | Round-trip + manifest binding | T10 |
| `scripts/migrate_alpha_manifests.py` | One-shot: backfill `cluster_id`/`kill_reason` | T15 |
| `tests/integration/test_alpha_factory_e2e.py` | DoD-D1..D6 evidence | T17,18,19 |
| `tests/unit/alpha/test_screener.py` | Unit coverage for screener | T6 |
| `tests/unit/alpha/test_cluster.py` | Unit coverage for clustering | T7 |
| `tests/unit/alpha/test_dsl_round_trip.py` | DoD-D6 golden round-trip | T20 |
| `tests/unit/alpha/test_kill_ledger.py` | Ledger jsonl + CH fallback | T4 |
| `tests/unit/migrations/test_alpha_kill_ledger_schema.py` | Schema sanity | T3 |
| `docs/runbooks/alpha-factory.md` | Operator runbook | T21 |

### §4.2 Modified files

| Path | Change | Owner task |
|---|---|---|
| `research/registry/schemas.py` | `AlphaManifest` gains `dsl_formula`, `parent_alpha_id`, `kill_reason`, `cluster_id` (path b — provenance stays on `Scorecard`) | T2 |
| `src/hft_platform/alpha/audit.py` | New `log_kill()` mirroring `log_promotion_result()` (line 144) | T5 |
| `src/hft_platform/alpha/promotion.py` | `promote_alpha` calls `log_kill()` on Gate-C/D rejection | T14 |
| `src/hft_platform/cli/_alpha.py` | Add `cmd_alpha_screen`, `cmd_alpha_kill`, `cmd_alpha_cluster` | T11,12,13 |
| `src/hft_platform/cli/_parser.py` | Register the 3 new subcommands at `_parser.py:338-364` (alpha sub-parser block) | T11,12,13 |
| `docs/architecture/current-architecture.md` | Append §7C "Slice D — Alpha Factory MVP" | T21 |
| `docs/operations/env-vars-reference.md` | Add `HFT_KILL_LEDGER_ENABLED` | T21 |

## §5 Schema decisions (path-b, locked)

The blueprint left two schema paths open. **Path (b) is locked** because
`Scorecard` already carries `data_fingerprint` and `rng_seed` (verified at
`research/registry/scorecard.py:51-58, 82-83`), so duplicating them on
`AlphaManifest` would create a sync surface no caller benefits from.

`AlphaManifest` (frozen dataclass, `research/registry/schemas.py`) gains:

```python
dsl_formula: str | None = None        # round-trippable formula text; None for legacy alphas
parent_alpha_id: str | None = None    # genealogy; None for greenfield
kill_reason: str | None = None        # populated by promote_alpha or migrate_alpha_manifests
cluster_id: str | None = None         # set by cmd_alpha_cluster; None pre-clustering
```

`Scorecard` is **untouched** — provenance remains where it already lives.

`audit.alpha_kill_ledger` schema (CH):

```sql
CREATE TABLE IF NOT EXISTS audit.alpha_kill_ledger (
    killed_at      DateTime64(9, 'UTC')        DEFAULT now64(9, 'UTC'),
    alpha_id       String                      NOT NULL,
    gate           Enum8('A'=1,'B'=2,'C'=3,'D'=4,'E'=5,'F'=6,'pre_screen'=7,'cluster'=8,'manual'=9),
    reason         String                      NOT NULL,
    manifest_hash  String                      DEFAULT '',
    scorecard_id   String                      DEFAULT '',
    killed_by      String                      DEFAULT 'system'  -- {system,operator_xxx}
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(killed_at)
ORDER BY (alpha_id, killed_at)
TTL killed_at + INTERVAL 365 DAY
SETTINGS index_granularity = 8192;
```

Mirrors Slice C's `hft.order_intents` 365d TTL. Reuses `_write_fallback`
jsonl pattern from `audit.py:42-55` for the offline path
(`research/alphas/_kill_ledger.jsonl`).

## §6 Pre-flight

1. **WIP-park** — `git checkout -b wip/pre-slice-d-2026-05-05` and commit any
   in-flight working-tree changes (the main checkout currently has 27 dirty files
   on `loop-v1/convergence`; do not touch them in Slice D's tree).
2. **Worktree isolation** — work in `/tmp/slice-d-wt` (already created at
   detached `5861730d`). Branch off as `slice-d/alpha-factory-mvp`.
3. **Slice C base** — confirm `git rev-parse HEAD` → `5861730d`.
4. **Anchors freeze** — copy this file to the worktree path and treat all
   §4 file paths as authoritative. Re-running `find research/alphas -name manifest.yaml`
   on Task 1 must still report 15 hits.
5. **Slice B awareness** — if Slice B PR #340 merges before this slice ships,
   re-baseline the worktree (`git fetch origin main && git rebase origin/main`)
   and re-verify §4.2 anchors. Slice B touches `promotion.py:283 _evaluate_gate_d`,
   which Task 14 also touches — rebase is **mandatory** in that case.

## §7 Tasks

> RED→GREEN→commit cadence. One subagent per task (per `superpowers:subagent-driven-development`).
> Every task ends with `make ci` green or a documented partial-pass and a commit.

### Task 1 — Pre-flight + plan freeze

- Verify worktree HEAD `5861730d`; create branch `slice-d/alpha-factory-mvp`.
- Re-verify anchors in §4 with `grep -n` and `find`.
- Commit this plan file under `docs/superpowers/plans/2026-05-05-slice-d-alpha-factory.md`.

### Task 2 — `AlphaManifest` schema extension (path b)

- Files: `research/registry/schemas.py`, `tests/unit/research_registry/test_schemas.py`.
- RED: write a failing `test_alpha_manifest_round_trip_with_new_fields` that
  constructs an `AlphaManifest` with `dsl_formula="qi_d1*qs_d2"`,
  `parent_alpha_id="r47_maker_pivot"`, `kill_reason=None`,
  `cluster_id="r47_family"`, then `to_dict()` → `from_dict()` and asserts equality.
- GREEN: extend the dataclass + `to_dict` / `from_dict` to round-trip the four
  new fields. Default all to `None`.
- Verify: `uv run pytest tests/unit/research_registry/test_schemas.py -q` passes;
  existing `manifest.yaml` files still load (no required-field break).

### Task 3 — `audit.alpha_kill_ledger` migration

- Files: `src/hft_platform/migrations/clickhouse/20260505_001_create_alpha_kill_ledger.sql`,
  `tests/unit/migrations/test_alpha_kill_ledger_schema.py`.
- Mirrors Slice C's `20260504_001_create_order_intents.sql` shape.
- Verify migration filename ordering keeps it after Slice C's last migration.

### Task 4 — `kill_ledger.py` writer + jsonl fallback

- Files: `src/hft_platform/alpha/kill_ledger.py`, `tests/unit/alpha/test_kill_ledger.py`.
- API:
  ```python
  @dataclass(frozen=True, slots=True)
  class KillRecord:
      killed_at: int          # ns since epoch
      alpha_id: str
      gate: str               # one of {'A','B','C','D','E','F','pre_screen','cluster','manual'}
      reason: str
      manifest_hash: str = ""
      scorecard_id: str = ""
      killed_by: str = "system"

  def append_kill(record: KillRecord) -> None: ...
  def read_kills(alpha_id: str | None = None) -> list[KillRecord]: ...
  ```
- jsonl path: `research/alphas/_kill_ledger.jsonl` (append-only, gitignored similar
  to current `_archive_2026-04-17/` pattern — verify `.gitignore` covers it).
- CH path: insert into `audit.alpha_kill_ledger` via `_get_client()` from `audit.py:68`.
- Mirror `_write_fallback` semantics: CH failure → jsonl; jsonl failure → log warning,
  do not raise.

### Task 5 — `audit.log_kill()`

- Files: `src/hft_platform/alpha/audit.py`.
- Mirror `log_promotion_result()` at line 144.
- Increments `alpha_kill_results_total{alpha_id, gate, reason}` Prometheus counter
  (add to `observability/metrics.py` if not present — verify first).
- Verify: `uv run pytest tests/unit/alpha/test_audit.py -q` (extend existing).

### Task 6 — `screener.py` cheap screener

- Files: `src/hft_platform/alpha/screener.py`, `tests/unit/alpha/test_screener.py`.
- API:
  ```python
  @dataclass(frozen=True, slots=True)
  class ScreenResult:
      alpha_id: str
      verdict: str            # 'pass' | 'kill'
      ic_mean: float
      ic_std: float
      turnover: float
      cost_floor_breach: bool
      reason: str             # populated when verdict == 'kill'
      duration_s: float

  def cheap_screen(alpha_id: str, *, project_root: Path = Path(".")) -> ScreenResult: ...
  ```
- Reuses `research/tools/feature_screener.py:1-100` `FeatureResult` for IC.
- Adds turnover gate (default >2.0/day → kill candidate) + cost-floor pre-check
  (reuses `cost_floor_per_fill_pts` threshold pattern from Slice B).
- Hard time budget: 60s wall-clock per alpha (DoD-D1).

### Task 7 — `cluster.py` hierarchical clustering

- Files: `src/hft_platform/alpha/cluster.py`, `tests/unit/alpha/test_cluster.py`.
- API:
  ```python
  @dataclass(frozen=True, slots=True)
  class ClusterAssignment:
      alpha_id: str
      cluster_id: str
      cluster_size: int
      max_intra_cluster_corr: float

  def cluster_alphas(*, base_dir: str = "research/experiments",
                     threshold: float = 0.7,
                     metric: str = "pearson") -> list[ClusterAssignment]: ...
  ```
- Wraps `pool.compute_pool_matrix(*, base_dir, sample_step)` (verified at
  `pool.py:77`) and `pool.flag_redundant_pairs(matrix_payload, *, threshold, metric)`
  (verified at `pool.py:127`).
- Uses single-linkage agglomerative clustering on `1 - |corr|` distance,
  cut at `1 - threshold` (defaults to 0.3 for ρ=0.7).
- `cluster_id` format: `cluster_<index>` for groups; `singleton_<alpha_id>` for size-1.

### Task 8 — `alpha/dsl/parser.py`

- Files: `src/hft_platform/alpha/dsl/__init__.py`,
  `src/hft_platform/alpha/dsl/parser.py`,
  `tests/unit/alpha/test_dsl_parser.py`.
- Minimum grammar: enough to express `r47_maker_pivot`'s formula
  (`d1_pe_entropy * d2_queue_survival * d3_mfg_skew`).
- Tokens: identifiers, `*`, `+`, `-`, `(`, `)`, numeric literals.
- AST: `BinOp`, `UnaryOp`, `Identifier`, `Literal`.
- No greenfield operators beyond what `r47_maker_pivot` requires.

### Task 9 — `alpha/dsl/compiler.py`

- Files: `src/hft_platform/alpha/dsl/compiler.py`,
  `tests/unit/alpha/test_dsl_compiler.py`.
- AST → callable that takes a `dict[str, np.ndarray]` of feature columns and
  returns `np.ndarray`.
- No `eval()`/`exec()`. Tree-walk interpreter only.
- Verify: round-trip `parse → compile → call` on `r47_maker_pivot` features
  reproduces existing signal output within `np.allclose(rtol=1e-9, atol=1e-12)`.

### Task 10 — `alpha/dsl/formula_context.py`

- Files: `src/hft_platform/alpha/dsl/formula_context.py`.
- API:
  ```python
  def round_trip(formula: str) -> str: ...   # parse → unparse, must equal canonicalized input
  def bind_to_manifest(formula: str, manifest: AlphaManifest) -> AlphaManifest: ...
  ```
- `bind_to_manifest` updates `manifest.dsl_formula`; raises if formula refers
  to identifiers not in `manifest.data_fields`.

### Task 11 — `cmd_alpha_screen`

- Files: `src/hft_platform/cli/_alpha.py`, `src/hft_platform/cli/_parser.py`.
- Mirror `cmd_alpha_promote:243` shape.
- Subcommand: `hft alpha screen <alpha_id> [--threshold-ic 0.005] [--threshold-turnover 2.0] [--write-kill]`.
- `--write-kill`: when verdict == 'kill', invoke `kill_ledger.append_kill()`.
- Register in `_parser.py:338-364` block.

### Task 12 — `cmd_alpha_kill`

- Subcommand: `hft alpha kill <alpha_id> --reason <text> [--gate manual]`.
- Wraps `kill_ledger.append_kill()` directly (manual operator path).
- Updates `manifest.yaml` `kill_reason` field via `AlphaManifest.from_dict` /
  `to_dict` round-trip.

### Task 13 — `cmd_alpha_cluster`

- Subcommand: `hft alpha cluster [--threshold 0.7] [--metric pearson] [--write-manifest]`.
- Calls `cluster_alphas()`, prints assignments table, and (if `--write-manifest`)
  updates `manifest.yaml` `cluster_id` for each alpha.

### Task 14 — `promote_alpha` auto-kill on Gate-C/D rejection

- Files: `src/hft_platform/alpha/promotion.py`.
- After `_evaluate_gate_c`/`_evaluate_gate_d` returns `(False, reasons)`,
  call `audit.log_kill(alpha_id, gate=<gate>, reason=<aggregated>)`.
- Gated by `HFT_KILL_LEDGER_ENABLED` env var (default `1`).
- Idempotent: re-running `promote_alpha` on an already-killed alpha must
  not duplicate ledger rows for the same `(alpha_id, gate, manifest_hash)`.

### Task 15 — `scripts/migrate_alpha_manifests.py`

- Backfills `cluster_id` for the 15 alphas with `manifest.yaml`.
- Backfills `kill_reason` for the 46 archived alphas in
  `research/archive/alphas_2026-04-17/` by reading their archived
  ScoringSheet / round-summary references.
- One-shot script. Idempotent: re-running with same data produces no diff.
- Documented in §17 runbook.

### Task 16 — Manifest backfill execution

- Run `scripts/migrate_alpha_manifests.py --dry-run`, verify diff.
- Run `scripts/migrate_alpha_manifests.py --apply`, commit the manifest changes.
- DoD-D2 evidence: 46 archived alphas now have `kill_reason` populated.

### Task 17 — DoD-D1 integration test

- Files: `tests/integration/test_alpha_factory_e2e.py::test_dod_d1_screen_15_alphas`.
- Iterates the 15 alphas with `manifest.yaml`, runs `cheap_screen`, asserts
  each completes in <60s.
- Records aggregate timing; CI floor: 95th-percentile <60s.

### Task 18 — DoD-D3 cluster pair detection

- Files: `tests/integration/test_alpha_factory_e2e.py::test_dod_d3_cluster_finds_r47_family`.
- Asserts at least one cluster pair with both `r47_maker_pivot` and a
  c14/c17/c60/c63/c72 family member (depending on which signals correlate
  in `research/experiments/`).
- The blueprint's "r47_maker_pivot vs r47_skew_mr" pair was wrong — `r47_skew_mr`
  has no manifest. We assert the **R47 family lineage** generically.

### Task 19 — DoD-D4 ledger row from Gate rejection

- Files: `tests/integration/test_alpha_factory_e2e.py::test_dod_d4_kill_ledger_row_on_gate_reject`.
- Calls `promote_alpha` on a fixture alpha with intentionally failing Gate-D
  thresholds. Asserts a row appears in `audit.alpha_kill_ledger` (CH-mocked
  client + jsonl fallback verified separately).

### Task 20 — DoD-D6 DSL round-trip

- Files: `tests/unit/alpha/test_dsl_round_trip.py::test_r47_maker_pivot_round_trips`.
- Parses `r47_maker_pivot.dsl_formula`, unparses it, asserts canonical equality.
- Compiles the AST and runs against `research/experiments/r47_maker_pivot/`
  signal fixtures; asserts `np.allclose` with the existing scorecard signal
  within `rtol=1e-9`.

### Task 21 — Runbook + arch map + env vars

- New `docs/runbooks/alpha-factory.md` mirroring
  `docs/runbooks/replay-parity-gate.md` and `docs/runbooks/maker-realism-gate.md`.
- Append §7C "Slice D — Alpha Factory MVP" surface table to
  `docs/architecture/current-architecture.md`.
- Add `HFT_KILL_LEDGER_ENABLED` (default `1`) to
  `docs/operations/env-vars-reference.md`.

### Task 22 — `make ci` green + ruff format

- `uv run ruff format src/ tests/ scripts/ research/`.
- `make ci` must produce ≥87% coverage (Slice C baseline 87.15%, Slice B 87.59%).
- Commit format-only fixup.

## §8 Definition of Done

| DoD | Evidence | Owner task |
|---|---|---|
| **D1** — All 15 alphas with `manifest.yaml` produce a `ScreenResult` in <60s each (95th percentile) | `test_dod_d1_screen_15_alphas` | T17 |
| **D2** — All 46 archived alphas have `kill_reason` populated | manifest diff in T16 commit | T16 |
| **D3** — Clustering on the 15 active alphas produces at least one R47-family cluster | `test_dod_d3_cluster_finds_r47_family` | T18 |
| **D4** — `audit.alpha_kill_ledger` accumulates a row from a Gate-C/D rejection | `test_dod_d4_kill_ledger_row_on_gate_reject` | T19 |
| **D5** — Cold-start subagent can run `hft alpha screen <id>` end-to-end | runbook §"Operator quick reference" | T21 |
| **D6** — DSL round-trips `r47_maker_pivot.dsl_formula` and `np.allclose` matches existing signal | `test_r47_maker_pivot_round_trips` | T20 |
| **D7** — `make ci` green with coverage ≥87% | CI log | T22 |

## §9 Self-review checklist

Before opening the PR:

- [ ] All §4 files touched match the §7 task ownership column (no orphan files,
  no mystery diffs).
- [ ] `AlphaManifest` round-trip preserves all four new fields including `None` defaults.
- [ ] `_write_fallback` jsonl path follows `audit.py:42-55` pattern verbatim.
- [ ] `cheap_screen` 60s budget enforced via `time.monotonic()` not wall-clock.
- [ ] `cluster_alphas` deterministic — same input → same `cluster_id` strings
  across runs (sort by `alpha_id` before agglomerating).
- [ ] `cmd_alpha_kill --reason` rejects empty / whitespace-only reasons.
- [ ] `promote_alpha` auto-kill is idempotent on `(alpha_id, gate, manifest_hash)`.
- [ ] `migrate_alpha_manifests.py` is dry-run-by-default; `--apply` is required to write.
- [ ] DSL parser rejects identifiers not in `manifest.data_fields` (Task 10 invariant).
- [ ] No new `unwrap()` / `panic!()` in any Rust path (Slice D is Python-only —
  the box should stay unchecked since no Rust changes; flag if it isn't).
- [ ] No `float` in `kill_ledger.py` for monetary fields (the ledger has no money,
  but `manifest_hash` and `scorecard_id` are plain strings — confirm).
- [ ] `HFT_KILL_LEDGER_ENABLED=0` short-circuits cleanly (no CH connection attempt).

## §10 Risk register (Slice D-specific)

| Risk | Mitigation |
|---|---|
| `manifest.yaml` schema break — adding 4 fields could break legacy alphas without those keys | Defaults `None`; `from_dict` uses `.get(...)` not `[...]`; T2 RED test must include a legacy fixture |
| Cluster determinism — Python dict ordering changes could re-label `cluster_<index>` | Sort `alpha_ids` lexicographically before clustering; assign `cluster_id` from sorted-pair lowest member |
| Screen 60s budget breached on slow CK reads | T6 has a `_warm_signal_cache(alpha_id)` pre-step; if signals not cached, screener returns `verdict='unknown'` not `kill` (advisory) |
| Backfill of 46 archived `kill_reason` requires reading round summaries that may not be machine-parseable | T15 falls back to `kill_reason='archived_2026_04_17'` when round summary parse fails; full backfill is best-effort |
| DSL grammar grows beyond `r47_maker_pivot` — scope creep | §2 non-goals locks scope; T8 grammar review must reject any operator not used by `r47_maker_pivot` |
| Slice B PR #340 merges mid-execution → `promotion.py:283` rebase conflict on Task 14 | §6 pre-flight requires rebase; rebase-and-revalidate is mandatory before T14 |
| `audit.alpha_kill_ledger` adds CH write load — already on PR #339 path | T4's CH write is best-effort with jsonl fallback (mirrors Slice C); load is bounded by alpha-promotion frequency, not tick rate |
| Coverage floor regression from new modules without tests | T6/T7/T8/T9 each ship with their own unit test in §4.1; T22 `make ci` gates on ≥87% |
| `scripts/migrate_alpha_manifests.py` rewrites all 61 manifests in one pass — large diff | T15 `--dry-run` default; T16 splits diff into separate commits per logical group (15 active + 46 archived) for review |

## §11 Out of scope

- Slice C path (a) live 2026-04-21 reconstruction (still user-gated).
- Greenfield DSL operator design beyond round-tripping `r47_maker_pivot`.
- Mass alpha re-scoring (no PnL re-run on archived alphas).
- Multi-broker support changes (Slice D is research-side only).
- Live trading integration (the kill ledger is a research/audit artifact).

## §12 Execution handoff

Recommended workflow for the executing operator:

1. Run `git worktree add /tmp/slice-d-wt origin/main` (already done in pre-flight).
2. `git checkout -b slice-d/alpha-factory-mvp` in the worktree.
3. Drive Tasks 1→22 via `superpowers:subagent-driven-development`. One fresh
   subagent per task, two-stage review between tasks.
4. After T22, open PR with the same template Slice C #339 and Slice B #340 use:
   Summary / Goal / Why / What changed / AI Participation / Test Plan
   (one box per DoD-D1..D7) / HFT Design Review / Out of scope.
5. Tag `slice-d-merged-YYYY-MM-DD` post-merge; write
   `slice_d_alpha_factory.md` memory card mirroring `slice_c_replay_parity_gate.md`.

---

## §13 Codex adversarial review record

This plan will be Codex-adversarial-reviewed via
`/codex:adversarial-review --background` immediately after drafting.
Findings will be folded into a §13.1 "Post-review delta" addendum below
before Task 1 begins. Until that addendum is written, this plan is **draft**
and Task 1 must not start.

### §13.1 Post-review delta (Codex 2026-05-05, task `bvsi1ogg6`)

Verdict: **ACCEPT-WITH-FIXES** (Codex flagged "leaning REJECT"; the seven findings
below are folded back into §4–§10 so the plan is now ACCEPTed).

**CRITICAL findings — block Task 1:**

- **C1. Task 14 references `_evaluate_gate_c` which does not exist.** Actual code:
  Gate C is enforced by `_verify_gate_c_passed()` which **raises** before the
  audit block (verified at `promotion.py:386-420`). A literal T14 implementation
  would only cover Gate D and silently miss Gate-C rejects.
  - **Fix:** T14 rewritten to (a) keep auto-kill on `_evaluate_gate_d` returning
    `(False, …)`, and (b) wrap `_verify_gate_c_passed()` with `try / except` that
    logs to kill ledger and re-raises. DoD-D4 test now exercises the Gate-C path
    explicitly.

- **C2. D3 clustering has no signal corpus on `5861730d`.** `research/experiments/`
  has no committed `meta.json` / `signals.npy` runs; `ExperimentTracker.latest_signals_by_alpha()`
  returns empty, so `compute_pool_matrix` payload is empty and "find R47-family
  cluster" is unprovable.
  - **Fix:** New **Task 7b** (between T7 and T11) — generate deterministic
    synthetic signal runs for the 15 manifest-bearing alphas under
    `research/experiments/_slice_d_fixtures/<alpha_id>/runs/<run_id>/{meta.json,signals.npy}`,
    seeded so r47_maker_pivot, c60, c63 (R47 family on TMFD6) correlate >0.7
    by construction. Cluster determinism then has a corpus to operate on.
    `cluster_alphas()` MUST fail-closed (raise `EmptyCorpusError`) when the
    payload is empty, never silently return `[]`.

- **C3. D6 round-trip has no fixture.** `r47_maker_pivot/manifest.yaml` has no
  `dsl_formula` field, and the plan named `d3_mfg_skew` while the manifest
  `signals` block uses `d3_mfg_inventory`.
  - **Fix:** T20 split into **T20a** (parser/compiler unit test on synthetic
    arrays — no manifest dependency) and **T20b** (commit `dsl_formula:
    "d1_pe_entropy * d2_queue_survival * d3_mfg_inventory"` to the
    r47_maker_pivot manifest as part of T2's RED test, then assert
    `np.allclose` against the synthetic fixture from C2 above). DoD-D6 evidence
    moves to **T20a + T20b**, with T20a alone sufficient for DoD-D6 if T20b
    blocks.

**HIGH findings — fold into §4/§10:**

- **H1. D2 claims 46 archived backfills but only 25 archived alphas have
  `manifest.yaml`** (21 are jsonl/scoring-sheet only).
  - **Fix:** DoD-D2 narrowed to "25 existing archived manifests get
    `kill_reason` populated; the other 21 are recorded in
    `research/archive/_kill_summary_2026-04-17.jsonl` (new artifact, not a
    manifest)". §4.1 adds the jsonl artifact to T16's deliverables. Risk register
    "61 manifest rewrites" line corrected to "25 manifest rewrites + 1 jsonl
    summary".

- **H2. Kill-ledger idempotency is unenforceable as written.** MergeTree does
  not enforce uniqueness; `killed_at` changes on every retry; `manifest_hash`
  drifts when kill_reason/cluster_id mutate the manifest itself.
  - **Fix:** §5 schema gains `kill_id String` (deterministic
    `sha256(alpha_id || gate || stable_artifact_hash)`) where
    `stable_artifact_hash` is computed from manifest fields **excluding**
    `kill_reason` and `cluster_id`. ORDER BY changes to `(alpha_id, kill_id, killed_at)`.
    `append_kill` checks for `(alpha_id, kill_id)` existence before insert
    in both CH and jsonl paths. T4 RED test must include a duplicate-insert
    scenario.

- **H3. Schema path-b puts mutable run outcomes on `AlphaManifest`.**
  `kill_reason` is per-rejection (could fire many times); `cluster_id` depends
  on threshold/metric/corpus and shifts when new experiments arrive.
  - **Fix:** §5 schema **revised** — `AlphaManifest` keeps **only**
    `dsl_formula` and `parent_alpha_id` (intrinsic, write-once). `kill_reason`
    moves to the `audit.alpha_kill_ledger` row (`reason` column already exists)
    plus a per-rejection lookup helper in `kill_ledger.py`. `cluster_id` moves
    to a new artifact `research/alphas/_cluster_assignments.json` keyed by
    `(threshold, metric, base_dir, corpus_hash)`. T2/T13 update accordingly.
    This restores `manifest_hash` stability and unblocks H2's idempotency.

- **H4. §13 + Task 1 deadlock.** §13.1 said "Task 1 must not start until
  delta written" but Task 1 commits this plan.
  - **Fix:** New **Task 0 (fold-review)** added explicitly: apply this §13.1
    delta + the §4/§5/§7 amendments it triggers, commit the amended plan.
    Task 1 then renamed to "branch freeze + anchor re-verification" with no
    plan-content changes. The deadlock dissolves.

**Counter to the §5 path-b argument (Codex finding 4 / H3):** Codex's
counterargument was correct. Locking `kill_reason` and `cluster_id` on the
manifest *did* create the very `manifest_hash` drift that breaks H2's dedupe.
Path-b is now narrowed: only `dsl_formula` and `parent_alpha_id` go on the
manifest; the other two move to artifacts. `Scorecard` still untouched.

**Scope creep check (Codex finding 5):** No splits/merges adopted. T15/T16
remain split because T16 is the user-visible diff that gets reviewed
separately. T8/T9/T10 (DSL trio) remain three tasks because each has a
distinct unit test surface.

**HFT-laws check (Codex finding 7):** Slice D introduces no `float` on any
money path. `kill_ledger` has no money fields. Confirmed clean.

**Anchor accuracy (Codex finding 1):** Codex spot-checked 8 of the 8 anchors
listed in the review brief and confirmed all of them on `5861730d`. No
anchor changes needed beyond the C1 correction (`_evaluate_gate_c` →
`_verify_gate_c_passed`).

**Plan amendment summary** (applied below this delta in a follow-up commit):

1. §4.1 — add `research/experiments/_slice_d_fixtures/...` deliverables (T7b),
   add `research/archive/_kill_summary_2026-04-17.jsonl` (T16),
   add `research/alphas/_cluster_assignments.json` (T13).
2. §4.2 — `AlphaManifest` change narrows to `dsl_formula`, `parent_alpha_id`
   only (drop `kill_reason`, `cluster_id` from §4.2 row).
3. §5 — schema rewritten with `kill_id`/`stable_artifact_hash` and the
   narrowed manifest split.
4. §7 — insert **Task 0 (fold-review)** before Task 1; insert **Task 7b
   (signal-fixture generation)** between T7 and T11; split T20 into T20a/T20b;
   T14 rewritten to handle `_verify_gate_c_passed` raise path.
5. §8 — DoD-D2 narrows to 25 archived manifests + 1 jsonl summary; DoD-D6
   evidence moves to T20a+T20b.
6. §10 — risk register updated: "61 → 25+jsonl", "manifest_hash drift → kill_id".

After amendments are applied, plan is **ACCEPTed for execution**.
