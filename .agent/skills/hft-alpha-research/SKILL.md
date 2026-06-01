---
name: hft-alpha-research
description: Use when creating or evolving research alphas, scaffolding governed research artifacts, preparing datasets for Gate A-C, or working inside the paper-to-promotion research factory workflow.
---

# HFT Alpha Research

Use this skill to author and maintain research artifacts. Use `research/SOP.md` as the top-level lane and keep this skill focused on alpha-specific source layout, dataset preparation, and factory entrypoints.

## Start Here

Create or update an alpha in this order:

1. Scaffold or inspect `research/alphas/<alpha_id>/`.
2. Confirm the manifest fields and test layout exist.
3. Prepare or validate governed datasets under `research/data/`.
4. Run Gate A-C through the factory or CLI.
5. Send promotion and paper-trade work to `validation-gate` and `paper-trader`.

## Canonical Paths

| Purpose | Path |
| --- | --- |
| Alpha source | `research/alphas/<alpha_id>/` |
| Registry and schemas | `research/registry/` |
| Research backtest runtime | `research/backtest/` |
| Validation and promotion runtime | `src/hft_platform/alpha/` |
| Latency profiles | `config/research/latency_profiles.yaml` |
| Synthetic data tools | `research/tools/` |

## Scaffold and Validate

Scaffold a new alpha:

```bash
uv run python -m research scaffold <alpha_id>
```

Run the governed validation lane:

```bash
uv run hft alpha validate <alpha_id>
```

Run the factory lane directly when you need explicit data and latency inputs:

```bash
uv run python -m research.factory run-gate-c \
  <alpha_id> \
  --data research/data/processed/<alpha_id>/<file>.npy \
  --latency-profile <profile>
```

Treat `run_gate_b()` as a project-root operation. Pass the repository root, not the `research/` directory.

## Dataset Rules

Apply these before treating any scorecard as promotion-ready:

- Keep datasets inside the governed research roots.
- Stamp metadata with `python -m research stamp-data-meta <dataset.npy>`.
- Validate metadata with `python -m research validate-data-meta <dataset.npy>`.
- Preserve `local_ts` for latency-aware backtests.
- Use versioned latency profiles from `config/research/latency_profiles.yaml`.

Use the synthetic lane when you need reproducible TWSE-style data:

```bash
make research-gen-synth-lob OUT=research/data/processed/<alpha_id>/<file>.npy ARGS='--version v2 --rng-seed 42'
```

## Boundaries

Keep these responsibilities separated:

- Use `hft-backtest-engine` for latency realism and adapter behavior.
- Use `validation-gate` for pass/fail interpretation and promotion blockers.
- Use `hft-strategy-dev` only after the logic moves toward live strategy integration.
- Use `hft-architect` when the alpha requires new runtime modules, feature contracts, or Rust migration.

## Common Failure Modes

| Symptom | Action |
| --- | --- |
| Gate A rejects dataset provenance | regenerate or validate sidecar metadata |
| Gate B cannot find tests | inspect alpha-specific test path and run from repo root |
| Gate C Sharpe collapses to zero | inspect latency application and `local_ts` cadence with `hft-backtest-engine` |
| Gate D blocks on feature set version | align the manifest with the live feature registry version |

## Paper-to-Prototype Bridge

Scaffold an alpha directly from a paper reference:

```bash
make research-paper-prototype PAPER_REF=<ref> ARGS='--alpha-id <id> --complexity O1'
```

This scaffolds `research/alphas/<alpha_id>/` and writes a reverse mapping into `paper_index.json` so Gate A can trace the alpha back to its source paper.

## Gate A Strict Mode

Strict Gate A (enabled by UL6 validation profile) enforces:

- `manifest.paper_refs` must exist and map to entries in `paper_index.json`
- Dataset metadata sidecars validated (all required keys + row count consistency)
- Dataset paths must be under allowed roots (`research/data/`)
- `complexity` field required in manifest

## Alpha Package Structure

```
research/alphas/<alpha_id>/
├── __init__.py
├── signal.py          # Alpha signal implementation
├── manifest.yaml      # Alpha metadata (paper_refs, complexity, features)
├── README.md          # Hypothesis, formula, validation status
├── CHANGELOG.md       # Version history (optional)
└── tests/
    └── test_signal.py # Alpha-specific tests
```

## Cross-References

| Related Skill | When to Use |
| --- | --- |
| research-factory | Full end-to-end pipeline orchestration |
| research-data-governance | Dataset preparation and metadata sidecar management |
| validation-gate | Gate A-E pass/fail interpretation and promotion blockers |
| hft-backtest-engine | Backtest adapter configuration and latency realism |
