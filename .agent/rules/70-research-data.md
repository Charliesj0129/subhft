# Research Data

Local L2/tick source is ClickHouse `hft.market_data` on 8123/9000. Auth comes from `.env`; never expose password. Main fields: `exch_ts`, `ingest_ts` ns, `type`, `price_scaled`, and **variable-length (0-5) bid/ask arrays** — see "Book depth" below. Raw ClickHouse price scale is x1,000,000; live platform scale is x10,000, so conversion must be explicit.

Longest unbroken clean intervals, measured 2026-08-07: 2026-05-25 to 2026-06-12 (15 sessions), 2026-07-17 to 2026-08-06 (15), 2026-03-03 to 2026-03-18 (12). The previously documented "2026-03-02 to 2026-03-24" was wrong — 2026-03-02 has zero rows. Avoid known sparse/unusable dates unless intentionally testing gaps.

Local corpus now spans 2026-01-26 to 2026-08-06 (949,820,979 rows) after the
2026-07-31 closed-partition repair and 2026-08-07 archive sync; its TTL was
removed locally. Of 127 exchange sessions: 92 clean, 12 partial, 7 degraded,
16 missing. Coverage holes, degraded days, and the symbol-universe range
(1–523) are inventoried in `docs/operations/local-clickhouse-market-data-corpus.md`
— read it before choosing a date range, and regenerate it with the audit rather
than editing by hand.

The local archive is the only durable copy and upstream retains ~2 months, so a lapse in syncing is permanent loss. Maintain it with `make research-archive-sync` (`scripts/sync_market_data_archive.py`) — read-only on production, per-partition `FORMAT Native` + `cityHash64` verification, and it refuses any partition that already has local rows because `market_data` is a plain MergeTree. The audit's `archive_sync` check measures how far behind the archive is and how long before the upstream copy expires.

## Book depth is variable length, never assume 5

`bids_price`/`asks_price`/`bids_vol`/`asks_vol` are **0-5 elements, not fixed
L5**. The normalizer's Rust kernel (`scale_book_seq_inner`,
`rust_core/src/fast_lob/scale.rs`) keeps only levels with `price > 0`, so
`length(bids_price)` is the number of levels the exchange actually priced.
There is no cap and no slice anywhere on the path: a short array means market
thinness, never truncation or a recording defect.

Measured 2026-09-13 over 2026-08-24 -> 2026-09-11 (121,469,696 BidAsk rows):

| class | BidAsk rows | full 5 levels | avg bid levels |
|---|---|---|---|
| option | 61,529,450 | **24.9%** | 2.98 |
| future | 39,992,893 | 96.3% | 4.92 |
| equity | 19,947,353 | 99.0% | 4.98 |

Rules for research code:

- Never index `bids_price[5]` or branch on `length == 5`. Any depth-N feature
  must filter `length(bids_price) >= N` (and the ask side independently), or
  fall back to L1/L2.
- **Futures and equities may be treated as full depth; options may not.**
  Option depth falls monotonically with quote activity (>=500K rows/symbol:
  55.1% full L5; <10K rows/symbol: 0%, avg 0.77 levels) and with distance from
  ATM (calls >4,500 pts OTM/ITM: 0% full L5).
- Bid and ask depth are independent — only 29.4% of option BidAsk rows have
  equal depth on both sides (avg bid 2.98 vs ask 2.08). One-sided and empty
  books are normal: 5.66M rows have exactly one side populated and 617,117 have
  neither; 94% of both are options.
- `make research-data-quality` therefore reports `depth_shape: fail` on any
  options-heavy range, driven by `empty_bidask_rows`. That is expected. Only
  `ragged_depth` — a price/volume length mismatch **within one side** — is a
  real corruption signal (it was 0 on the range above).
- The archive-wide "full L5 %" is a **mix statistic, not a quality metric**. It
  fell 93.7% (202601) -> 61.7% (202609) purely because options grew from 1.2%
  to 47.7% of BidAsk rows; equity and future depth never left 93-99% in any
  month. Do not read a falling blended rate as feed degradation.

The L2 exporter drops rows outside a TAIFEX session (clock window **and** XTAI calendar membership) and records `session_filtered_rows` / `session_rule` in the sidecar. `--allow-non-session` relaxes the calendar half only.

Canonical governed L2+tick export is `research.data_pipeline` via `make research-export-l2-ticks`. Sidecar/data-root rules live in `.agent/skills/hft-alpha-research/references/data-governance.md`. `research/tools/ch_batch_export.py` is legacy/L1 wrapper and must not reimplement sidecar/dtype governance.

Source-layer quality is audited by `make research-data-quality DATE_FROM=... DATE_TO=...` (`research/data_pipeline/quality.py`). Advisory, read-only; writes `research/reports/data_quality/*_source_audit.{json,md}`. Its verdict is stamped into dataset sidecars as `source_quality_*`. `ts_causality` (`exch_ts` may never lead `ingest_ts`) is the invariant that the 2026-01/02 +8h shift broke — run the audit before trusting a new or re-pulled date range. See `docs/modules/data_quality.md`.

Every export needs metadata sidecar with dataset ID, source, rows, symbols, date, fingerprint, and data UL/provenance. L2 exports dedup identical BidAsk within 0.5 ms where applicable.

Large queries must set memory limits and preserve deterministic ordering by exchange timestamp/sequence.
