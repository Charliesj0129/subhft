---
name: validation-gate
description: "Interpret alpha Gates A-F, diagnose promotion blockers, and judge whether evidence suffices, given the loop_v1 L11 registry freeze. Use when reading validation or promotion output or deciding if an alpha may advance. Not for authoring alphas or backtest setup."
---

# Alpha validation gates

Exact thresholds are the source of truth in `src/hft_platform/alpha/validation.py`,
`src/hft_platform/alpha/promotion.py`, and the active profile
(`config/research/profiles/vm_ul6_strict.yaml`, a frozen Do-NOT-Edit file).
Lifecycle and gate contract: `docs/runbooks/alpha-development-workflow.md`.

## Freeze

The live registry is locked to `r47_tmf_v1` (loop_v1 L11, `docs/loop_v1_stabilization_charter.md`,
`.github/workflows/freeze-guard.yml`). A new alpha can complete Gate F, canary,
and shadow evaluation but cannot replace the active loop. Never edit
`config/loops/<id>.yaml` or the live registry to "advance" an alpha.

## Gate map

| Gate | Question | Evidence |
|---|---|---|
| A | Is the manifest, dataset, and governance valid? | manifest, sidecar metadata, allowed roots, complexity |
| B | Is it correct and testable? | alpha tests discovered from the repo root, coverage |
| C | Does it hold under realistic assumptions? | scorecard, latency profile, stress, walk-forward; separate warn-only from true blockers |
| D | Is it eligible for promotion? | configured thresholds plus feature-set parity with live; strict profile has blocking sub-gates and needs a replay-parity report |
| E | Is paper-trade execution quality adequate? | recorded shadow sessions, reject rates, governance report |
| F | Rust readiness (optional, taker-heavy alphas) | manifest, parity tests, optional benchmark gate |
| Canary | Controlled exposure | canary config and `hft alpha canary evaluate` |

## Run and read

```bash
uv run hft alpha validate <alpha_id>                 # Gates A-C (add --profile vm_ul6_strict for promotion eligibility)
uv run hft alpha cheap-screen ...                    # pre-Gate-A IC / turnover / cost triage
uv run hft alpha promote --alpha-id <id> --owner <you>   # Gates D-F, writes the canary config
uv run hft alpha canary status
uv run hft alpha canary evaluate ...
```

Artifacts: `research/experiments/validations/<id>/<stamp>/`,
`research/experiments/promotions/<id>/<stamp>/` (including
`paper_governance_report.json`).

## Common blockers

| Symptom | Likely cause | Action |
|---|---|---|
| Gate A rejects metadata | missing or stale sidecar | restamp and validate |
| Gate B fails | launched from `research/`, not the repo root | rerun from the root |
| Gate C Sharpe collapses to zero | latency wrapper keeps deferring fills | inspect position-latency logic (`hft-backtest`) |
| Gate C inflated | missing `local_ts` or wrong step cadence | rebuild data with timestamps |
| Gate D: feature-set version | manifest and live registry diverged | align the version |
| Gate D: no latency profile | scorecard lacks one | declare a measured profile |
| Gate D: synthetic equity | `require_real_equity` is true | use real data |
| Gate E | too few sessions or incomplete reject evidence | record more sessions |

## Judgment

Treat a pass as evidence, not as a verdict on the idea: statistical gates use
Benjamini-Hochberg or effective test counts rather than p-values alone, and a
gate threshold is never relaxed after seeing the result. KILL / NEEDS-MORE-DAYS /
INCONCLUSIVE are reported as they come out. Canary is config-driven and
reversible at every stage (states `hold`, `escalate`, `rollback`, `graduate`).

## Done when

Each gate is reported PASS, FAIL (with the failing check and output), or NOT RUN,
and the next action respects the freeze.
