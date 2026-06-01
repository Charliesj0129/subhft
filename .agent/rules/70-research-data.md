# Research Data

Local L2/tick source is ClickHouse `hft.market_data` on 8123/9000. Auth comes from `.env`; never expose password. Main fields: `exch_ts`, `ingest_ts` ns, `type`, `price_scaled`, 5-level bid/ask arrays. Raw ClickHouse price scale is x1,000,000; live platform scale is x10,000, so conversion must be explicit.

Best known complete research interval: 2026-03-02 to 2026-03-24. Avoid known sparse/unusable dates unless intentionally testing gaps.

Canonical governed L2+tick export is `research.data_pipeline` via `make research-export-l2-ticks`. Sidecar/data-root rules live in `.agent/skills/research-data-governance/SKILL.md`. `research/tools/ch_batch_export.py` is legacy/L1 wrapper and must not reimplement sidecar/dtype governance.

Every export needs metadata sidecar with dataset ID, source, rows, symbols, date, fingerprint, and data UL/provenance. L2 exports dedup identical BidAsk within 0.5 ms where applicable.

Large queries must set memory limits and preserve deterministic ordering by exchange timestamp/sequence.
