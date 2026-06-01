# Alpha Lifecycle State

> Stage 6 of the research-workflow deep consolidation (2026-05-28).
> Declares the single source of truth for "is alpha X alive, killed, or
> archived", and names the six derived stores that must agree.

## Source of truth

`research/alphas/<alpha_id>/manifest.yaml::status` — every other store is
either a derived cache, an audit-trail append-only log, or a snapshot.

```
              ┌─────────────────────────────────────────┐
              │  manifest.yaml::status  (source of truth)│
              └────────────────┬────────────────────────┘
                               │  refresh / drift checks
        ┌──────────────────────┼──────────────────────┐
        ▼            ▼         ▼           ▼          ▼
  filesystem    kill_ledger  cluster   paper_index  AlphaRegistry
   placement    (jsonl + CH) assignments reverse     (in-memory,
   under         (audit-trail) snapshot  links       rebuilt at
   alphas/ vs    — append-only)                       process start)
   archive/
```

| Store | Path / location | Role | Refresh mechanism |
|---|---|---|---|
| Filesystem placement | `research/alphas/<id>/` vs `research/archive/alphas_<date>/<id>/` | Authoritative "is this alive" answer (paired with manifest status). | Operator `git mv` after a kill; lifecycle audit flags drift. |
| Manifest `status:` field | `<id>/manifest.yaml` | Authoritative lifecycle state. | Operator edit when status changes; advisory log line on kill. |
| Kill ledger (jsonl) | `research/alphas/_kill_ledger.jsonl` | Offline append-only audit trail of every kill. | `hft_platform.alpha.kill_ledger.append_kill()`. |
| Kill ledger (ClickHouse) | `audit.alpha_kill_ledger` | Durable queryable mirror of the jsonl. | Same `append_kill()` writes both. |
| Cluster assignments | `research/alphas/_cluster_assignments.json` | Snapshot of last clustering run. | `hft alpha cluster --write-artifact`. |
| Paper-index reverse links | `research/knowledge/paper_index.json::alphas[]` | Editorial cross-reference from each paper to alphas that cite it. | Operator edit; lifecycle audit warns on orphans. |
| `AlphaRegistry` | in-memory (`research/registry/alpha_registry.py`) | Process-local registry rebuilt from filesystem at startup. | Recreated each process; no persistent state. |

## Status values

The canonical enum lives in `research.registry.schemas.AlphaStatus`:

| Value | Meaning | Expected placement |
|---|---|---|
| `DRAFT` / `GATE_ZERO` / `PROTOTYPE` | Active research; pre-promotion. | `research/alphas/` |
| `GATE_A` … `GATE_E` | Mid-pipeline; gate-specific work in progress. | `research/alphas/` |
| `PRODUCTION` | Promoted to live registry. | `research/alphas/` (live registry under `config/loops/` is the separate operator surface). |
| `DEPRECATED` | Replaced or retired; not killed. | `research/archive/` |
| `KILLED` | Ledger-confirmed failure. | `research/archive/` |

Anything else in the wild (`PARKED`, `EXPLORATORY`, `EXPLORATORY_INCOMPLETE`,
legacy lowercase variants) appears only in archived manifests and is treated
as non-terminal by the audit.

## Drift gate

`make research-audit-lifecycle` (`research/tools/lifecycle_audit.py`) walks
the active + archive trees, indexes every `manifest.yaml`, and cross-checks
every derived store. It surfaces:

| Severity | Code | Meaning |
|---|---|---|
| ERROR | `terminal_in_active` | manifest has `status: KILLED`/`DEPRECATED` but still under `research/alphas/`. |
| ERROR | `active_and_archived` | same alpha_id exists under both trees. |
| ERROR | `killed_but_active` | alpha is in `_kill_ledger.jsonl` AND under `research/alphas/` AND status is non-terminal. |
| ERROR | `duplicate_alpha_id` | two manifests claim the same `alpha_id`. |
| ERROR | `manifest_parse_error` | YAML can't be parsed. |
| WARN  | `ledger_orphan` | kill-ledger row references an alpha_id with no manifest anywhere. |
| WARN  | `cluster_orphan_refs` | cluster snapshot references unknown alphas (snapshot is stale). |
| WARN  | `paper_index_orphan_refs` | paper-index reverse link points at a missing alpha. |

Exit codes: `0` = no errors (warnings OK), `1` = at least one error, `2` = fatal IO error.

## Post-kill workflow

When `kill_ledger.append_kill()` succeeds it emits a structured log line
(`alpha_kill_recorded`) with the **operator commands to run**:

```
git mv research/alphas/<id> research/archive/alphas_<YYYY-MM-DD>/<id>
# edit research/archive/alphas_<YYYY-MM-DD>/<id>/manifest.yaml → status: KILLED
make research-audit-lifecycle    # must return 0 errors
```

Filesystem moves are deliberately **not** automated: kills happen from CI,
from interactive `hft alpha kill`, and from batch promotions, and a write
side-effect in a hot codepath would block on `git` credentials. The audit
catches operators who skip the move.

## CI integration

`make research-audit-lifecycle` should run after any commit that touches:

* `research/alphas/`,
* `research/archive/`,
* `research/knowledge/paper_index.json`,
* `research/alphas/_cluster_assignments.json`,
* `research/alphas/_kill_ledger.jsonl`.

It is a separate target from `make research-audit-strict` (data-governance
audit) so the two surfaces can fail independently.

## Cross-references

* Source of truth: `research/registry/schemas.py::AlphaStatus`,
  `research/registry/schemas.py::AlphaManifest`.
* Audit tool: `research/tools/lifecycle_audit.py`.
* Append-only ledger: `src/hft_platform/alpha/kill_ledger.py`.
* Plan reference: Stage 6 of `~/.claude/plans/swift-meandering-avalanche.md`.
* Memory entry (to be added in Stage 9): `research_workflow_consolidation_2026_05_28.md`.
