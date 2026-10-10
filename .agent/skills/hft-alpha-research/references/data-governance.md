# Research data governance

## Layout and mutability

| Directory | Contents | Mutability |
|---|---|---|
| `raw/` under `research/data/` | unprocessed market data | append-only |
| `interim/` under `research/data/` | cleaning, resampling | recreatable from raw + scripts |
| `processed/` under `research/data/` | final `.npy`/`.npz` with sidecars | immutable once stamped |
| `models/` under `research/data/` | trained binaries (`.onnx`, `.zip`, `.pkl`) | versioned, never overwritten |

Datasets referenced in manifests or factory commands must sit under `raw/`,
`interim/`, or `processed/`; Gate A rejects other paths.

## Metadata sidecar (`<file>.meta.json`, same directory)

Required for every `.npy`/`.npz`: `source` (`real` / `synthetic` / `replay`),
`generator`, `symbols`, `split` (`train` / `val` / `test` / `full`), `row_count`
(must match the data), `created_at` (ISO 8601). Strict profile (UL6) adds
`rng_seed`, `version`, `owner`, and requires provenance (`source`, `generator`,
`rng_seed`) plus `paper_refs` mapped in `paper_index.json`.

```bash
make research-stamp-data-meta DATA_PATH=<path.npy> ARGS='--source-type real --owner <you> --symbols 2330'
make research-validate-data-meta DATA_PATH=<path.npy>
```

## Synthetic data

OU-Hawkes-Markov v2 produces TWSE-style LOB data (mean-reverting mid, Hawkes
order flow, regime switching). Always pass `--rng-seed` and `--version`; the
sidecar is written automatically. Separate train/val/test files use different seeds.

```bash
make research-gen-synth-lob OUT=research/data/processed/<id>/synth_train.npy \
  ARGS='--version v2 --rng-seed 42 --symbols TXF,MXF --split train'
```

Synthetic data supports pipeline development only; it does not support an edge claim.

## Real data traps

Raw ClickHouse scale is x1,000,000; depth is 0-5 levels; some days have
duplicate delivery; some date ranges are partial or missing. All of it:
`.agent/rules/70-research-data.md` and
`docs/operations/local-clickhouse-market-data-corpus.md`.
