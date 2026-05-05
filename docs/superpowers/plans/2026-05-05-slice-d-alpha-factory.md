# Slice D — Alpha Factory MVP (kill ledger + screener + cluster + DSL)

> Phase 3 of the alpha-promotion overhaul. Master blueprint:
> `/home/charlie/.claude/plans/curried-launching-unicorn.md` (Slice D §).
> Predecessors: Slice A (PR #337, `66f3eb8a`), Slice C (PR #339, `5861730d`),
> Slice B (PR #340, branch `slice-b/maker-realism` open at `46bf3299`).
> This rewrite (2026-05-05) folds Codex adversarial-review findings (task
> `bvsi1ogg6`) into the plan body. The original draft + delta lives on
> commit `f717f649`; this rewrite supersedes it.

---

## §0 Spec reference

This plan was drafted on Slice C base `5861730d` (Slice C merged 2026-05-05)
and rewritten 2026-05-05 after Codex adversarial review. All file:line anchors
were re-verified against `5861730d`. The master blueprint's Slice D anchors
had drifted in three places, and Codex caught four more during review:

| Blueprint / draft claim | Verified reality on `5861730d` | Source |
|---|---|---|
| `correlation_tracker.py` lives in `src/hft_platform/...` | Lives at `research/registry/correlation_tracker.py:21-54` | `find` |
| `flag_redundant` method at `correlation_tracker.py:21-54` | Method `flag_redundant` on `CorrelationTracker:38`; delegates to `pool.flag_redundant_pairs:127` | `grep` |
| 4 active alphas | 15 alphas have `manifest.yaml`; `r47_skew_mr` has no manifest; `_templates` is a templates dir | `find research/alphas -name manifest.yaml` |
| `factor_compiler.py:139-147` rolling-state leak | File does not exist on `5861730d` (was on parked `wip/pre-slice-a-2026-05-03` only) — anachronistic risk dropped from §10 | `find` no result |
| `data_fingerprint`/`rng_seed` could be added to `AlphaManifest` (path a) | Already on `Scorecard:51-58, 82-83` — duplication rejected | `grep` |
| Auto-kill hooks `_evaluate_gate_c` (Codex C1) | Gate C is enforced by `_verify_gate_c_passed()` at `promotion.py:386-420` which **raises**, not returns `(False, reasons)` | `grep` |
| D3 has a signal corpus to cluster (Codex C2) | `research/experiments/` has zero committed `meta.json`/`signals.npy`; `ExperimentTracker.latest_signals_by_alpha()` returns empty | `find research/experiments` |
| `r47_maker_pivot.dsl_formula` exists (Codex C3) | Field absent from current manifest; signals key is `d3_mfg_inventory`, not the draft's `d3_mfg_skew` | `cat` |
| 46 archived alphas have `manifest.yaml` (Codex H1) | 46 directories exist under `research/archive/alphas_2026-04-17/`, but only 25 have `manifest.yaml` | `find research/archive ... -name manifest.yaml` |

This document is the canonical spec for the slice; the blueprint § is reference
only.

## §1 Goal

Replace the human-reviewed alpha-research workflow with an automated pipeline:
candidate alphas pass through a **cheap screener** (IC + turnover + cost-floor),
correlation **clustering** flags redundant pairs, **kill rejections** are
written to a structured ledger (jsonl + ClickHouse), and a minimal **DSL**
allows `r47_maker_pivot`'s formula to round-trip between text and executable
code.

## §2 Non-goals

- Greenfield DSL features beyond round-tripping `r47_maker_pivot`.
- Mass re-scoring of the archived alphas (only kill-reason backfill, no PnL
  re-run).
- Replacing Gates A–F. Slice D adds an upstream filter and a downstream ledger;
  the gate machinery from Slices A/B/C is reused untouched.
- Greenfield correlation algorithms. We wrap existing
  `pool.compute_pool_matrix` + `pool.flag_redundant_pairs` with hierarchical
  (single-linkage, ρ≥0.7) clustering.
- Live broker integration. Slice D is research-side only; the kill ledger
  writes to `audit.alpha_kill_ledger`, not to live trading topics.

## §3 Why

Slice A made promotion gates strict. Slice B made the backtest realistic.
Slice C made live/replay parity a gate. None of them solves the **inbound**
problem: candidate alphas still arrive at Gate A faster than humans can sift,
and rejected alphas leave no machine-readable record. R47-OE1's failure mode
also exposed a redundancy gap — `r47_maker_pivot` and the c14/c17/c60/c63/c72
maker family share microstructure features but no tree had a documented
correlation pair.

Slice D closes both:

1. **Throughput** — `hft alpha screen <id>` produces a kill verdict in <60s
   per alpha, replacing the per-round human ScoringSheet.
2. **Auditability** — `audit.alpha_kill_ledger` accumulates every rejection
   with a deterministic `kill_id`, so retries don't dupe and downstream
   operators can reconstruct *why* an alpha was killed.
3. **Redundancy** — `hft alpha cluster` produces `cluster_id` per alpha,
   persisted to a sidecar artifact (not the manifest, to preserve manifest
   immutability). Future round summaries can flag siblings automatically.

## §4 Surface

### §4.1 New files

| Path | Purpose | Owner task |
|---|---|---|
| `src/hft_platform/migrations/clickhouse/20260505_001_create_alpha_kill_ledger.sql` | CH table `audit.alpha_kill_ledger` (with `kill_id` deterministic dedupe key) | T3 |
| `src/hft_platform/alpha/kill_ledger.py` | Append-only ledger (jsonl + CH mirror) with idempotent `append_kill` | T4 |
| `src/hft_platform/alpha/screener.py` | `cheap_screen(alpha_id) -> ScreenResult` | T6 |
| `src/hft_platform/alpha/cluster.py` | Hierarchical clustering wrapper; writes to `_cluster_assignments.json` artifact | T7 |
| `research/experiments/_slice_d_fixtures/<alpha_id>/runs/<run_id>/{meta.json,signals.npy}` | Deterministic synthetic signal corpus for the 15 manifest-bearing alphas | T7b |
| `src/hft_platform/alpha/dsl/__init__.py` | DSL package marker | T8 |
| `src/hft_platform/alpha/dsl/parser.py` | DSL text → AST | T8 |
| `src/hft_platform/alpha/dsl/compiler.py` | AST → executable callable | T9 |
| `src/hft_platform/alpha/dsl/formula_context.py` | Round-trip + manifest binding | T10 |
| `research/alphas/_cluster_assignments.json` | Cluster artifact keyed by `(threshold, metric, base_dir, corpus_hash)` | T13 |
| `research/archive/_kill_summary_2026-04-17.jsonl` | Kill-summary line per archived non-manifest alpha (covers the 21 directories without `manifest.yaml`) | T16 |
| `scripts/migrate_alpha_manifests.py` | Backfill `kill_reason` for the 25 archived alphas with manifests; emit `_kill_summary_2026-04-17.jsonl` for the 21 without | T15 |
| `tests/integration/test_alpha_factory_e2e.py` | DoD-D1..D6 evidence | T17,18,19 |
| `tests/unit/alpha/test_screener.py` | Unit coverage for screener | T6 |
| `tests/unit/alpha/test_cluster.py` | Unit coverage for clustering | T7 |
| `tests/unit/alpha/test_dsl_round_trip.py` | DoD-D6 golden round-trip | T20a, T20b |
| `tests/unit/alpha/test_kill_ledger.py` | Ledger jsonl + CH fallback + idempotency | T4 |
| `tests/unit/migrations/test_alpha_kill_ledger_schema.py` | Schema sanity | T3 |
| `docs/runbooks/alpha-factory.md` | Operator runbook | T21 |

### §4.2 Modified files

| Path | Change | Owner task |
|---|---|---|
| `research/registry/schemas.py` | `AlphaManifest` gains **only** `dsl_formula` + `parent_alpha_id` (intrinsic, write-once). `kill_reason` and `cluster_id` deliberately NOT on manifest — see §5. | T2 |
| `research/alphas/r47_maker_pivot/manifest.yaml` | Adds `dsl_formula: "d1_pe_entropy * d2_queue_survival * d3_mfg_inventory"` (matching the actual `signals` block keys, not the draft's `d3_mfg_skew`) | T2 / T20b |
| `src/hft_platform/alpha/audit.py` | New `log_kill()` mirroring `log_promotion_result()` (line 144) | T5 |
| `src/hft_platform/alpha/promotion.py` | `_verify_gate_c_passed()` (raises at `~promotion.py:386-420`) wrapped in try/except → `log_kill()` → re-raise; `_evaluate_gate_d` rejection at `promotion.py:283` also calls `log_kill()` | T14 |
| `src/hft_platform/cli/_alpha.py` | Add `cmd_alpha_screen`, `cmd_alpha_kill`, `cmd_alpha_cluster` | T11,12,13 |
| `src/hft_platform/cli/_parser.py` | Register the 3 new subcommands at `_parser.py:338-364` (alpha sub-parser block) | T11,12,13 |
| `docs/architecture/current-architecture.md` | Append §7C "Slice D — Alpha Factory MVP" | T21 |
| `docs/operations/env-vars-reference.md` | Add `HFT_KILL_LEDGER_ENABLED` | T21 |

## §5 Schema decisions (path-b narrowed, locked)

The original draft put `dsl_formula`, `parent_alpha_id`, `kill_reason`,
`cluster_id` on `AlphaManifest`. Codex (H3) caught the flaw: `kill_reason`
fires per rejection (could be many), and `cluster_id` depends on
`(threshold, metric, corpus, tie-breaking)` — both are mutable run
outcomes, not intrinsic alpha properties. Mutating the manifest changes
`manifest_hash`, which broke the kill-ledger dedupe key (Codex H2).

**Final split:**

```python
# AlphaManifest (research/registry/schemas.py) — only intrinsic, write-once
dsl_formula: str | None = None        # round-trippable formula text; None for legacy alphas
parent_alpha_id: str | None = None    # genealogy; None for greenfield
# kill_reason and cluster_id are deliberately NOT here.
```

```python
# kill_reason lives on each kill_ledger row (the `reason` column already
# exists; no schema change). A helper `kill_ledger.latest_reason(alpha_id)`
# returns the most-recent ledger row's reason.
```

```json
// research/alphas/_cluster_assignments.json — keyed by clustering parameters
{
  "schema_version": 1,
  "assignments": {
    "<threshold>:<metric>:<base_dir_hash>:<corpus_hash>": {
      "computed_at_ns": 1762345296789012345,
      "alphas": {
        "r47_maker_pivot": {"cluster_id": "cluster_0", "cluster_size": 3, "max_intra_cluster_corr": 0.81},
        "c60_tmfd6_r47_minimal_inst_rt": {"cluster_id": "cluster_0", "cluster_size": 3, "max_intra_cluster_corr": 0.81}
      }
    }
  }
}
```

`Scorecard` is **untouched** — `data_fingerprint` and `rng_seed` already live
there.

`audit.alpha_kill_ledger` schema (CH):

```sql
CREATE TABLE IF NOT EXISTS audit.alpha_kill_ledger (
    kill_id              String                      NOT NULL, -- sha256(alpha_id || gate || stable_artifact_hash)
    killed_at            DateTime64(9, 'UTC')        DEFAULT now64(9, 'UTC'),
    alpha_id             String                      NOT NULL,
    gate                 Enum8('A'=1,'B'=2,'C'=3,'D'=4,'E'=5,'F'=6,'pre_screen'=7,'cluster'=8,'manual'=9),
    reason               String                      NOT NULL,
    stable_artifact_hash String                      DEFAULT '', -- sha256 over manifest fields excluding kill_reason/cluster_id
    scorecard_id         String                      DEFAULT '',
    killed_by            String                      DEFAULT 'system'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(killed_at)
ORDER BY (alpha_id, kill_id, killed_at)
TTL killed_at + INTERVAL 365 DAY
SETTINGS index_granularity = 8192;
```

**Idempotency contract** (Codex H2):

- `kill_id = sha256(alpha_id || ':' || gate || ':' || stable_artifact_hash)`
- `stable_artifact_hash` is `sha256` over a canonical JSON of the manifest
  with `kill_reason`/`cluster_id` keys excluded (those don't exist on the
  manifest after the §5 narrow, but the exclusion is defensive).
- `append_kill` runs a `SELECT count() FROM audit.alpha_kill_ledger WHERE
  alpha_id = ? AND kill_id = ?` pre-check; if >0, no insert. Same dedupe in
  the jsonl path via in-memory cache + on-disk grep on append.
- T4 RED test must include a duplicate-insert scenario — second call MUST be
  a no-op.

365d TTL mirrors Slice C's `hft.order_intents`. Reuses `_write_fallback`
jsonl pattern from `audit.py:42-55` for the offline path
(`research/alphas/_kill_ledger.jsonl`).

## §6 Pre-flight

1. **WIP-park** — `git checkout -b wip/pre-slice-d-2026-05-05` and commit any
   in-flight working-tree changes (the main checkout currently has 27 dirty
   files on `loop-v1/convergence`; do not touch them in Slice D's tree).
2. **Worktree isolation** — work in `/tmp/slice-d-wt` (already created at
   detached `5861730d`, branch `slice-d/alpha-factory-mvp` already exists at
   `f717f649` with the original draft + Codex delta committed).
3. **Slice C base** — confirm `git rev-parse origin/main` → `5861730d`.
4. **Anchors freeze** — Task 1 must re-run `find research/alphas -name
   manifest.yaml` and confirm 15 hits; re-grep `_verify_gate_c_passed` and
   `_evaluate_gate_d` to confirm both still exist on the merge base.
5. **Slice B awareness** — if Slice B PR #340 merges before this slice ships,
   re-baseline (`git fetch origin main && git rebase origin/main`) and
   re-verify §4.2 anchors. Slice B touches `promotion.py:283 _evaluate_gate_d`,
   which T14 also touches — rebase is **mandatory** in that case.

## §7 Tasks

> RED→GREEN→commit cadence. One subagent per task (per
> `superpowers:subagent-driven-development`). Every task ends with `make ci`
> green or a documented partial-pass and a commit.

### Task 0 — Fold Codex review into plan (this rewrite)

- Already executed in this rewrite commit. Plan now ACCEPTED for execution.
- No additional code changes; just the plan amendment commit on
  `slice-d/alpha-factory-mvp`.

### Task 1 — Pre-flight + plan freeze

- Verify worktree HEAD descends from `5861730d`; current branch is
  `slice-d/alpha-factory-mvp`.
- Re-verify §0 anchors with `grep -n` and `find`.
- Confirm 15 manifest.yaml hits under `research/alphas/`, 25 under
  `research/archive/alphas_2026-04-17/`.
- This task closes the freeze; T2 starts implementation.

### Task 2 — `AlphaManifest` schema extension (path-b narrowed)

- Files: `research/registry/schemas.py`,
  `tests/unit/research_registry/test_schemas.py`,
  `research/alphas/r47_maker_pivot/manifest.yaml`.
- RED: write a failing `test_alpha_manifest_round_trip_with_new_fields` that
  constructs an `AlphaManifest` with `dsl_formula="d1_pe_entropy *
  d2_queue_survival * d3_mfg_inventory"` and `parent_alpha_id="r47_maker"`,
  then `to_dict()` → `from_dict()` and asserts equality. Also a
  `test_legacy_manifest_loads_without_new_fields` to lock backwards compat.
- GREEN: extend the dataclass + `to_dict` / `from_dict` to round-trip the two
  new fields. Default both to `None`. `from_dict` uses `.get(...)` not `[...]`.
- Commit `dsl_formula` to `research/alphas/r47_maker_pivot/manifest.yaml`
  using the manifest's actual signal keys (`d1_pe_entropy`,
  `d2_queue_survival`, `d3_mfg_inventory`) — not the draft's `d3_mfg_skew`.
- Verify: `uv run pytest tests/unit/research_registry/test_schemas.py -q` passes;
  loading every existing `manifest.yaml` produces no warnings.

### Task 3 — `audit.alpha_kill_ledger` migration

- Files:
  `src/hft_platform/migrations/clickhouse/20260505_001_create_alpha_kill_ledger.sql`,
  `tests/unit/migrations/test_alpha_kill_ledger_schema.py`.
- Schema as in §5 with `kill_id String` first column, ORDER BY `(alpha_id,
  kill_id, killed_at)`.
- Mirrors Slice C's `20260504_001_create_order_intents.sql` shape.
- Verify migration filename ordering: post Slice C (`20260504_001`) and
  before any subsequent migration.

### Task 4 — `kill_ledger.py` writer + jsonl fallback + idempotency

- Files: `src/hft_platform/alpha/kill_ledger.py`,
  `tests/unit/alpha/test_kill_ledger.py`.
- API:
  ```python
  @dataclass(frozen=True, slots=True)
  class KillRecord:
      alpha_id: str
      gate: str                       # one of {'A','B','C','D','E','F','pre_screen','cluster','manual'}
      reason: str
      stable_artifact_hash: str = ""
      scorecard_id: str = ""
      killed_by: str = "system"
      killed_at: int = 0              # ns; 0 means "fill in now_ns()"

      def kill_id(self) -> str:       # deterministic
          payload = f"{self.alpha_id}:{self.gate}:{self.stable_artifact_hash}"
          return hashlib.sha256(payload.encode()).hexdigest()

  def append_kill(record: KillRecord) -> bool: ...   # returns True if inserted, False if dedupe-skipped
  def read_kills(alpha_id: str | None = None) -> list[KillRecord]: ...
  def latest_reason(alpha_id: str) -> str | None: ...
  def stable_artifact_hash(manifest: AlphaManifest) -> str: ...  # sha256 of canonical-json with kill_reason/cluster_id excluded
  ```
- jsonl path: `research/alphas/_kill_ledger.jsonl` (append-only; gitignore
  must cover it — verify `.gitignore`, add if missing).
- CH path: insert via `_get_client()` from `audit.py:68`; pre-check with
  `SELECT count() WHERE alpha_id=? AND kill_id=?`.
- Mirror `_write_fallback` semantics: CH failure → jsonl; jsonl failure →
  log warning, do not raise.
- RED tests must include:
  - duplicate insert (same `KillRecord`) returns `False` and ledger row count
    stays 1 (CH path + jsonl path);
  - different `gate` → different `kill_id` → both inserted;
  - `stable_artifact_hash` is invariant across `kill_reason`/`cluster_id`
    field mutations (the latter no longer on manifest, but we test the
    exclusion list defensively).

### Task 5 — `audit.log_kill()`

- Files: `src/hft_platform/alpha/audit.py`.
- Mirror `log_promotion_result()` at line 144.
- Increments `alpha_kill_results_total{alpha_id, gate, reason_class}` Prometheus
  counter (verify `observability/metrics.py:1128-1155` to check counter
  registration pattern; add new counter if absent).
- `reason_class` is a coarsened bucket (e.g. `inventory_mtm`,
  `cost_uncertainty`, `latency`, `replay_parity`, `screener_ic`,
  `screener_turnover`, `screener_cost_floor`, `cluster_redundant`, `manual`)
  to keep label cardinality bounded.
- Verify: `uv run pytest tests/unit/alpha/test_audit.py -q` (extend existing).

### Task 6 — `screener.py` cheap screener

- Files: `src/hft_platform/alpha/screener.py`,
  `tests/unit/alpha/test_screener.py`.
- API:
  ```python
  @dataclass(frozen=True, slots=True)
  class ScreenResult:
      alpha_id: str
      verdict: str            # 'pass' | 'kill' | 'unknown'
      ic_mean: float
      ic_std: float
      turnover: float
      cost_floor_breach: bool
      reason: str             # populated when verdict in {'kill','unknown'}
      duration_s: float

  def cheap_screen(alpha_id: str, *, project_root: Path = Path(".")) -> ScreenResult: ...
  ```
- Reuses `research/tools/feature_screener.py:1-100` `FeatureResult` for IC.
- Adds turnover gate (default >2.0/day → `verdict='kill'`) + cost-floor
  pre-check (reuses `cost_floor_per_fill_pts` threshold pattern from Slice B).
- Hard time budget enforced via `time.monotonic()` (Codex §9 self-review
  constraint). 60s budget per alpha (DoD-D1).
- When signals are missing, returns `verdict='unknown'` (advisory, NOT
  `'kill'` — see §10 risk row).

### Task 7 — `cluster.py` hierarchical clustering (writes sidecar artifact)

- Files: `src/hft_platform/alpha/cluster.py`,
  `tests/unit/alpha/test_cluster.py`.
- API:
  ```python
  class EmptyCorpusError(RuntimeError): ...   # raised when no signals to cluster

  @dataclass(frozen=True, slots=True)
  class ClusterAssignment:
      alpha_id: str
      cluster_id: str
      cluster_size: int
      max_intra_cluster_corr: float

  def cluster_alphas(*, base_dir: str = "research/experiments",
                     threshold: float = 0.7,
                     metric: str = "pearson",
                     write_artifact: bool = False) -> list[ClusterAssignment]: ...
      # raises EmptyCorpusError if compute_pool_matrix returns empty payload.
      # if write_artifact, persists results into research/alphas/_cluster_assignments.json
      # under key f"{threshold}:{metric}:{sha256(base_dir)}:{corpus_hash}".
  ```
- Wraps `pool.compute_pool_matrix(*, base_dir, sample_step)` (verified at
  `pool.py:77`) and `pool.flag_redundant_pairs(matrix_payload, *, threshold,
  metric)` (verified at `pool.py:127`).
- Single-linkage agglomerative clustering on `1 - |corr|` distance, cut at
  `1 - threshold` (defaults to 0.3 for ρ=0.7).
- **Determinism contract** (Codex §10 H mitigation): sort `alpha_ids`
  lexicographically before clustering; `cluster_id` is `cluster_<index>`
  where index is the rank of the cluster's lex-min alpha among all clusters
  also sorted by lex-min alpha. Singletons: `singleton_<alpha_id>`.
- T7's RED test fixes a synthetic 4-alpha correlation matrix and asserts
  identical `ClusterAssignment` lists across 100 reruns.

### Task 7b — Synthetic signal corpus generation

- Files:
  `research/experiments/_slice_d_fixtures/<alpha_id>/runs/2026-05-05_seed42/{meta.json,signals.npy}`
  for each of the 15 manifest-bearing alphas.
- Generates deterministic signals seeded `42`, with intra-R47-family
  correlation forced to ρ≈0.81 by construction:
  `r47_maker_pivot`, `c60_tmfd6_r47_minimal_inst_rt`,
  `c63_txfd6_r47_tight_spread`, `c72_tmfd6_queue_position_aware` share a
  latent factor; the other 11 are independent Gaussians.
- `meta.json` schema mirrors what `ExperimentTracker.latest_signals_by_alpha()`
  expects (`alpha_id`, `run_id`, `created_at_ns`, `signals_path`,
  `proxy_returns_path`).
- Synthetic returns are pure noise; the corpus is for *clustering* (DoD-D3),
  not screening (DoD-D1).
- Committed to git so DoD-D3 is deterministic in CI; bytes are <2 MB total
  (15 × 100 KB float32 array).
- Generation script: `scripts/generate_slice_d_signal_corpus.py` (committed
  alongside the artifacts).

### Task 8 — `alpha/dsl/parser.py`

- Files: `src/hft_platform/alpha/dsl/__init__.py`,
  `src/hft_platform/alpha/dsl/parser.py`,
  `tests/unit/alpha/test_dsl_parser.py`.
- Minimum grammar — enough to express
  `d1_pe_entropy * d2_queue_survival * d3_mfg_inventory`:
  identifiers, `*`, `+`, `-`, `(`, `)`, numeric literals.
- AST: `BinOp`, `UnaryOp`, `Identifier`, `Literal`.
- No greenfield operators beyond what `r47_maker_pivot` requires.
- Parser is a hand-rolled recursive-descent (no `eval`, no `compile`, no
  `ast.parse`).

### Task 9 — `alpha/dsl/compiler.py`

- Files: `src/hft_platform/alpha/dsl/compiler.py`,
  `tests/unit/alpha/test_dsl_compiler.py`.
- AST → callable `(features: dict[str, np.ndarray]) -> np.ndarray`.
- Tree-walk interpreter only. No `eval()`/`exec()`. **Explicit ban** on
  `getattr` / `__import__` shortcuts (Codex §13.1 critical safety check).
- Verify: simple cases (`a*b`, `a+b`, `(a+b)*c`) match numpy directly.

### Task 10 — `alpha/dsl/formula_context.py`

- Files: `src/hft_platform/alpha/dsl/formula_context.py`,
  `tests/unit/alpha/test_dsl_formula_context.py`.
- API:
  ```python
  def round_trip(formula: str) -> str: ...   # parse → unparse, must equal canonicalized input
  def bind_to_manifest(formula: str, manifest: AlphaManifest) -> AlphaManifest: ...
  ```
- `bind_to_manifest` updates `manifest.dsl_formula`; raises if formula
  references identifiers not in `manifest.data_fields`.

### Task 11 — `cmd_alpha_screen`

- Files: `src/hft_platform/cli/_alpha.py`, `src/hft_platform/cli/_parser.py`.
- Mirror `cmd_alpha_promote:243` shape.
- Subcommand:
  `hft alpha screen <alpha_id> [--threshold-ic 0.005] [--threshold-turnover 2.0] [--write-kill]`.
- `--write-kill`: when verdict == `'kill'`, invoke `kill_ledger.append_kill()`
  with `gate='pre_screen'`, `reason=<screener.ScreenResult.reason>`.
- Register in `_parser.py:338-364` block.

### Task 12 — `cmd_alpha_kill`

- Subcommand: `hft alpha kill <alpha_id> --reason <text> [--gate manual]`.
- Wraps `kill_ledger.append_kill()` directly (manual operator path).
- **Does NOT mutate `manifest.yaml`** (kill_reason is no longer a manifest
  field — see §5).
- Rejects empty / whitespace-only `--reason`.

### Task 13 — `cmd_alpha_cluster` + `_cluster_assignments.json`

- Subcommand:
  `hft alpha cluster [--threshold 0.7] [--metric pearson] [--write-artifact]`.
- Calls `cluster_alphas(write_artifact=…)`, prints assignments table.
- Writes `research/alphas/_cluster_assignments.json` (sidecar) keyed by
  `(threshold, metric, base_dir_hash, corpus_hash)`.
- **Does NOT mutate `manifest.yaml`** (cluster_id is no longer a manifest
  field — see §5).

### Task 14 — `promote_alpha` auto-kill on Gate-C raise + Gate-D rejection

- Files: `src/hft_platform/alpha/promotion.py`,
  `tests/unit/test_alpha_promotion.py`.
- **Gate-C path (Codex C1 fix):** wrap `_verify_gate_c_passed()` (raises at
  `promotion.py:386-420`) in `try` / `except` (catch the specific exception
  type used there — read the file at task start) → call
  `audit.log_kill(alpha_id, gate='C', reason=<exception.message>,
  stable_artifact_hash=…)` → re-raise.
- **Gate-D path:** after `_evaluate_gate_d` returns `(False, reasons)` at
  `promotion.py:283`, call `audit.log_kill(alpha_id, gate='D',
  reason=<aggregated reasons>, …)` before propagating the rejection.
- Gated by `HFT_KILL_LEDGER_ENABLED` env var (default `1`).
- Idempotent: T4's `kill_id` dedupe means re-running `promote_alpha` on an
  unchanged manifest produces no duplicate ledger rows.
- T14 RED test must include: (a) Gate-C raise path produces ledger row with
  `gate='C'`; (b) Gate-D reject path produces ledger row with `gate='D'`;
  (c) re-running both paths is a no-op on the ledger.

### Task 15 — `scripts/migrate_alpha_manifests.py`

- Walks `research/archive/alphas_2026-04-17/`:
  - For each of the 25 directories with `manifest.yaml`: parse the round
    summary / scoring sheet (best-effort) and back-fill the corresponding
    kill ledger row (NOT the manifest — the manifest is intrinsic). If parse
    fails, fallback `reason='archived_2026_04_17'`.
  - For each of the 21 directories without `manifest.yaml`: emit a row to
    `research/archive/_kill_summary_2026-04-17.jsonl` with
    `{alpha_id, killed_at_iso='2026-04-17T00:00:00Z',
    reason='archived_2026_04_17_no_manifest'}`.
- One-shot script. Idempotent: re-running with same data produces no diff.
  Achieved via `kill_ledger.append_kill` dedupe (same `kill_id`).
- Default `--dry-run`; `--apply` required to write.
- T15 commits the script alone; T16 commits the produced data.

### Task 16 — Manifest backfill execution

- Run `scripts/migrate_alpha_manifests.py --dry-run`, verify diff.
- Run `scripts/migrate_alpha_manifests.py --apply`, commit produced
  `research/archive/_kill_summary_2026-04-17.jsonl` + any kill-ledger jsonl
  appends.
- DoD-D2 evidence: 25 archived alphas have ledger rows; 21 are in the
  summary jsonl.

### Task 17 — DoD-D1 integration test

- Files:
  `tests/integration/test_alpha_factory_e2e.py::test_dod_d1_screen_15_alphas`.
- Iterates the 15 alphas with `manifest.yaml`, runs `cheap_screen`, asserts
  each completes in <60s wall-clock (`time.monotonic`).
- 95th-percentile <60s under default CI runner (≥2 cores, ≥4 GB RAM).
- For alphas without committed signals, accept `verdict='unknown'` — only
  measure timing (the budget is the DoD, not the verdict).

### Task 18 — DoD-D3 cluster pair detection

- Files:
  `tests/integration/test_alpha_factory_e2e.py::test_dod_d3_cluster_finds_r47_family`.
- Uses the deterministic corpus from T7b. Calls `cluster_alphas(threshold=0.7,
  metric='pearson', base_dir='research/experiments/_slice_d_fixtures',
  write_artifact=False)`.
- Asserts at least one cluster contains both `r47_maker_pivot` and at least
  one of `{c60_tmfd6_r47_minimal_inst_rt, c63_txfd6_r47_tight_spread,
  c72_tmfd6_queue_position_aware}` (the latent-factor siblings from T7b).
- Asserts `cluster_alphas` raises `EmptyCorpusError` when invoked against an
  empty `base_dir`.

### Task 19 — DoD-D4 ledger row from Gate-C/D rejection

- Files:
  `tests/integration/test_alpha_factory_e2e.py::test_dod_d4_kill_ledger_row_on_gate_c_raise`,
  `::test_dod_d4_kill_ledger_row_on_gate_d_reject`,
  `::test_dod_d4_kill_ledger_idempotent_under_retry`.
- Calls `promote_alpha` on a fixture alpha with intentionally failing
  Gate-C / Gate-D thresholds. Asserts a row appears in
  `audit.alpha_kill_ledger` (CH-mocked client + jsonl fallback verified
  separately).
- Idempotency test: rerun produces zero new rows; `kill_id` matches.

### Task 20a — DSL parser + compiler synthetic round-trip

- Files: `tests/unit/alpha/test_dsl_round_trip.py::test_synthetic_formula_round_trip`.
- Parses `"a * b * c"` and `"(a + b) * c"`, unparses, asserts canonical
  equality. Compiles AST and runs against synthetic numpy arrays; asserts
  bit-exact match against direct numpy expression.
- **DoD-D6 is satisfied by T20a alone** — the manifest binding (T20b) is a
  bonus.

### Task 20b — DSL bound to r47_maker_pivot manifest

- Files: `tests/unit/alpha/test_dsl_round_trip.py::test_r47_maker_pivot_round_trip`.
- Reads `dsl_formula` from the updated `r47_maker_pivot/manifest.yaml`
  (committed in T2). Parses, unparses, canonical equality.
- Compiles the AST and runs against the T7b synthetic signals; asserts
  output is finite and shape-matches input.
- We do NOT compare against an "existing scorecard signal" because no such
  fixture exists on `5861730d` (Codex C3); T20b is purely a binding /
  compilation check.

### Task 21 — Runbook + arch map + env vars

- New `docs/runbooks/alpha-factory.md` mirroring
  `docs/runbooks/replay-parity-gate.md` and `docs/runbooks/maker-realism-gate.md`.
- Append §7C "Slice D — Alpha Factory MVP" surface table to
  `docs/architecture/current-architecture.md`.
- Add `HFT_KILL_LEDGER_ENABLED` (default `1`) to
  `docs/operations/env-vars-reference.md`.

### Task 22 — `make ci` green + ruff format

- `uv run ruff format src/ tests/ scripts/ research/`.
- `make ci` must produce ≥87% coverage (Slice C baseline 87.15%, Slice B
  87.59%).
- Commit format-only fixup.

## §8 Definition of Done

| DoD | Evidence | Owner task |
|---|---|---|
| **D1** — All 15 alphas with `manifest.yaml` produce a `ScreenResult` in <60s each (95th percentile) | `test_dod_d1_screen_15_alphas`; `verdict='unknown'` accepted for alphas without signals (timing is the DoD, not the verdict) | T17 |
| **D2** — 25 archived alphas with `manifest.yaml` get kill-ledger rows; 21 without get jsonl summary rows | manifest diff + jsonl artifact in T16 commit | T16 |
| **D3** — Clustering on the T7b corpus produces a cluster containing `r47_maker_pivot` + at least one R47-family sibling | `test_dod_d3_cluster_finds_r47_family` | T18 |
| **D4** — `audit.alpha_kill_ledger` accumulates rows from both Gate-C raise and Gate-D reject; rerun is idempotent | `test_dod_d4_kill_ledger_row_on_gate_c_raise` + `..._gate_d_reject` + `..._idempotent_under_retry` | T19 |
| **D5** — Cold-start subagent can run `hft alpha screen <id>` end-to-end | runbook §"Operator quick reference" | T21 |
| **D6** — DSL round-trips a synthetic 3-operator formula bit-exact (T20a) and binds to `r47_maker_pivot.dsl_formula` (T20b) | `test_synthetic_formula_round_trip` + `test_r47_maker_pivot_round_trip` | T20a, T20b |
| **D7** — `make ci` green with coverage ≥87% | CI log | T22 |

## §9 Self-review checklist

Before opening the PR:

- [ ] All §4 files touched match the §7 task ownership column (no orphan
  files, no mystery diffs).
- [ ] `AlphaManifest` round-trip preserves `dsl_formula` and
  `parent_alpha_id`; `kill_reason` and `cluster_id` deliberately absent.
- [ ] `_write_fallback` jsonl path follows `audit.py:42-55` pattern verbatim.
- [ ] `cheap_screen` 60s budget enforced via `time.monotonic()`.
- [ ] `cluster_alphas` deterministic — same input → same `cluster_id` strings
  across 100 reruns (lex-sort assignment).
- [ ] `cluster_alphas` raises `EmptyCorpusError` on empty corpus (no silent
  empty list).
- [ ] `cmd_alpha_kill --reason` rejects empty / whitespace-only reasons.
- [ ] `promote_alpha` auto-kill is idempotent — `kill_id` deduplicates both
  Gate-C raise and Gate-D reject paths.
- [ ] `migrate_alpha_manifests.py` is dry-run-by-default; `--apply` required
  to write.
- [ ] DSL parser rejects identifiers not in `manifest.data_fields`
  (`bind_to_manifest` invariant).
- [ ] DSL compiler does NOT use `eval()`, `exec()`, `compile()`, `getattr`
  on user input, or `__import__`.
- [ ] No new `unwrap()` / `panic!()` in any Rust path (Slice D is
  Python-only — flag if not).
- [ ] No `float` in `kill_ledger.py` for monetary fields (the ledger has no
  money fields — confirm).
- [ ] `HFT_KILL_LEDGER_ENABLED=0` short-circuits cleanly (no CH connection
  attempt, no jsonl write).
- [ ] `_kill_ledger.jsonl`, `_cluster_assignments.json`, and
  `_kill_summary_2026-04-17.jsonl` are `.gitignore`d unless explicitly
  intended to be tracked (the summary IS tracked; the ledger jsonl is NOT).

## §10 Risk register (Slice D-specific)

| Risk | Mitigation |
|---|---|
| `manifest.yaml` schema break — adding 2 fields could break legacy alphas without those keys | Defaults `None`; `from_dict` uses `.get(...)` not `[...]`; T2 RED test must include a legacy fixture |
| Cluster determinism — Python dict ordering changes could re-label `cluster_<index>` | T7 lex-sorts `alpha_ids` before clustering; cluster index is the rank of the lex-min alpha; T7 RED test asserts 100-rerun stability |
| Empty signal corpus → cluster silently returns `[]` (Codex C2) | `cluster_alphas` raises `EmptyCorpusError`; T18 explicit assertion |
| Screen 60s budget breached on slow CK reads | T6 returns `verdict='unknown'` (advisory) when signals missing; D1 measures timing not verdict |
| Backfill of archived alphas — only 25 of 46 have manifests (Codex H1) | T15 splits handling: 25 → ledger rows; 21 → jsonl summary; T16 commits both; D2 narrowed |
| DSL grammar grows beyond `r47_maker_pivot` — scope creep | §2 non-goals locks scope; T8 grammar review must reject any operator not used by `r47_maker_pivot` |
| DSL compiler smuggles in `eval`-equivalent shortcuts (Codex CRITICAL safety) | T9 explicit ban list (`eval`, `exec`, `compile`, `getattr` on user input, `__import__`); §9 self-review checklist enforces |
| Kill-ledger duplicate rows on retry / concurrent promotion (Codex H2) | `kill_id = sha256(alpha_id || gate || stable_artifact_hash)` deterministic; CH pre-check + jsonl in-memory cache; T4 RED test for duplicate insert |
| `manifest_hash` drift after audit events breaks dedupe (Codex H3) | `kill_reason` and `cluster_id` not on manifest; `stable_artifact_hash` excludes them defensively even if added later |
| Gate-C auto-kill missed because `_verify_gate_c_passed` raises (Codex C1) | T14 wraps the raise path; D4 has explicit Gate-C test |
| Slice B PR #340 merges mid-execution → `promotion.py:283` rebase conflict on T14 | §6 pre-flight requires rebase; rebase-and-revalidate is mandatory before T14 |
| `audit.alpha_kill_ledger` adds CH write load — already on PR #339 path | T4's CH write is best-effort with jsonl fallback (mirrors Slice C); load is bounded by alpha-promotion frequency, not tick rate |
| Coverage floor regression from new modules without tests | T6/T7/T8/T9/T10 each ship unit tests; T22 `make ci` gates on ≥87% |
| Synthetic signal corpus inflates repo size | T7b caps each `signals.npy` at ~100 KB float32; total <2 MB; committed bytes audited in T7b commit message |
| `_kill_ledger.jsonl` accidentally committed | T4 verifies `.gitignore` covers it; pre-commit grep blocks the path |

## §11 Out of scope

- Slice C path (a) live 2026-04-21 reconstruction (still user-gated).
- Greenfield DSL operator design beyond round-tripping `r47_maker_pivot`.
- Mass alpha re-scoring (no PnL re-run on archived alphas).
- Multi-broker support changes (Slice D is research-side only).
- Live trading integration (the kill ledger is a research/audit artifact).
- Comparing T20b's compiled DSL output against an "existing scorecard signal"
  — no such fixture exists on `5861730d` (Codex C3); deferred to a post-D
  follow-up if needed.

## §12 Execution handoff

Recommended workflow for the executing operator:

1. Branch `slice-d/alpha-factory-mvp` already exists at this rewrite's
   commit (worktree `/tmp/slice-d-wt`).
2. Drive Tasks 1→22 via `superpowers:subagent-driven-development`. One
   fresh subagent per task, two-stage review between tasks.
3. After T22, open PR with the same template Slice C #339 and Slice B #340
   use: Summary / Goal / Why / What changed / AI Participation / Test Plan
   (one box per DoD-D1..D7) / HFT Design Review / Out of scope.
4. Tag `slice-d-merged-YYYY-MM-DD` post-merge; write
   `slice_d_alpha_factory.md` memory card mirroring
   `slice_c_replay_parity_gate.md`.

---

## §13 Codex adversarial review record

**Initial draft + review:** committed at `f717f649` on
`slice-d/alpha-factory-mvp`. Codex task `bvsi1ogg6`
(2026-05-05). Verdict: ACCEPT-WITH-FIXES (leaning REJECT). Findings folded
into §13.1.

**Rewrite (this commit):** §4–§10 amendments applied per §13.1. Plan now
ACCEPTED for execution. The §13.1 audit log below is preserved verbatim
for traceability.

### §13.1 Codex findings — folded into body

| ID | Severity | Finding | Folded into |
|---|---|---|---|
| C1 | critical | T14 hooked non-existent `_evaluate_gate_c` | §4.2 (T14 row), §7 T14, §10 |
| C2 | critical | D3 had no signal corpus to cluster | §4.1 (T7b row), §7 T7b, §10 |
| C3 | critical | D6 had no `dsl_formula` fixture; signal name mismatch | §4.2 (T2 manifest row), §7 T2 + T20a/b split, §11 |
| H1 | high | 46 archived alphas → only 25 with manifest | §4.1 (T16 jsonl artifact), §7 T15, §8 D2, §10 |
| H2 | high | Kill-ledger idempotency unenforceable on plain MergeTree | §5 (kill_id schema), §7 T4 contract, §10 |
| H3 | high | `kill_reason`/`cluster_id` are mutable run outcomes | §5 (narrowed manifest), §4.2 (T2 row), §7 T12+T13 |
| H4 | high | §13/Task 1 self-referential deadlock | §7 Task 0 added |

**Counter-arguments adopted from Codex:**
- Path-b narrowed (Codex finding 4): only `dsl_formula` + `parent_alpha_id`
  on `AlphaManifest`; `kill_reason` and `cluster_id` moved off-manifest.

**Counter-arguments rejected:**
- None. All Codex findings adopted.

**HFT-laws check (Codex finding 7):** Slice D introduces no `float` on any
money path. `kill_ledger` has no money fields. Confirmed clean.

**Anchor accuracy (Codex finding 1):** All 8 spot-checked anchors confirmed
on `5861730d`. C1 was the only file:line error; corrected.
