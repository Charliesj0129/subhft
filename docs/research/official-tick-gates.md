# Official tick files: integrity gates, and why they are never deduplicated

Scope: the parsed layer written by `python -m research data-pipeline official parse`
(`<root>/parsed/<dataset>/<day>.parquet` plus `<day>.summary.json`; code in
`research/data_pipeline/official.py`). Measured on the 19 local `fut_ticks` days, 2026-09-07
to 2026-10-05, 13,480,206 rows. Nothing here talks to the network, ClickHouse or the platform.

## 1. Running the gates

```
make research-official-fetch ACTION=check ARGS='--dataset fut_ticks'
make research-official-fetch ACTION=check ARGS='--dataset fut_ticks --day 2026-10-05'
make research-official-fetch ACTION=check ARGS='--dataset fut_ticks --products TX,MTX,TMF,CAF'
```

`check` is read-only: it opens the Parquet and summary files and prints one JSON report. It does
not fetch, parse a zip, or write into the data root. Exit codes: 0 no gate failed (information
findings do not fail), 1 at least one gate failed, 2 bad arguments (unknown dataset or day, or no
parsed files yet; the message says to run `parse --dataset <dataset>` first).

| check | severity | what it flags |
|---|---|---|
| `non_integer_price` | fail | a single-leg price that is not a whole point (NaN and infinity included) |
| `price_not_positive` | fail | a single-leg price at or below zero |
| `price_missing` | fail | a null price on an in-scope row |
| `contracts_not_positive` | fail | contracts at or below zero, spread rows included |
| `contracts_missing` | fail | null contracts on an in-scope row |
| `summary_mismatch` | fail | per (product, expiry, session, spread): contracts or trade count in the Parquet differ from `summary.json`, or a group exists on one side only |
| `summary_missing`, `summary_unreadable` | fail | no `summary.json` next to the Parquet, or it is not a list of well-formed rows |
| `parquet_missing` | fail | a non-empty summary with no Parquet beside it |
| `schema` | fail | a required column is absent |
| `negative_spread_price` | info | calendar-spread rows with a price below zero |
| `empty_day` | info | no Parquet and an empty summary (the day held no ticks) |
| `no_in_scope_rows` | info | none of the `--products` appear that day, so the price and contract gates did not run |

`no_in_scope_rows` exists so that a mistyped `--products` cannot pass silently.

### Scope, and why

The price and contract gates apply to `TX`, `MTX`, `TMF` by default and, for prices, only to
single legs. That is what the data supports:

| measured on the 19 days | rows |
|---|---|
| in-scope rows (TX, MTX, TMF) | 8,797,926 |
| non-integer single-leg prices, TX/MTX/TMF | 0 |
| single-leg prices at or below zero, all products | 0 |
| contracts at or below zero, all products | 0 |
| non-integer single-leg prices, all products | 1,670,176 across 281 products |
| spread rows priced below zero, all products | 39,414 of 230,414 spread rows |
| spread rows priced below zero, TX/MTX/TMF | 15 rows on 3 of the 19 days |
| `summary.json` groups that differ from the Parquet | 0 of 19 days |

Most of the 281 products are stock and other non-index futures that quote fractional prices, so
a whole-point rule over every product would fail on legitimate data. A spread row carries a
price *difference*, which is legitimately negative. Option premiums can be fractional too, so
`--dataset opt_ticks` gets the summary reconciliation only. **No `opt_ticks` day is parsed on
this machine, so that path is covered by synthetic fixtures and has not been run on real
option data.**

Run against the 19 days the gate reports `failed: false` with only the `negative_spread_price`
information rows above.

## 2. Why official ticks must not be deduplicated

The tick files have no trade id, no side (a futures quantity is `B+S`, an options quantity is
`B or S`) and no time finer than one second. Two rows that agree on every column therefore
cannot be told apart from two real trades.

```
 raw rows            13,480,206
 repeat an earlier   4,750,885  (35.24%)  key: product, expiry, second, price,
                                           contracts, auction, spread
                     4,750,268  (35.24%)  same key plus near_price, far_price, session
```

A repeat can be several fills in the same second at the same price and size, and the one day
and product checked against official volume says that is what they are: on 2026-10-05, TX front
month (`202610`), the tick file reproduces the official `DailyMarketReportFut` volume to the
contract with every repeat kept, and loses a fifth to a quarter of it without them. Other days
and products were not checked against an official report.

| session | single-leg contracts in the tick file | official daily volume | single-leg contracts after `distinct` |
|---|---|---|---|
| day | 41,765 (+93 from calendar-spread legs = 41,858) | 41,858 | 31,241 (-10,524, -25.2%) |
| night | 32,209 (+79 from calendar-spread legs = 32,288) | 32,288 | 25,312 (-6,897, -21.4%) |

The official volume of a contract month includes the legs of calendar spreads, which is the
`+93` and `+79` (`reconcile_volume` adds each spread's volume to both of its months).
`official reconcile --day 2026-10-05 --contract TX` returns difference 0 on all 15 (expiry,
session) rows it reports; a `distinct` would break that. Rule: **never deduplicate the official tick files, and never `distinct` them**. Use each
row as one trade, and compare against another source only in aggregate (daily totals by
session, or per-second volume), not row by row.

This is unrelated to duplicate *delivery* in the locally recorded feed, which is a different
problem with its own handling; it says nothing about those rows.

## 3. Not covered

- Real option ticks (see above), and any day outside the 19 local ones.
- Scheduling: nothing runs `check` automatically; running it after each `parse` is an operator
  step. Wiring its result into the daily report or metrics is a separate decision.
- The reconcile check against the official volume is one day (2026-10-05, TX). The snapshot used
  is the probe file stored with the data; other days need their own stored official report.
- Time-axis and duplicate-delivery checks on the recorded ClickHouse data live in
  `research/data_pipeline/quality.py` (`make research-data-quality`); they do not apply to the
  official files.

## 4. Reproducing the numbers

```
make research-official-fetch ACTION=check ARGS='--dataset fut_ticks'
make research-official-fetch ACTION=reconcile \
  ARGS='--day 2026-10-05 --contract TX --official-file <root>/probe/taifex_openapi_DailyMarketReportFut.json'
```

The repeat counts and the `distinct` effect come from a `group_by` over the Parquet columns
named in the key above (a scratch script, not committed). The `distinct` figures are single-leg
contracts only; the effect of `distinct` on the spread rows was not measured.
