# Pre-open and simulated-match windows: what the official trades say

Analysis only. No code, config or data was changed. Measured on 2026-10-17 over the 19 parsed
official futures days 2026-09-07 to 2026-10-05. Follow-up on the TJF pre-open trades and on the
`is_simtrade` flag.

## The two questions

1. **Does the clock rule throw away real trades?** The research archive (`hft.market_data`) is cleaned
   by a clock rule: drop 08:30-08:44 and 13:45-14:59, keep the sessions 08:45-13:45 and 15:00-05:00
   (Taipei). The reason is that the recorded feed carries simulated-matching prints in those minutes. The
   rule is written down in an auto-memory note (`market_data_archive_usable_ranges`, outside this repo),
   not in a repo file. The only code that uses the same windows is a *check* in
   `research/data_pipeline/quality.py` (`evaluate_session_window`, line 238): it counts rows outside the
   sessions per day and warns above 3% (`SESSION_WINDOW_OUTSIDE_MAX_RATIO`, line 58). It does not remove rows.
2. **Could the feed's own flag `is_simtrade` replace the clock windows?**

## Short answers

| question | answer |
|---|---|
| TX, MTX, TMF (what the platform trades) | The rule loses nothing. 8,797,926 official rows over 19 days, none outside the sessions: no trade between 05:00:01 and 08:44:59, nor between 13:45:01 and 14:59:59, on any date. |
| every product | 1,540 of 13,480,206 rows (0.0114%) and 12,563 of 22,055,628 contracts (0.057%) are outside the parser's sessions. They belong to 17 products. |
| is the recorded 08:30-08:44 bucket real trading? | No, for the index futures. The official trade file holds no TX, MTX or TMF trade in that window, so recorded prints there cannot be executed trades. This agrees with the note. (The note's own figures, about 178K ticks and a 30,500-47,810 price span, were not re-measured.) |
| do the repo's definitions of "in session" agree? | No. The official parser and the quality check disagree on 812 of the 1,540 rows (13 in the closing minute, 799 at 15:00 and later; see below). |
| can `is_simtrade` replace the windows? | Not testable today, and not for old days at all. See "What the flag can and cannot do". |

## What was measured

The official layer is `~/hft_research_data/official/parsed/fut_ticks/<day>.parquet`. Each row is one
executed trade from TAIFEX's daily tick file; `contracts` is the file's buy-plus-sell quantity halved.
The `session` column is `day` (08:45:00-13:45:00 on the trading day), `night` (15:00 on an earlier date to
05:00 on the trading day) or `other` (anything else), decided in whole seconds by `tick_session()` in
`research/data_pipeline/official.py`. That module is on the unmerged branch `rp/official` (PR #555), not on
`origin/main`; the script below reads the parsed files only and does not import it.

The script is read-only. Its output on this machine:

```python
import glob
import os
from collections import Counter, defaultdict

import pyarrow.parquet as pq

ROOT = os.path.expanduser("~/hft_research_data/official/parsed/fut_ticks")
H, M = 3600, 60
WINDOWS = [  # clock windows for the rows the parser labelled "other"
    ("before 08:30", 0, 8 * H + 30 * M - 1),
    ("08:30-08:44", 8 * H + 30 * M, 8 * H + 45 * M - 1),
    ("13:45:01-13:45:59", 13 * H + 45 * M + 1, 13 * H + 46 * M - 1),
    ("13:46-14:59", 13 * H + 46 * M, 15 * H - 1),
    ("15:00 and later", 15 * H, 24 * H - 1),
]


def hms(x):
    return f"{x // H:02d}:{x % H // M:02d}:{x % M:02d}"


sessions = Counter()
by_product = defaultdict(Counter)  # product -> session -> rows
span = {}  # product -> [first, last] second of its "other" rows
win_rows, win_contracts, win_products = Counter(), Counter(), defaultdict(set)
tjf_early = Counter()  # TJF rows before 08:45, by quarter-hour
odd = Counter()  # properties of the "other" rows
total_contracts = 0
files = sorted(glob.glob(f"{ROOT}/*.parquet"))
for f in files:
    t = pq.read_table(f, columns=["product", "second", "session", "contracts", "auction", "date", "trading_day"])
    cols = [t[n].to_pylist() for n in ("product", "second", "session", "contracts", "auction", "date", "trading_day")]
    for p, sec, s, c, auction, date, trading_day in zip(*cols):
        sessions[s] += 1
        by_product[p][s] += 1
        total_contracts += c or 0
        if s != "other":
            continue
        odd["auction=True"] += bool(auction)
        odd["dated before the trading day"] += str(date) != str(trading_day)
        lo_hi = span.setdefault(p, [sec, sec])
        lo_hi[0], lo_hi[1] = min(lo_hi[0], sec), max(lo_hi[1], sec)
        label = next(n for n, lo, hi in WINDOWS if lo <= sec <= hi)
        if p == "TJF" and sec < 8 * H + 45 * M:
            tjf_early[hms(sec // (15 * M) * 15 * M)[:5]] += 1
        win_rows[label] += 1
        win_contracts[label] += c or 0
        win_products[label].add(p)

rows = sum(sessions.values())
print(f"days {len(files)}  rows {rows}  contracts {total_contracts}")
print("sessions", dict(sorted(sessions.items())))
print("other rows by clock window (rows / contracts / products):")
for name, _, _ in WINDOWS:
    print(f"  {name:16s} {win_rows[name]:5d} {win_contracts[name]:6d} {len(win_products[name]):3d}")
other = sessions["other"]
print(f"  {'all':16s} {other:5d} {sum(win_contracts.values()):6d}   share of rows {100 * other / rows:.4f}%")
print("products with 'other' rows (rows, other, share, first-last):")
for p in sorted(span, key=lambda k: -by_product[k]["other"]):
    n = sum(by_product[p].values())
    o = by_product[p]["other"]
    print(f"  {p:4s} {n:7d} {o:5d} {100 * o / n:5.1f}%  {hms(span[p][0])}-{hms(span[p][1])}")
for p in ("TX", "MTX", "TMF"):
    print(f"  {p:4s} {sum(by_product[p].values()):7d} {by_product[p]['other']:5d}")
print("TJF rows before 08:45 by quarter-hour:", dict(sorted(tjf_early.items())))
print("other rows with:", {k: odd[k] for k in ("auction=True", "dated before the trading day")})
```

```text
days 19  rows 13480206  contracts 22055628
sessions {'day': 8830247, 'night': 4648419, 'other': 1540}
other rows by clock window (rows / contracts / products):
  before 08:30        37    135   1
  08:30-08:44         33    227   1
  13:45:01-13:45:59    13     38   7
  13:46-14:59        658   4523  17
  15:00 and later    799   7640  15
  all               1540  12563   share of rows 0.0114%
products with 'other' rows (rows, other, share, first-last):
  RZF     5371   420   7.8%  13:45:48-16:14:28
  TJF     1306   389  29.8%  08:00:02-16:13:59
  TGF     1090   223  20.5%  13:45:12-16:13:15
  XJF     1025   162  15.8%  13:45:30-16:12:48
  OKF      556   115  20.7%  13:45:14-16:14:21
  UJF      566    52   9.2%  13:45:01-16:10:24
  RTF      135    32  23.7%  13:52:58-15:59:57
  GDF      228    30  13.2%  13:53:57-16:14:16
  XEF       63    28  44.4%  14:04:16-16:12:24
  RHF       62    23  37.1%  14:07:42-15:54:05
  OJF      134    17  12.7%  14:13:52-15:53:39
  XBF       44    14  31.8%  13:49:05-16:07:36
  XAF       55    10  18.2%  14:31:04-16:08:22
  OAF       86     9  10.5%  13:49:34-16:11:47
  UKF       53     8  15.1%  13:45:52-16:09:59
  OOF       56     5   8.9%  14:01:43-14:20:16
  RXF       40     3   7.5%  14:00:36-14:13:38
  TX   1135689     0
  MTX  2830632     0
  TMF  4831605     0
TJF rows before 08:45 by quarter-hour: {'08:00': 16, '08:15': 21, '08:30': 33}
other rows with: {'auction=True': 0, 'dated before the trading day': 0}
```

The last line of the output shows that none of the 1,540 `other` rows has `auction = True` and none is dated
before its trading day. They lie between 08:00:02 (TJF) and 16:14:28 (RZF).

### The same rows under three definitions

| clock window | rows | `tick_session()` (seconds) | `quality.py` check (whole minutes) | archive rule as written in the note |
|---|---|---|---|---|
| 08:00-08:29 | 37 | other | outside | not named; outside the kept sessions, so dropped |
| 08:30-08:44 | 33 | other | outside | dropped |
| 13:45:01-13:45:59 | 13 | other | **inside** (minute 13:45) | edge not stated |
| 13:46-14:59 | 658 | other | outside | dropped |
| 15:00-16:14 | 799 | other | **inside** (night, 15:00 and later) | **kept** |

So 799 rows are kept by the clock rule and the check but labelled `other` by the official parser, and 13 more
sit in the closing minute. Which label is right is not known (see below).

## Findings

1. **TX, MTX and TMF are clean.** The three products have 1,135,689, 2,830,632 and 4,831,605 rows and no
   `other` row, which means no trade between 05:00:01 and 08:44:59 or between 13:45:01 and 14:59:59.
2. **TJF is the only product that trades before 08:45.** 70 rows between 08:00 and 08:44 (16, 21 and 33 in the
   08:00, 08:15 and 08:30 quarter-hours, which matches the inventory). The 08:30-08:44 part is 33 rows and 227
   contracts. The rule as named drops only that part; any "keep only the sessions" rule also drops the other
   37. Over the whole sample 389 of TJF's 1,306 rows (29.8%) are `other`.
3. **All 17 products have trades after 13:45** (TJF also before 08:45), from 13:45:01 until at most 16:14:28
   (RZF). Their `other` share of rows ranges from 7.5% (RXF) to 44.4% (XEF); the volumes are small (RZF, the
   largest of the 17, has 5,371 rows in all 19 days).
4. **The cost of the clock rule is small overall but not uniform.** For the platform's products it is zero.
   For the 17 products it is between 7.5% and 44.4% of their rows, which would matter for a strategy on one
   of them and for a per-product row-count comparison between the archive and the official files.
5. **Nothing in the live path acts on the flag today** (next section).

## What the flag can and cannot do

- It originates in the Shioaji tick: `src/hft_platform/feed_adapter/normalizer.py:889,900` read `simtrade`.
  It rides on `TickEvent.is_simtrade` (`src/hft_platform/events.py:63`) and in the bus tuple
  (`src/hft_platform/engine/event_bus.py:83,455`).
- The Rust kernels carry it too (`rust_core/src/fast_lob/normalize_tick.rs:58`, `rust_core/src/bus/ring_tick.rs:9`).
  The only Python consumer unpacks it into an unused name: `src/hft_platform/feed_adapter/lob_engine.py:732` (`_is_simtrade`).
  No feature, strategy or risk code in `src/` reads it (searched `src`, `rust_core`, `config` and `scripts`;
  `scripts/latency/shioaji_sim_soak.py:144` only builds a fake payload).
- **The recorder on `origin/main` has no column for it** (no `simtrade` in `src/hft_platform/recorder/`), so
  no archived row carries the flag.
- PR #575 (`feat/recorder-tick-integrity-columns`, head `5ef37bb6`, open when checked) adds
  `is_simtrade UInt8 DEFAULT 0` through the migration `20261006_001_add_tick_integrity_columns.sql`, plus
  the mapper, worker and loader changes. Using it needs four separate steps: merge, apply the migration on
  the host, redeploy the engine that writes the column, then let days accumulate. The middle two are
  production operations and are not part of this task.
- **`DEFAULT 0` means "real" for every row recorded before the engine writes the column.** After the migration
  the old days read as if nothing were simulated. Any check must start at the first full day the new engine
  wrote the column, which is what the `{first_day}` parameter below is for.
- Consequence: the flag can never reclassify the days already in the archive. The clock windows must stay for
  all of them, flag or no flag.
- Whether the SDK sets the flag in 13:46-14:59 as well as before the open is **NOT VERIFIED**. The memory
  note only says the 13:45-14:59 prints exist.

### The check to run once there is data (NOT RUN against real data)

```sql
WITH toHour(fromUnixTimestamp64Nano(exch_ts, 'Asia/Taipei')) * 60
     + toMinute(fromUnixTimestamp64Nano(exch_ts, 'Asia/Taipei')) AS minute_of_day
SELECT
    toDate(fromUnixTimestamp64Nano(ingest_ts, 'Asia/Taipei')) AS day,
    multiIf(
        minute_of_day >= 510 AND minute_of_day <= 524, '08:30-08:44',
        minute_of_day >= 826 AND minute_of_day <= 899, '13:46-14:59',
        'elsewhere'
    ) AS clock_window,
    is_simtrade,
    count() AS ticks
FROM hft.market_data
WHERE type = 'Tick'
  AND exch_ts > 0
  AND toDate(fromUnixTimestamp64Nano(ingest_ts, 'Asia/Taipei')) >= {first_day:Date}
GROUP BY day, clock_window, is_simtrade
ORDER BY day, clock_window, is_simtrade
```

It was run only in `clickhouse-local` on eight synthetic rows (one outside `first_day`, one `BidAsk`), where
it grouped them as expected. It has not touched `hft.market_data`. What to look at: ticks inside the two
windows with `is_simtrade = 0`, and ticks elsewhere with `is_simtrade = 1`. This document proposes no
threshold; what counts as "close enough to replace the windows" is a decision (below).

## Recommendations

1. **Keep the clock windows for the archive.** For TX, MTX and TMF they cost nothing, and they are the only
   cleaning that exists for the days already recorded.
2. **Do not switch to the flag yet.** Re-run the check after #575 is merged, migrated and deployed and some
   trading days have accumulated.
3. **Do not apply a global window to other products without a per-product trading-hours table.** The repo
   holds none.

## Decisions for Charlie

| decision | suggestion |
|---|---|
| Which "in session" definition is canonical for research: whole seconds (`tick_session`) or whole minutes (`quality.py`), and does 15:00-16:14 on the trading date count as session | Keep as is for TX, MTX, TMF (identical there). Decide only if a strategy on another product appears. |
| Fetch TAIFEX's per-product trading hours | Same question as the pending "why fetch?" for the official daily data (R02). Not needed for the platform's three products. |
| When to run the flag check | After #575 is deployed and at least a few trading days exist. Each step before that is a production operation. |

## Not verified

- Why 17 products print between 13:45:01 and 16:14, and whether the 799 trades at 15:00-16:14 dated on a
  trading day belong to an extended session of those products or to the next night session. Needs the
  exchange's per-product hours table.
- Whether the `other` trades were executed in continuous trading or are special prints (combination orders,
  block trades). The file's `auction` column is `False` on all of them.
- The flag's coverage and whether it is set in 13:46-14:59.
- Anything about days outside 2026-09-07 to 2026-10-05; 19 days is one month of one regime.
- The memory note's own figures (178K ticks, price span, 91 days), which were not re-measured here.
- Whether PR #575 is still open; it was when checked (2026-10-17).

## Sources

| what | where | on `origin/main` |
|---|---|---|
| in-session check | `research/data_pipeline/quality.py` lines 50-58, 238, 682-687 | yes |
| flag origin | `src/hft_platform/feed_adapter/normalizer.py:889,900` | yes |
| flag field and carriers | `src/hft_platform/events.py:63`, `src/hft_platform/engine/event_bus.py:83,455`, `src/hft_platform/feed_adapter/lob_engine.py:732`, `rust_core/src/fast_lob/normalize_tick.rs:58`, `rust_core/src/bus/ring_tick.rs:9` | yes |
| official parser and `tick_session()` | `research/data_pipeline/official.py` | **no**, branch `rp/official` (#555) |
| flag column | `src/hft_platform/migrations/clickhouse/20261006_001_add_tick_integrity_columns.sql` | **no**, branch `feat/recorder-tick-integrity-columns` (#575) |
| the cleaning rule | auto-memory `market_data_archive_usable_ranges` | outside the repo |
