# Research Data

Local L2/tick source is ClickHouse `hft.market_data` on 8123/9000. Auth comes from `.env`; never expose password. Main fields: `exch_ts`, `ingest_ts` ns, `type`, `price_scaled`, 5-level bid/ask arrays. Raw ClickHouse price scale is x1,000,000; live platform scale is x10,000, so conversion must be explicit.

Best known complete research interval: 2026-03-02 to 2026-03-24. Avoid known sparse/unusable dates unless intentionally testing gaps.

Local corpus now spans Asia/Taipei wall dates 2026-01-26 to 2026-07-30
(903,498,775 rows) after the 2026-07-25 pull and the 2026-07-31
closed-partition repair from the production host; its TTL was removed locally.
Verified closed UTC ingest partitions extend through 20260729. The 2026-07-30
trading session is incomplete, and UTC partitions 20260730/20260731 remain
deferred because they overlap the active local WAL. Coverage holes, degraded
days, and the 368 -> 296 symbol-universe change are inventoried in
`docs/operations/local-clickhouse-market-data-corpus.md` — read it before
choosing a date range.

Canonical governed L2+tick export is `research.data_pipeline` via `make research-export-l2-ticks`. Sidecar/data-root rules live in `.agent/skills/research-data-governance/SKILL.md`. `research/tools/ch_batch_export.py` is legacy/L1 wrapper and must not reimplement sidecar/dtype governance.

Every export needs metadata sidecar with dataset ID, source, rows, symbols, date, fingerprint, and data UL/provenance. L2 exports dedup identical BidAsk within 0.5 ms where applicable.

Large queries must set memory limits and preserve deterministic ordering by exchange timestamp/sequence.
