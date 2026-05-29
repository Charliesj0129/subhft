# Research Context

Mode: Exploration, investigation, learning
Focus: Alpha research, architecture questions, evidence-first investigation

## Behavior
- Read the relevant docs, `.agent/skills`, manifests, datasets, and tests before concluding.
- Treat claims about alpha performance, latency, fills, and promotion readiness as evidence requirements.
- Record exact files, commands, datasets, and gate outputs used for conclusions.
- Do not promote or recommend live use without latency, replay, and governance evidence.

## Research Process
1. Understand the question
2. Retrieve relevant code/docs/research artifacts
3. Form a falsifiable hypothesis
4. Verify with tests, scorecards, metadata, or runtime evidence
5. Summarize findings

## Canonical docs (read before acting)
- Factory operations (entrance, 8 stages, layout, data governance, paper-trade, triage): `research/README.md`
- Lifecycle + Gates A–F + profile semantics (`vm_ul6` vs `vm_ul6_strict`): `docs/runbooks/alpha-development-workflow.md`
- Replay-parity gate (fail-closed matrix, canonical hash): `docs/runbooks/replay-parity-gate.md`
- Data source + L1/L2 formats: `.agent/rules/70-research-data.md`; export contract: `docs/runbooks/research-data-pipeline.md`
- Constrained hypothesis ideation: `.agent/teams/alpha-research/factor-ideation-pipeline.md`

## Reuse-first inventory (do NOT reinvent)
- Canonical orchestrator: `hft alpha pipeline {run,triage}` (Stage 5, 2026-05-28). `make research` is a thin shim over it.
- Other entrances: `hft alpha {scaffold,validate,screen,cheap-screen,promote,cluster,kill,pool,canary}`.
- Backtest engine config: `research/backtest/contract.py::BacktestContractSpec` (Stage 4) — one declarative spec drives `MakerEngine`, `HftNativeRunner`, `HftBacktestAdapter`. Do not pass loose cost/fill/latency kwargs.
- Result persistence: `research/registry/result_store.py` (`ResultStore`) — never hand-roll run output.
- Gate logic: add new sub-gates to `src/hft_platform/alpha/_sub_gates/` (registry-driven), not ad-hoc scripts.
- Kill / dedup: `src/hft_platform/alpha/kill_ledger.py`; correlation cull: `src/hft_platform/alpha/cluster.py`.
- Data governance: `research/tools/data_governance.py`; scaffolding: `research/tools/alpha_scaffold.py`; backtest engines: `research/backtest/`.
- Lifecycle source of truth: `manifest.yaml::status`. Drift gate: `make research-audit-lifecycle` (Stage 6). Six derived stores listed in `docs/runbooks/alpha-lifecycle-state.md`.
- Validation profile: single token `vm_ul6_strict` across all entrypoints; `vm_ul6` is a legacy alias with `DeprecationWarning` for one release.
- No ad-hoc scripts at `research/` root; `research/tools/legacy/` is non-official.

## Tools to favor
- `rg` / `rg --files` for local retrieval
- `research/alphas/*`, `research/data/*`, `config/research/*`
- HFT skills: `hft-alpha-research`, `research-factory`, `research-data-governance`, `validation-gate`, `hft-backtest-engine`

## Output
Findings first, recommendations second
