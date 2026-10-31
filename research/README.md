# research/

Governed alpha research: generators, the Gate C backtest runtime, the governed
pipeline and its tools. Nothing here enables live trading.

The handbook is
[`docs/runbooks/alpha-development-workflow.md`](../docs/runbooks/alpha-development-workflow.md):
lifecycle, verdict vocabulary, Gates A-F and their thresholds, the evidence root,
factory audit and index, layout and artifact rules. Rules for agents working in this
tree: [`AGENTS.md`](./AGENTS.md).

## Quick start

```bash
make research ALPHA=<alpha_id> OWNER=<owner> DATA='<path.npy>'   # governed strict run (vm_ul6_strict_v2)
python -m research status [--alpha ID]                          # status derived from the ledger
uv run python -m research.factory audit                         # read-only layout and data audit
make research-check                                             # research tests and lint
```

A governed run needs `HFT_RESEARCH_EVIDENCE_ROOT` (absolute, outside this
repository) and a clean tree; see handbook section 5.

## Other references

| Topic | Source |
|---|---|
| Decision record | `docs/adr/005_research_v2.md` |
| Research data, scales, book depth (0-5) | `.agent/rules/70-research-data.md` |
| Backtest engines and their bias | `docs/runbooks/backtest-engine-selection.md` |
| Replay-parity gate | `docs/runbooks/replay-parity-gate.md` |
