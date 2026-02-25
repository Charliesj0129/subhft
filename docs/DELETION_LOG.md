# Deletion Log

This file tracks significant code deletions and cleanups across the project to maintain context about why certain components or scripts were removed.

## [Date: 2026-02-24] Dead Code and Redundant Scripts Cleanup

**Reason:** Scheduled project cleanup to identify, remove dead code, and consolidate overlapping scripts for better maintainability.
**Tools Used:** `vulture`
**Files Affected:**

- `src/hft_platform/feed_adapter/lob_engine.py`: Removed unused `exc_type`, `tb` arguments.
- `src/hft_platform/recorder/wal.py`: Removed unused `approx_bytes` argument.
- `src/hft_platform/recorder/worker.py`: Removed unused `clickhouse_client` argument.
- `src/hft_platform/risk/fast_gate.py`: Removed unused `exc_type`, `exc_val`, `exc_tb` arguments.
- `research/extract_pdf_temp.py`: Deleted (redundant temporary script; core logic exists in `research/tools/extract_pdf.py`).
- `research/verify_paper_hypotheses.py`: Deleted (superseded by `research/verify_paper_hypotheses_v2.py`).

**Validation:**

- All tests passing with 70.96% coverage (`make test`).
- Ran `ruff check` and `ruff format` to ensure style consistency.
