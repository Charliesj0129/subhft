---
name: research-data-governance
description: Use when preparing, validating, or generating research datasets. Covers metadata sidecar requirements, synthetic data generation, UL6 provenance rules, and data path governance.
---

# Research Data Governance

Governs dataset preparation, metadata provenance, and data path discipline for the alpha research pipeline. Every dataset used in Gate A onward must meet these requirements.

## Data Directory Layout

```
research/data/
├── raw/           # Unprocessed market data (tick, LOB snapshots)
├── interim/       # Intermediate transformations (cleaning, resampling)
├── processed/     # Ready for backtest (final .npy/.npz with sidecars)
└── models/        # Trained model binaries (.onnx, .zip)
```

### Path Rules

| Directory | Contents | Mutability |
| --- | --- | --- |
| `raw/` | Unprocessed market data from exchanges or broker feeds | Append-only, never modified after ingestion |
| `interim/` | Intermediate transformations (cleaning, resampling, merging) | Recreatable from raw + transform scripts |
| `processed/` | Final datasets ready for backtest consumption | Immutable once stamped with metadata |
| `models/` | Trained model binaries (.onnx, .zip, .pkl) | Versioned, never overwritten in place |

## Metadata Sidecar Contract

Every `.npy` or `.npz` file MUST have a corresponding `.meta.json` sidecar in the same directory.

### Required Keys (all profiles)

| Key | Type | Description |
| --- | --- | --- |
| `source` | string | Origin: `"real"`, `"synthetic"`, `"replay"` |
| `generator` | string | Script or tool that produced the data |
| `symbols` | list[str] | Symbol codes included (e.g., `["2330", "TXF"]`) |
| `split` | string | Data split: `"train"`, `"val"`, `"test"`, `"full"` |
| `row_count` | int | Number of rows/events in the dataset |
| `created_at` | string | ISO 8601 timestamp of creation |

### Additional Keys (UL6 strict profile)

| Key | Type | Description |
| --- | --- | --- |
| `rng_seed` | int | Random seed used for synthetic generation |
| `version` | string | Generator version (e.g., `"v2"`) |
| `owner` | string | Person or system that created the dataset |

### Example Sidecar

```json
{
  "source": "synthetic",
  "generator": "research.tools.synth_lob_v2",
  "symbols": ["TXF", "MXF"],
  "split": "train",
  "row_count": 500000,
  "created_at": "2026-03-22T10:00:00+08:00",
  "rng_seed": 42,
  "version": "v2",
  "owner": "charlie"
}
```

## Commands

### Stamp Metadata

Attach or update a metadata sidecar for an existing dataset:

```bash
make research-stamp-data-meta DATA_PATH=<path.npy> \
  ARGS='--source-type real --owner charlie --symbols 2330'
```

### Validate Metadata

Check that a dataset has a valid sidecar and consistent row counts:

```bash
make research-validate-data-meta DATA_PATH=<path.npy>
```

### Generate Synthetic LOB Data

Generate OU-Hawkes-Markov v2 synthetic LOB data:

```bash
make research-gen-synth-lob \
  OUT=research/data/processed/<id>/synthetic_lob_v2_train.npy \
  ARGS='--version v2 --rng-seed 42 --symbols TXF,MXF --split train'
```

This automatically creates the `.meta.json` sidecar alongside the output file.

## Gate A Strict Enforcement

Gate A validates datasets before any backtest runs. Under strict mode (UL6), these checks apply:

| Check | Standard | UL6 Strict |
| --- | --- | --- |
| Dataset path under allowed roots | `research/data/` | `research/data/` |
| Metadata sidecar exists | required | required |
| Required contract keys present | 6 base keys | 6 base + 3 UL6 keys |
| Row count consistency | sidecar vs actual | sidecar vs actual |
| Provenance fields | optional | required (`source`, `generator`, `rng_seed`) |
| Paper linkage | optional | `paper_refs` must map to `paper_index.json` |

### Allowed Data Roots

Datasets referenced in manifests or factory commands must reside under:

- `research/data/raw/`
- `research/data/interim/`
- `research/data/processed/`

Paths outside these roots are rejected by Gate A.

## Synthetic Data Generation

### OU-Hawkes-Markov v2 Generator

The default synthetic data generator produces TWSE-style LOB data with:

- Ornstein-Uhlenbeck mean-reverting mid-price process
- Hawkes self-exciting order flow
- Markov regime switching (trending, mean-reverting, volatile, low-liquidity)

### Reproducibility

- Always specify `--rng-seed` for deterministic output
- Always specify `--version` to pin generator behavior
- Sidecar is auto-generated with full provenance

### Splits

Generate separate files for train/val/test with different seeds:

```bash
make research-gen-synth-lob OUT=research/data/processed/<id>/synth_train.npy \
  ARGS='--version v2 --rng-seed 42 --split train'
make research-gen-synth-lob OUT=research/data/processed/<id>/synth_val.npy \
  ARGS='--version v2 --rng-seed 43 --split val'
make research-gen-synth-lob OUT=research/data/processed/<id>/synth_test.npy \
  ARGS='--version v2 --rng-seed 44 --split test'
```

## Cross-References

| Skill | When to Use |
| --- | --- |
| research-factory | Full pipeline orchestration |
| hft-alpha-research | Alpha scaffold and manifest preparation |
| hft-backtest-engine | Backtest adapter and latency profile configuration |
| validation-gate | Gate A-E pass/fail interpretation |
