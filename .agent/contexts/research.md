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

## Tools to favor
- `rg` / `rg --files` for local retrieval
- `research/alphas/*`, `research/data/*`, `config/research/*`
- HFT skills: `hft-alpha-research`, `research-factory`, `research-data-governance`, `validation-gate`, `hft-backtester`

## Output
Findings first, recommendations second
