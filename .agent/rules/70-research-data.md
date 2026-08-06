# Research Data

Local L2/tick source is ClickHouse `hft.market_data` on 8123/9000. Auth comes from `.env`; never expose password. Main fields: `exch_ts`, `ingest_ts` ns, `type`, `price_scaled`, 5-level bid/ask arrays. Raw ClickHouse price scale is x1,000,000; live platform scale is x10,000, so conversion must be explicit.

Longest unbroken clean intervals, measured 2026-08-06: 2026-05-25 to 2026-06-12 (15 sessions), 2026-07-17 to 2026-08-04 (13), 2026-03-03 to 2026-03-18 (12). The previously documented "2026-03-02 to 2026-03-24" was wrong — 2026-03-02 has zero rows. Avoid known sparse/unusable dates unless intentionally testing gaps.

Local corpus now spans 2026-01-26 to 2026-08-05 (936,216,159 rows); its TTL was removed locally. Of 126 exchange sessions: 90 clean, 12 partial, 8 degraded, 16 missing. Coverage holes, degraded days, and the symbol-universe range (1–523) are inventoried in `docs/operations/local-clickhouse-market-data-corpus.md` — read it before choosing a date range, and regenerate it with the audit rather than editing by hand.

Canonical governed L2+tick export is `research.data_pipeline` via `make research-export-l2-ticks`. Sidecar/data-root rules live in `.agent/skills/research-data-governance/SKILL.md`. `research/tools/ch_batch_export.py` is legacy/L1 wrapper and must not reimplement sidecar/dtype governance.

Source-layer quality is audited by `make research-data-quality DATE_FROM=... DATE_TO=...` (`research/data_pipeline/quality.py`). Advisory, read-only; writes `research/reports/data_quality/*_source_audit.{json,md}`. Its verdict is stamped into dataset sidecars as `source_quality_*`. `ts_causality` (`exch_ts` may never lead `ingest_ts`) is the invariant that the 2026-01/02 +8h shift broke — run the audit before trusting a new or re-pulled date range. See `docs/modules/data_quality.md`.

Every export needs metadata sidecar with dataset ID, source, rows, symbols, date, fingerprint, and data UL/provenance. L2 exports dedup identical BidAsk within 0.5 ms where applicable.

Large queries must set memory limits and preserve deterministic ordering by exchange timestamp/sequence.
