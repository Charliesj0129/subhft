---
name: hft-alpha-research
description: "Run the governed alpha research factory: scaffold, datasets with metadata sidecars, Gates A-C, optimization, paper-trade records. Use when creating or evolving an alpha under research/alphas or preparing data. Not for gate interpretation (validation-gate) or live strategy code (hft-strategy)."
---

# Alpha research

Handbook (lifecycle, verdict vocabulary, gates, evidence root, factory):
`docs/runbooks/alpha-development-workflow.md`; project rules for this tree:
`research/AGENTS.md`. Research artifacts never enable live trading.

## Pipeline

```
generator (paper | mining | candidate_loop | manual) -> data -> Gates A-C -> Gates D-E -> canary -> shadow -> live (frozen)
```

Single entrance: `make research ALPHA=<id> OWNER=<you> DATA='<path.npy>'`, a
governed strict run on `vm_ul6_strict_v2` that needs `HFT_RESEARCH_EVIDENCE_ROOT`
and a clean tree. There is no Rust stage while Rust is paused. Preflight: the read-only
factory audit; finalization: the factory index.

## Commands

```bash
uv run python -m research scaffold <alpha_id>          # canonical layout under research/alphas/<id>/
make research-paper-prototype PAPER_REF=<ref> ARGS='--alpha-id <id> --complexity O1'
uv run hft alpha validate --alpha-id <id> --profile vm_ul6_strict_v2 --data <path>  # strict Gates A-C
make research-stamp-data-meta DATA_PATH=<file.npy>     # sidecar
make research-validate-data-meta DATA_PATH=<file.npy>
make research-gen-synth-lob OUT=research/data/processed/<id>/<file>.npy ARGS='--version v2 --rng-seed 42'
make research-record-paper ALPHA=<id> ARGS='--trading-day YYYY-MM-DD ...'
make research-check-paper-governance ALPHA=<id> ARGS='--strict --out outputs/paper_governance_<id>.json'
```

Run from the repository root (not from `research/`). Debug triage output is not
promotable (`HFT_RESEARCH_ALLOW_TRIAGE=1`, `make research-triage`).

## Rules

1. Datasets live under `research/data/{raw,interim,processed}`; every `.npy`/`.npz`
   has a `.meta.json` sidecar; preserve `local_ts`; use versioned latency profiles.
2. `paper_refs` in the manifest map to `paper_index.json`; Gate A strict mode
   enforces it, plus sidecar validity, allowed roots, and a `complexity` field.
3. Governed runs write their evidence to `HFT_RESEARCH_EVIDENCE_ROOT` (outside this
   repository, sealed, ledgered); exploratory output goes to `outputs/`. Never write into
   `research/alphas/<id>/` (source and manifest only); `research/experiments/**` is frozen
   legacy evidence. Binary artifacts stay out of source dirs.
4. Verdicts are the handbook's vocabulary (PASS / KILL / NEEDS-MORE-DAYS / INCONCLUSIVE /
   INVALID, handbook section 2), written only by `derive_verdict`. Generators shortlist,
   never KILL. Never relax a pre-registered floor or gate.
5. Float is allowed in research for offline metrics; nothing here reaches live accounting.
   Manifest `skills_used` is validated against `VALID_SKILLS` in `contracts/alpha.py`
   (Do-NOT-Edit), which still lists the retired skill names; use those tokens, not directory names.
6. Check the local data corpus before choosing dates:
   `docs/operations/local-clickhouse-market-data-corpus.md`; depth, scale, and
   duplicate-delivery traps: `.agent/rules/70-research-data.md`.

## Common failures

| Symptom | Action |
|---|---|
| Gate A rejects provenance | regenerate or validate the sidecar |
| Gate B cannot find tests | check the alpha's test path; run from the repo root |
| Gate C Sharpe collapses to zero | inspect latency application and `local_ts` cadence (`hft-backtest`) |
| Gate D blocks on feature-set version | align manifest with the live feature registry version |

## Done when

The alpha has a valid manifest and sidecars, Gates A-C ran with a declared
latency profile and cost profile, outputs sit under `research/experiments/`, and
the verdict and its evidence are in the evidence root as they came out.

## References

- `references/factory.md` — read for the stage table, gate mapping, artifact layout, paper-trade governance, and batch commands.
- `references/data-governance.md` — read when preparing or validating datasets, sidecars, or synthetic data.
