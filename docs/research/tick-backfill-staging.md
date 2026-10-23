# Staging Shioaji historical ticks for archive gaps

Module: `research/data_pipeline/tick_backfill.py`. Tests: `tests/research/test_tick_backfill.py`
(offline: a scripted fake source and a fake clock). **Nothing here logged in or called the broker.**
Every statement about what `api.ticks()` returns comes from the repo's own notes, not from a call.

## 1. What already exists, and why this is a second tool

`scripts/backfill_historical_ticks.py` already fetches `api.ticks()` and inserts the rows into
`hft.market_data`. Read from the source, this is what it does to the archive:

| line | behaviour | consequence |
|---|---|---|
| 22, 76 | inserts into `HFT_CLICKHOUSE_HOST` (default `clickhouse`, the compose service) | run inside the engine container it writes to the production table |
| 161, 165 | no check of what the day already holds | a *partial* day (12 sessions) has its recorded ticks duplicated; the archive already suffers from duplicate delivery |
| 143 | `ingest_ts = exch_ts` | the receive latency is erased for those rows |
| 144 | `int(price * 1_000_000)` truncates | `int(2.01 * 1_000_000)` is `2009999`; for 1,196 of the first 99,999 prices on a 0.01 grid (up to 1,000) the result is exactly one unit short |
| 151 | `trade_direction` from the broker's `tick_type` | recorded rows carry the EMO classifier's inference (`trade_classifier.py`): one column, two definitions |
| 161, 165, 167 | the result of `ch_insert` is dropped; `fetch_and_insert` returns the number of ticks *fetched* | a failed insert is printed as "inserted" |
| 29, 38 | symbols are a fixed list of 2026-04 contracts (`TXFD6`...) and 50 stocks | it cannot fill a February or July gap without editing the file |
| 75 | the ClickHouse password goes into the request URL | it ends up in the server's query log |
| 217 | `--dry-run` still logs in | a dry run uses one of the 5 broker sessions |

Nothing in `src/`, `scripts/` or `research/` calls `HistoricalGateway.get_ticks`
(`feed_adapter/shioaji/historical_gateway.py`, which wraps the same API) except the facade's pass-through
(`facade.py:181`); only its unit tests call it.

## 2. What the new module does

It keeps the useful part (one call per symbol and day, spaced) and changes what is done with the
answer. Each response is converted once and staged as `<out_dir>/<YYYY-MM-DD>/<symbol>.parquet`.
It writes through a `.partial` name, never overwrites a staged file, and never touches ClickHouse.
It imports neither the SDK nor a ClickHouse client (a test pins that); the caller logs in and hands
it a `TickSource`.

| column | meaning |
|---|---|
| `symbol`, `exchange`, `instrument_type`, `source` | request fields and the constant `api_ticks_backfill` |
| `seq` | position in the response, so a re-run gives the same rows |
| `ts_local_ns` | the broker's `ts`, exactly as received (never passed through a float: it is beyond 2**53) |
| `exch_ts` | `ts_local_ns - 8 h`, matching the archive's UTC epoch ns; **see 3** |
| `price_scaled` | price x 1,000,000, exact on the shortest decimal form, half-even |
| `volume`, `tick_type` | as received; `tick_type` is *not* turned into `trade_direction` |
| `bid_price_scaled`, `ask_price_scaled`, `bid_volume`, `ask_volume` | the quote at each trade, if the response carries it; null otherwise |

Column types are fixed (text, or int64 with nulls), so a day with the quote columns and a day without read as one dataset.

Not written: `trade_direction`, `ingest_ts`, `bids_*`/`asks_*` arrays. A backfilled row has no book and
no receive time, so those columns stay the archive's to fill or to leave empty.

Run behaviour: a staged file is skipped without a call; an empty answer stages nothing; one bad
answer is recorded and the run continues; **a `Blocked` from the source, or 3 errors in a row, stops
the run**; requests are validated (ISO day, plain symbol name) before the first call.

## 3. What is not known, and the one probe that answers it

None of this was verified, because every fact needs a logged-in session:

- **The timestamp zone.** `exch_ts = ts - 8 h` is the existing script's assumption and consistent with the
  early-2026 +8 h repair, but no call was made. The raw value is kept so it can be undone.
- **How far back `api.ticks()` goes** for futures and for expired contracts, and its quota. The staging
  limit is a conservative 1 s between calls, not a measured allowance.
- **Which contract codes to ask for** on a past day (dated codes such as `TXFD6` versus a continuous one).
- **The book cannot come back.** `api.ticks()` returns trades with the quote at the trade, not the 5-level
  book, so a day degraded for lack of BidAsk rows is not restored by this; only the Tick rows are.

The probe: in one authorised session, request one past day for one symbol, compare its trade count,
volume and first/last stamps with the archive's rows for that day, and read the error for a date older
than the retention. That decides whether the February-July gaps are recoverable at all.

## 4. Decisions this change does not make

- Whether to log in and run it (a broker session, so a per-operation grant).
- How staged rows are merged into the archive and deduplicated against a partly recorded day.
- Whether `trade_direction` for staged rows is re-derived with the EMO classifier from the staged quote
  columns (consistent with the archive) or left empty.
- Whether `scripts/backfill_historical_ticks.py` is retired. This change does not edit it.

## 5. Driving it (sketch, NOT RUN)

```python
from pathlib import Path
from research.data_pipeline import tick_backfill as tb

class ApiTicks:                      # written by the operator after logging in
    def __init__(self, api, contract_for): ...
    def fetch(self, request):        # one api.ticks(contract=..., date=request.day) call
        ...                          # raise tb.Blocked on a rate-limit or session error

plan = tb.plan_requests(Path("staging"), days, symbols, exchange="FUT", instrument_type="future")
report = tb.run_backfill(ApiTicks(api, contract_for), [r for r, staged in plan if not staged], Path("staging"))
```
