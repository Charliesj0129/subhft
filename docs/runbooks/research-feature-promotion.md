# Research Feature Promotion Runbook (Prototype)

## Workflow
1. Run validation and profile checks:
   - `uv run python research/tools/feature_promotion_check.py --alpha-id <id> --data <npz>`
2. Optional promotion artifact:
   - add `--promote --owner <name>`
3. Render markdown summary:
   - `uv run python research/tools/render_promotion_report.py <json> --out report.md`
4. Benchmark matrix:
   - `uv run python research/tools/feature_benchmark_matrix.py`

## Notes
- This is a process wrapper around existing alpha validation/promotion and perf gate tools.
- Cold/warm benchmark interpretation must be documented in experiment notes.
