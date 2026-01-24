# Outputs and Artifacts

Use consistent locations to keep experiments and deployments reproducible.

## Directories
- `outputs/`: transient run outputs (plots, snapshots, adhoc exports).
- `artifacts/`: durable baselines (benchmarks, golden datasets, calibration outputs).
- `reports/`: human-readable reports (HTML/PDF/Markdown).

## Rules
- Do not commit secrets in outputs or artifacts.
- Store a config snapshot or metadata next to each artifact (e.g., `metadata.json` with git commit and params).
- Use date-stamped subfolders: `YYYYMMDD/<task>/...`.

## Cleanup
- Keep only the latest 3 runs in `outputs/`.
- Keep only approved baselines in `artifacts/`.
- Use `make clean` for build artifacts.
