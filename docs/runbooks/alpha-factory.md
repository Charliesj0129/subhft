# Alpha Factory Runbook (Slice D)

## What the alpha factory does

Slice D ships the alpha-factory MVP — four loosely-coupled offline
components plus a kill-ledger durability surface. They sit upstream of
the existing promotion pipeline (`hft alpha promote`), filtering and
classifying candidates so only credible alphas reach Gate C / Gate D.

- **Cheap screener** — IC + turnover + cost-floor pre-check with a
  60 s budget per alpha. Verdict ∈ `{'pass', 'kill', 'unknown'}`.
  Source: `src/hft_platform/alpha/screener.py`.
- **Kill ledger** — append-only audit surface keyed by `kill_id`.
  ClickHouse-first (`audit.alpha_kill_ledger`), jsonl fallback
  (`research/alphas/_kill_ledger.jsonl`). Idempotent on re-run.
  Source: `src/hft_platform/alpha/kill_ledger.py`.
- **Correlation clustering** — single-linkage agglomerative on
  `1 - |corr|`; lex-stable cluster_ids. Optional sidecar artifact.
  Source: `src/hft_platform/alpha/cluster.py`.
- **Minimal DSL** — recursive-descent parser, tree-walk interpreter.
  Explicit bans on `eval`, `exec`, `compile`, `__import__`, `getattr`.
  Source: `src/hft_platform/alpha/dsl/{parser,compiler,formula_context}.py`.

## Operator quick reference (DoD-D5)

CLI subcommands and their purpose:

```bash
# Cheap-screen one alpha (60 s budget; verdict pass | kill | unknown).
# On verdict='kill' AND --write-kill, appends a gate='pre_screen' row.
hft alpha screen <alpha_id> \
    [--threshold-ic 0.005] \
    [--threshold-turnover 2.0] \
    [--write-kill]

# Manually record a kill (operator path; idempotent).
hft alpha kill <alpha_id> --reason "<text>" \
    [--gate manual]                # one of A|B|C|D|E|F|pre_screen|cluster|manual
    [--killed-by <operator_id>]

# Hierarchical correlation clustering across all alphas with signals
# under base_dir/runs/. Optional sidecar persistence.
hft alpha cluster \
    [--base-dir research/experiments] \
    [--threshold 0.7] \
    [--metric pearson|spearman] \
    [--write-artifact] \
    [--json]
```

Sample 5-minute end-to-end smoke test (cold-start):

1. Generate the synthetic corpus:
   `uv run python scripts/generate_slice_d_signal_corpus.py`
2. Run the cluster command:
   `hft alpha cluster --base-dir research/experiments/_slice_d_fixtures --json`
3. Confirm the R47 family lands in one cluster.

Exit-code contract (mirrors `cmd_alpha_promote` shape):

| Subcommand | exit 0 | exit 1 | exit 2 |
|---|---|---|---|
| `screen` | verdict ∈ {'pass', 'unknown'} | infra/import failure | verdict == 'kill' |
| `kill` | row inserted (or duplicate) | empty reason / import failure | — |
| `cluster` | clustering succeeded | import failure | empty corpus |

## How to enable / disable auto-kill on promotion failure

`HFT_KILL_LEDGER_ENABLED=1` (default) wires `promote_alpha()` to write
a kill-ledger row when Gate C raises or Gate D rejects. Set
`HFT_KILL_LEDGER_ENABLED=0` to skip the auto-kill writes — useful for
debugging or operator-driven sweeps where you don't want every dry run
leaving rows.

The auto-kill path uses `gate='C'` for Gate-C exceptions and
`gate='D'` for Gate-D rejections. The CLI manual path uses
`gate='manual'` (or whatever `--gate` is supplied).

## Reading `audit.alpha_kill_ledger`

ClickHouse query for "what got killed in the last 7 days":

```sql
SELECT
    killed_at,
    alpha_id,
    gate,
    reason,
    killed_by
FROM audit.alpha_kill_ledger
WHERE killed_at >= now() - INTERVAL 7 DAY
ORDER BY killed_at DESC
LIMIT 100;
```

For the offline jsonl sink (gitignored fallback), use:

```bash
jq . research/alphas/_kill_ledger.jsonl | head -50
```

For the committed archive snapshot (Slice D Task 16 backfill of the
2026-04-17 sweep — 25 ledger rows + 21 summary rows), see
`research/archive/_kill_summary_2026-04-17.jsonl`.

## Idempotency contract

```
kill_id = sha256(alpha_id || ":" || gate || ":" || stable_artifact_hash)
```

Re-running the same kill produces no new ledger rows in either CH or
the jsonl. The jsonl path uses an in-memory cache plus on-disk warming
so a process restart still dedupes.

## Cluster sidecar artifact

`research/alphas/_cluster_assignments.json` (gitignored) accumulates
cluster runs keyed by

```
f"{threshold}:{metric}:{sha256(base_dir)}:{corpus_hash}"
```

Multiple thresholds can coexist in the same file; the sidecar is
merged on each `--write-artifact` invocation, never truncated.

## DSL safety

The DSL compiler (`src/hft_platform/alpha/dsl/compiler.py`) is a
tree-walk interpreter with **explicit bans** on `eval`, `exec`,
`compile`, `getattr`, and `__import__` — verified by an attestation
test (`tests/unit/alpha/dsl/test_compile_*` suite). Any new operator
goes through the parser → AST → tree-walk path; no string-eval
shortcut exists. The attestation grep is part of `make ci`, so any
future change reintroducing a banned token fails CI.

## Common gotchas

- **`research/alphas/_kill_ledger.jsonl` is gitignored** — it's the
  offline fallback, not the source of truth. CH
  `audit.alpha_kill_ledger` is canonical when CH is up.
- **`hft alpha screen` returns `verdict='unknown'` when signals are
  missing** — the screener fails closed; it never auto-kills on data
  errors. The ledger row is only written when `--write-kill` is set
  AND verdict is `'kill'` (turnover or cost-floor breach).
- **`PYTHONPATH` may be needed for one-shot scripts** —
  `scripts/migrate_alpha_manifests.py` imports
  `research.registry.schemas`. From the repo root,
  `uv run python scripts/...` works; from elsewhere, set
  `PYTHONPATH=$(pwd)`.
- **Cluster needs at least 2 alphas with non-degenerate signals** —
  `EmptyCorpusError` is raised when no signals are loadable under
  `base_dir/runs/`. Generate the synthetic corpus first if you're
  smoke-testing.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `EmptyCorpusError` from `cluster_alphas` | No signals committed under `base_dir/runs/` | Run `scripts/generate_slice_d_signal_corpus.py` or commit real signals |
| `verdict='unknown'` for every alpha | Signal files missing at `research/experiments/<alpha_id>/signal.npy` | This is the screener's expected fail-closed mode — not a bug |
| Kill ledger row inserted twice with different `kill_id` | `stable_artifact_hash` changed (manifest mutated mid-run) | Check `kill_reason` / `cluster_id` are NOT in the manifest; freeze manifest before running migration |
| `make ci` coverage drops below 87 % | New code without tests | Add unit tests; `alpha/` is exempt from float-precision rules but NOT from coverage rules |
| CH writes silently failing under load | `audit.log_kill()` is best-effort by design | Confirm via `alpha_kill_results_total{result="fail"}` Prometheus counter; jsonl sink continues to capture rows |
| `hft alpha kill` exits 1 on whitespace-only `--reason` | Argparse passes the value through; the command rejects it | Provide a non-empty reason string |

## Path (a) vs path (b) for clustering inputs

Slice D ships clustering in **path (b)** mode: signals stored under
`research/experiments/<alpha_id>/runs/<run_id>/signal.{parquet,npy}`
are read directly. ClickHouse-side correlation (path (a)) is not in
scope for the MVP; reactivate when the signal-recorder topic is
available in production.

## Related runbooks

- `docs/runbooks/replay-parity-gate.md` — Slice C parity gate; the
  alpha factory's screener / cluster outputs feed promotion, which
  then runs through the parity gate when configured.
- `docs/architecture/current-architecture.md` §7C — Slice D surface
  table (canonical listing of every Slice D module + persistence
  surface).
- `docs/operations/env-vars-reference.md` — `HFT_KILL_LEDGER_ENABLED`
  and `HFT_ALPHA_KILL_LEDGER_PATH` references.
