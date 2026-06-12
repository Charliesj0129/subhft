"""Alpha Candidate Loop v1 (docs/research/alpha_candidate_loop_v1_spec.md).

Batch candidate pipeline: JSONL candidates -> validator -> compiler ->
per-split evaluator -> hard gates -> ClickHouse records + failure_summary.
Offline research code only; nothing in src/ imports this package.
"""
