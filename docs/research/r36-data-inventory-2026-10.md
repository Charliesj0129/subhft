# R36 direction triage: the data each direction needs, what the repo has, and what is already answered

Scope: candidate inventory item I5 and the research directions C1-C12 (plan item R03). Everything is read
at `origin/main` `53f8b815`. This note changes no code, config or data. It fetched nothing and contacted
no host or broker; the one network call was a read-only `gh pr view` (PR #575 is OPEN; the call for #571
failed, so #571's state was not re-read).

This note issues **no KILL**. `.agent/skills/taifex-alpha-kill-criteria/SKILL.md` says a KILL rests on that
candidate's own backtest. Where an older candidate's result bears on a direction, the note lists what that
result measured and what it did not, and leaves the call to you.

The numbers in `docs/research/r36_20_directions.md` ("5-25 pts net", "25-75 pts") are that list's own
estimates, written 2026-04-08. The file gives no source or run for them, so they are treated as unverified.

## Short answers

| # | Direction | Where it stands | What stops it | Next step |
|---|---|---|---|---|
| C1 | TSMOM 5-20 day | Data first, and the gate does not fit | No multi-year daily series in the repo. The strict profile asks for 200 out-of-sample trades; a 5-20 day hold fits 6 or 1 in 30 days (section 4.5) | Decide the gate profile before getting data |
| C2 | Institutional flow momentum | Data first | No institutional feed is ingested; the exchange API takes no date parameter | Start daily snapshots, or a manual download |
| C3 | VIX / vol spillover | Data first | 0 tracked code files mention VIX; no source chosen | Decide whether to use any external series |
| C4 | Overnight gap reversal | The fade is answered | T1-E tested the fade: 30 events (floor 80), mean +5.5 pts (floor 10), median -6.5 | Do not re-run the fade; a new spec only if the gap or horizon differs (4.1) |
| C5 | Settlement pin / max-pain | Futures settlement-day variants answered as "too few events"; TXO variant untested | R60 F1/F3: 3.8-4.6 events a year; T1-F: 2 events. TXO needs per-strike open interest history | Let daily snapshots accumulate |
| C6 | ETF close flow | Data first | Recorded equity tick stream is incomplete; R36's own range starts at 0 pts | Use BidAsk or daily data; low priority |
| C7 | HMM regimes | Not answered | The one earlier HMM diagnostic used 8 in-sample days and collapsed (one state with sigma 1e-10); `regime_lab` is rule-based, not an HMM | Treat as a filter on a parent signal; park until one exists |
| C8 | TXO skew / IV / gamma | Data first | ATM coverage of the recorded chain; `underlying_price` is not recorded | After the TXO window fix and #575 |
| C9 | Exchange-reported direction | Measurement first | `tick_type` is not recorded; measuring agreement needs a broker login | Your grant for A2 |
| C10 | c75 multi-window OFI taker | Nothing blocks a first run | "GATE_B" is the unit-test gate; no scorecard exists (4.3) | Re-derive the cost floor, then one Gate C run |
| C11 | TXF-TMF basis | Answered once | C74 was killed at T6 on cost (R10 note) | Decide which basis is meant |
| C12 | Long shots | Low priority | Each has a stated reason (section 5) | None |

## 1. How to read the evidence

- **tracked** means the file is in git at `53f8b815`. Anyone can re-check it.
- **local only** means the file exists in the author's working tree but is git-ignored. The `research/` tree is
  an allowlist in `.gitignore` (`research/*` at line 92, `research/tools/*` at line 177,
  `research/experiments/*` at line 117), and `artifacts/` and `outputs/` are ignored too. A fresh clone does not
  have these files. They are quoted here and marked.
- Two probes (section 6) print every figure that is not just a quotation. Probe 1 reads only tracked files at
  the pin. Probe 2 reads local-only files and the official-data root outside the repo.

## 2. The data items the directions share

```
 daily snapshots of the exchange API (no date parameter; +1 day per run)
   |-- institutional flow ------------> C2 --------> C7 (as a bias filter)
   |-- per-strike OI, settlement -----> C5 (TXO) --> C8 (partly)
   '-- final settlement price --------> C4 variant (settlement-based gap)
 Shioaji kbars (login; quota unknown) -> years of daily bars -> C1
 recorded ticks (already here) ---------> C3 (TW realised vol), C7, C10, C11
 tick_type (not recorded; A2 login) ----> C9
```

| Data item | In the repo or data root now | Missing | Ways to get it |
|---|---|---|---|
| Years of daily TX / MTX / TMF bars (C1, C2 as target, C5) | Research raw files from 2026-01-26 (first TMFD6 session-day in `docs/runbooks/tmfd6-corpus-2026-05-06.md`); 19 official tick days in the data root [local only] | Years of daily bars | `futDataDown` was BLOCKED on 2026-10-06 (an HTML page came back, the run stopped under the fetch policy). The `get_kbars` wrapper exists (`src/hft_platform/feed_adapter/shioaji/historical_gateway.py:101`) and has no caller except a probe script; its retention and quota are NOT VERIFIED and it needs a login |
| Institutional net volume and open interest per day (C2, C7) | The stored swagger lists a futures and an options endpoint with `Item`, `TradingVolume(Net)`, `OpenInterest(Net)` [local only]; no response is stored. `research/alphas/r52_amhp_dynamic_spread/backtest_t5_extensions.py:225-226` says the feeds are "NOT ingested" and uses a prior-day signed-volume proxy | Any history | The API takes no parameters (0 of 135 paths, probe 2) and the one stored response holds a single date, so history is assumed to grow one day per run; other endpoints were not seen. About five months of daily runs reach 100 trading days (21 days a month, arithmetic only) |
| Per-strike open interest and settlement price, final settlement price (C5, C8) | `DailyMarketReportOpt` has `StrikePrice`, `OpenInterest`, `SettlementPrice`; `FinalSettlementPrice` has the day and the price [local only]. 0 tracked code files mention `FinalSettlementPrice` | No stored response of either | Same as above |
| Volatility index and US overnight reference (C3) | 0 tracked code files mention VIX or VIXTWN. R52 used a prior-day TMFD6 close-to-open return as a stand-in | Any VIX series | An external source is your decision; this note fetched nothing |
| TXO chain with ATM coverage, underlying price (C8) | The recorded chain. `option_chain_atm_coverage` is not on main (0 hits). A memory note (outside the repo) puts front-month ATM coverage at 5 of 134 days | Coverage; `underlying_price` is not recorded | #571 (window centred on the underlying; state not re-read) and #575 (OPEN; adds `total_volume`, `is_simtrade`, `is_odd_lot`, not `tick_type` or `underlying_price`) |
| Exchange-reported trade direction (C9) | `src/hft_platform/trade_classifier.py` infers direction (EMO) | `tick_type` is not in `src/` | Measuring agreement is inventory item A2 and needs a broker login |
| Equity trades for 00631L / 00632R (C6) | R10 note (`docs/research/market-structure-notes.md` on branch `docs/market-structure-notes`, not on main): the recorded Tick stream for 2330 is 3-10% of the official count, from a memory note | ETF data; a completeness check | BidAsk, or exchange daily tables (a TWSE month table for 2330 is stored, so that site is month-addressable; ETFs and the institutional table were not probed) |

## 3. Per direction

Legend for the four kill modes: **HIT** a recorded result on file shows it; **BINDS** arithmetic on tracked
floors shows it; **RISK** could happen, not shown; **OPEN** depends on a choice not made; **n/a**.

| # | Cost wall | Too few events | Maker fill assumption | Recent-data prior |
|---|---|---|---|---|
| C1 | RISK: fee+tax round trip is 3.0 pts (TXF) or 4.0 (TMF); the 2x rule needs 6 or 8 pts of expected edge before any spread | BINDS: section 4.5 | n/a (a position held for days) | OPEN: not started |
| C2 | RISK: R36 says 15-45 pts, unverified | BINDS as a daily position; OPEN as a filter on intraday trades | n/a | OPEN |
| C3 | RISK: R36's 25-75 pts is the largest claim in the list and has no run behind it | BINDS if traded daily | n/a | OPEN |
| C4 | HIT for the fade: mean +5.5 pts after an 8-pt cost, floor 10 [local only] | HIT: 30 events against a floor of 80 [local only] | n/a (executable bid/ask) | The out-of-sample split (from 2026-03-26) is positive but has 9 events on one contract |
| C5 | HIT for the futures settlement-day variant: T1-F's stop-controlled edge is below the frozen 10-point floor (its README status line) | HIT: R60 F1/F3 rejected on 3.8-4.6 events a year [local only]; T1-F had 2 events | n/a | OPEN. The TXO variant was not tested |
| C6 | RISK: R36's range is 0-10 pts | OPEN | n/a | The data is incomplete (section 2) |
| C7 | n/a (a filter) | RISK: R29's challenger wrote that "120 days for HMM with 2-3 states = textbook overfit"; the one diagnostic had 16 days | n/a | OPEN |
| C8 | OPEN: option spreads were not measured here | OPEN | OPEN | OPEN: coverage is the blocker |
| C9 | n/a (a measurement) | n/a | n/a | n/a |
| C10 | RISK: 4.0 pts fee+tax, plus the spread it crosses as a taker | BINDS: the taker profile asks for 200 trades and 30 days; the corpus had 26 session-days on 2026-05-06 | n/a (IOC taker) | OPEN: never run |
| C11 | HIT for C74: killed at T6 on cost (R10 note, evidence local only) | n/a | The C74 version was a maker | n/a |
| C12 | See section 5 | | | |

## 4. Pre-checks that need more than a cell

### 4.1 C4 against T1-E

T1-E (`research/alphas/t1e_txf_opengap_fade_tmf/`, tracked) was frozen before its first run. Its rule:
`gap = today's open mid - mean of the prior session's final 30 minutes`; trade only if `|gap| >= 15` TXF
points; fade it; enter at open + 15 minutes; exit at 15, 30 or 60 minutes (headline 30); round-trip cost 8
points; data 2026-01-26 to 2026-04-15, out-of-sample from 2026-03-26. Its pre-registered kill: median net
after cost at 30 minutes at or below zero, among others.

| Measure (probe 2, local only) | Value |
|---|---|
| Verdict, reason | KILL, `t1b_kill:median_net_non_positive` |
| Trading days audited, events | 41, 30 (floor 80) |
| Mean net per trade | +5.5 pts (floor 10, not cleared) |
| Median net, remove-best-1 median, p10 | -6.5, -14.0, -212.6 |
| Stop-breach rate | 0.906 |
| Out-of-sample split | 9 events, one contract (TXFD6), mean +44.4, median +16.0 |

What this does not say:

- The stop-breach rate counts how often the path touched a 15-point extension at any time in the holding
  window (T1-E README: "recorded when the post-entry path touches it"). It is not a continuation edge. Flipping
  the sign is not free either: executable prices cross the other side of the book, so the mirror needs its own
  run.
- The gap is built from day-session boundaries (13:45 close to 08:45 open). The T1-E README records this as a
  promotion blocker and says a governed version would use settlement prices.
- 30 events is below the 80-event floor, and the rule still returns KILL because the median is negative. That
  is the rule working as registered; it is also a small sample.
- The tracked sources for the verdict are `.agent/memory/failed-attempts.md:29` ("E (open-gap fade) all KILL")
  and `research/alphas/t1f_txf_expiration_vreversal_tmf/README.md:7-8` ("overnight gaps *continue*, 90.6%
  stop-breach"). The audit files themselves are ignored.

R36 #2 says "Gap fade/continuation"; only the fade was tested. **Suggestion:** do not re-run the fade. If gap
work is wanted, write it as a new spec that differs in the gap definition (settlement-based, needs the daily
snapshots) or the horizon (days, not 30 minutes), and freeze its floors first.

### 4.2 C7 against `regime_lab` and the earlier diagnostic

- `research/tools/regime_lab` has 33 `.py` files in the author's tree and 1 tracked
  (`snapshot_builder.py`); the rest are ignored. None mentions an HMM (probe 2). It holds a rule classifier,
  a regime-to-factor map audit and event-study harnesses, so it does **not** already cover C7.
- `hmmlearn` is not in `pyproject.toml` (probe 1). The one HMM script in the repo is
  `research/experiments/validations/vrb_diagnostic/hmm_diagnostic.py`, with its result
  `hmm_results.json` (both tracked). Its header says "limited data": 2 states on 8 in-sample days of 5-minute
  TMFD6 bars. The fit collapsed to one state with sigma `1e-10`; the separation ratio was 0.024 where the gate
  needs 1 or more (means at least 2 sigma apart); and the model called 360 of 361 out-of-sample bars trending
  (probe 1). That shows the diagnostic was under-powered. It is not a finding about regimes, and
  `failed-attempts.md` has no HMM entry.
- R29 (`docs/alpha-research/r29_stage1_literature.md`, tracked) planned the regime filter as simple rules
  first, with an HMM only if the rules are insufficient.

**Suggestion:** C7 is a filter, not a source of profit, so it can only be judged on top of a parent signal.
Park it until C1 or C2 has one, and write the state count and the temporal cross-validation before touching
data.

### 4.3 C10 and the meaning of "GATE_B"

- `research/alphas/c75_tmf_mw_ofi_taker/manifest.yaml` is the only tracked manifest with `status: GATE_B`
  (probe 1: 1 of 41; the 41 use 15 different spellings of status and one has none).
- Gate B is "per-alpha pytest" (`docs/runbooks/alpha-development-workflow.md:177-181`). It runs the alpha's unit
  tests. It says nothing about edge, and Gate C (the backtest scorecard) comes after it.
- There is no scorecard: the alpha folder holds code and docs, the experiment folder holds `signal.npy`
  (probe 2). The CHANGELOG says version 0.1.0 "Never executed a real backtest" and version 0.2.0 records no run.
- The 2026-05-06 depth-parity runbook expected c75-class strategies to fail the strict gates on
  `single_day_dominance` and `min_sample_size`. That is an expectation written before any run.
- Cost arithmetic from `config/research/cost_profiles.yaml` (probe 1): TMFD6 fee and tax is
  2 x (1.3 + 0.7) = 4.0 points a round trip, before the spread a taker crosses. The skill says to re-derive the
  cost floor from today's inputs instead of copying a file's number. This note did not.

**Suggestion:** C10 is the only direction that can be tested with data already here. First re-derive the cost
floor and write down the pass criteria; then one Gate C run, most recent month first (the skill's rule), on a
frozen data set.

### 4.4 C11

The R10 note answers it (branch `docs/market-structure-notes`, commit `010edd4b`, not on main): C74, the
TXF-TMF basis maker, was killed at T6 on 2026-04-19 on cost, and its evidence sits in ignored folders. "Re-run
with the underlying price" is a different quantity (futures against the index) from C74's residual (one index
future against another). Which is meant is a decision, and no new work should start before it.

### 4.5 Daily directions against the strict profile

`config/research/profiles/vm_ul6_strict.yaml` asks a taker for `min_oos_trades: 200` and `min_oos_days: 30`
(maker: `min_fills: 300`, `min_days: 60`). Probe 1 section H gives the arithmetic for one instrument and one
position at a time:

| Hold | Whole holds in 30 days | Trading days for 200 holds |
|---|---|---|
| 5 days | 6 | 1,000 |
| 20 days | 1 | 4,000 |

A candidate that holds for days cannot meet the trade-count floor in any window this platform will have. TX,
MTX and TMF are three sizes of one index, so adding them does not add independent trades. The options:

- register a separate daily profile **before** any run, with its floors written down in advance (floors are never
  lowered after a result: AGENTS.md, research governance);
- use the signal only as a filter on intraday trades, whose count does not depend on the number of days;
- drop the direction.

### 4.6 C1 and the feasibility gate

The skill's three pre-code steps, as far as they can be done without data:

1. Cost floor: 3.0 pts (TXF) or 4.0 pts (TMF) a round trip for fee and tax, from the cost profile file. The 2x
   rule asks for 6 or 8 points of expected edge per round trip before spread. Not re-derived from today's fee
   schedule.
2. Horizon: 5-20 days, where cost weighs least.
3. KILL index: `.agent/memory/failed-attempts.md` has 48 lines and one line (29) naming any C-direction
   mechanism (the T1 menu, including T1-D intraday momentum). Nothing covers multi-day trend. T1-D is a different
   horizon and not a verdict on this one.

The gate cannot be completed without a daily series, and the gate profile (4.5) has to be settled first.

## 5. R36 items that C1-C12 leave out, and the long shots

| R36 # | Item | Note |
|---|---|---|
| 7 | RL optimal execution (priority B) | An execution topic, not an alpha. `docs/research/T1_researcher_literature_search.md` lists fill probability and TCA decomposition as the gaps. Needs fill and queue data; the markout work (inventory B2) is the nearer step |
| 10 | FOMC vol crush (C) | Needs an event calendar (none in the code) and an options volatility trade, so it shares C8's blocker |
| 11 | Time-of-day seasonality (C) | Needs only recorded ticks. Day-session quote rates are about 10x night-session rates (docstring of `regime_lab/tod_normalize.py`, local only), so any effect must be normalised per bucket |
| 16 | Rough volatility forecasting (C) | R30 ran a rough-volatility timing rule on TMFD6 ticks: 2 trades, Gate C failed on trade count (`docs/research/alpha-research-r30/stage3_4_gate_c_results.json`, tracked). Too few trades to say anything about the idea |
| 5 | DeepLOB / Transformer | Book depth varies from 0 to 5 levels (AGENTS.md), so training data needs depth filtering; R36 calls it high risk |
| 13 | US-TW NLP sentiment | Needs a text feed; none is known in the repo (not searched exhaustively) |
| 15 | Warrant hedging flow | Needs warrant trade data; none in the repo |
| 17 | Index rebalance | 4 events a year by R36's own label: the same event-count limit that rejected R60 F1/F3 |
| 19 | TXO calendar spread | `DailyVolumeReportOnCalendarSpreadOrders` is futures spread volume; the R10 note says spread rows are not a term structure |
| 20 | Closing auction imbalance | R36's own label: "cost > edge" |

## 6. The probes

Probe 1 runs from the root of any checkout that contains `53f8b815`, with the project interpreter
(`.venv/bin/python -I -`), and reads only tracked files through `git show` and `git grep`.

```python
import collections
import json
import re
import subprocess

import yaml

REF = "53f8b815"
CODE = ["src", "config", "scripts", "research/alphas", "research/experiments",
        "research/data_pipeline", "research/backtest", "research/candidate_loop",
        "research/combinatorial", "research/t1", "research/tools"]


def git(*args, ok=(0,)):
    p = subprocess.run(["git", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert p.returncode in ok, (args, p.stderr)
    return p.stdout


def show(path):
    return git("show", f"{REF}:{path}")


def files_matching(pattern):
    out = git("grep", "-l", "-i", "-E", pattern, REF, "--", *CODE, ok=(0, 1))
    return sorted(line.split(":", 1)[1] for line in out.splitlines())


print("A. code that mentions a data source (tracked code dirs, case-insensitive)")
for label, pat in [
    ("volatility index (VIX, VIXTWN)", r"\bvix\b|vixtwn"),
    ("institutional-flow endpoint or feed", "MajorInstitutionalTraders|\u4e09\u5927\u6cd5\u4eba"),
    ("FinalSettlementPrice endpoint", r"FinalSettlementPrice"),
    ("hmmlearn / GaussianHMM", r"hmmlearn|GaussianHMM"),
]:
    hits = files_matching(pat)
    print(f"  {label}: {len(hits)} file(s)")
    for h in hits:
        print(f"    {h}")

print("B. kbars")
callers = git("grep", "-n", "-E", r"get_kbars\(|\.kbars\(", REF, "--", "src", "scripts", "research", ok=(0, 1))
rows = sorted({line.split(":", 2)[1] for line in callers.splitlines()})
print("  files that define or call kbars:", rows)

print("C. hmmlearn in pyproject.toml:", "hmmlearn" in show("pyproject.toml"))

print("D. earlier HMM diagnostic (hmm_results.json, tracked)")
r = json.loads(show("research/experiments/validations/vrb_diagnostic/hmm_results.json"))
for k in ("n_dates", "is_n_returns", "oos_n_bars", "hmm_sigma", "separation_ratio", "kg_distinguishable", "overall_pass"):
    print(f"  {k} = {r[k]}")

print("E. status of tracked manifests under research/")
names = [n for n in git("ls-tree", "-r", "--name-only", REF, "--", "research").splitlines()
         if n.endswith("manifest.yaml")]
count = collections.Counter()
gate_b = []
for n in names:
    m = re.search(r"^status:\s*(\S+)", show(n), re.M)
    word = m.group(1) if m else "(none)"
    count[word] += 1
    if word == "GATE_B":
        gate_b.append(n)
print(f"  manifests: {len(names)}")
for word, n in sorted(count.items(), key=lambda kv: (-kv[1], kv[0])):
    print(f"  {word}: {n}")
print("  manifests with GATE_B:", gate_b)

print("F. floors in config/research/profiles/vm_ul6_strict.yaml")
prof = yaml.safe_load(show("config/research/profiles/vm_ul6_strict.yaml"))
th = prof["thresholds"]
print("  sections under thresholds:", sorted(th))
for sec in ("taker", "maker"):
    keys = {k: th[sec][k] for k in ("min_oos_days", "min_oos_trades", "min_days", "min_fills") if k in th[sec]}
    print(f"  {sec}: {keys}")

print("G. round-trip fee + tax in config/research/cost_profiles.yaml (points)")
cost = yaml.safe_load(show("config/research/cost_profiles.yaml"))
for sym in ("TMFD6", "TXFD6"):
    c = cost[sym]
    rt = 2 * (c["commission_pts_per_side"] + c["tax_pts_per_side"])
    print(f"  {sym}: 2 x ({c['commission_pts_per_side']} + {c['tax_pts_per_side']}) = {rt:.1f} pts, point value {c['point_value_nwd']} NTD")

print("H. holds a 30-day out-of-sample window can hold, one position at a time")
for hold in (5, 20):
    print(f"  hold {hold:>2} days: whole holds in 30 days = {30 // hold}; trading days for 200 holds = {200 * hold}")

print("I. verdict lines in tracked files")
lines = show(".agent/memory/failed-attempts.md").splitlines()
print("  failed-attempts.md:", [i + 1 for i, l in enumerate(lines) if "E (open-gap fade) all KILL" in l])
t1f = show("research/alphas/t1f_txf_expiration_vreversal_tmf/README.md").splitlines()
print("  t1f README, T1-E KILL line:", [i + 1 for i, l in enumerate(t1f) if "T1-E open-gap-fade KILL" in l])
print("  t1f README, 90.6% line:", [i + 1 for i, l in enumerate(t1f) if "90.6% stop-breach" in l])
kill_words = r"tsmom|multi-?day|trend|momentum|institution|settlement|regime|hmm|gap|basis|etf|skew"
print("  failed-attempts.md lines naming a C-direction mechanism:",
      [i + 1 for i, l in enumerate(lines) if re.search(kill_words, l, re.I)], "of", len(lines), "lines")
```

Output:

```text
A. code that mentions a data source (tracked code dirs, case-insensitive)
  volatility index (VIX, VIXTWN): 0 file(s)
  institutional-flow endpoint or feed: 1 file(s)
    research/alphas/r52_amhp_dynamic_spread/backtest_t5_extensions.py
  FinalSettlementPrice endpoint: 0 file(s)
  hmmlearn / GaussianHMM: 1 file(s)
    research/experiments/validations/vrb_diagnostic/hmm_diagnostic.py
B. kbars
  files that define or call kbars: ['scripts/latency/shioaji_api_probe.py', 'src/hft_platform/feed_adapter/shioaji/facade.py', 'src/hft_platform/feed_adapter/shioaji/historical_gateway.py']
C. hmmlearn in pyproject.toml: False
D. earlier HMM diagnostic (hmm_results.json, tracked)
  n_dates = 16
  is_n_returns = 413
  oos_n_bars = 361
  hmm_sigma = [1e-10, 31.916981737603685]
  separation_ratio = 0.02442283271004685
  kg_distinguishable = False
  overall_pass = False
E. status of tracked manifests under research/
  manifests: 41
  prototype: 11
  DRAFT: 10
  KILLED: 4
  GATE_ZERO: 2
  research: 2
  shadow_scaffolded: 2
  (none): 1
  DEPRECATED: 1
  EXPLORATORY: 1
  EXPLORATORY_INCOMPLETE: 1
  GATE_B: 1
  PARKED: 1
  PROTOTYPE: 1
  draft: 1
  exploration: 1
  exploratory: 1
  manifests with GATE_B: ['research/alphas/c75_tmf_mw_ofi_taker/manifest.yaml']
F. floors in config/research/profiles/vm_ul6_strict.yaml
  sections under thresholds: ['maker', 'taker']
  taker: {'min_oos_days': 30, 'min_oos_trades': 200}
  maker: {'min_days': 60, 'min_fills': 300}
G. round-trip fee + tax in config/research/cost_profiles.yaml (points)
  TMFD6: 2 x (1.3 + 0.7) = 4.0 pts, point value 10 NTD
  TXFD6: 2 x (0.3 + 1.2) = 3.0 pts, point value 200 NTD
H. holds a 30-day out-of-sample window can hold, one position at a time
  hold  5 days: whole holds in 30 days = 6; trading days for 200 holds = 1000
  hold 20 days: whole holds in 30 days = 1; trading days for 200 holds = 4000
I. verdict lines in tracked files
  failed-attempts.md: [29]
  t1f README, T1-E KILL line: [7]
  t1f README, 90.6% line: [8]
  failed-attempts.md lines naming a C-direction mechanism: [29] of 48 lines
```

Probe 2 reads local-only files. It runs from the root of the author's working tree (where the ignored files
are) and reads `~/hft_research_data/official`.

```python
import json
from pathlib import Path

home = Path.home()
root = Path("research/experiments/validations/t1e_opengap_fade_v0")
probe = home / "hft_research_data/official/probe"
data = home / "hft_research_data/official"

print("A. T1-E V0 audit summary (ignored file, local only)")
s = json.loads(next(root.glob("*_summary.json")).read_text())
full = s["splits"]["full"]
oos = s["splits"]["out_of_sample"]
for k in ("verdict", "audited_trading_days", "edge_floor_pts", "edge_floor_cleared"):
    print(f"  {k} = {s[k]}")
print("  reason =", s["research_decision"]["reason"])
for k in ("events", "mean_net_edge_pts_per_trade", "median_net_after_cost_30m", "remove_best_1_median_net", "p10_net_after_cost_30m", "stop_breach_rate"):
    print(f"  full.{k} = {round(full[k], 3) if isinstance(full[k], float) else full[k]}")
print("  hard_gate.min_events / events =", s["hard_gate"]["min_events"], "/", s["hard_gate"]["events"])
print("  out_of_sample: events, contracts, mean, median =", oos["events"], oos["contracts"],
      round(oos["mean_net_edge_pts_per_trade"], 1), oos["median_net_after_cost_30m"])

print("B. c75 files on disk (local tree)")
a = sorted(p.name for p in Path("research/alphas/c75_tmf_mw_ofi_taker").iterdir() if p.is_file())
e = sorted(p.name for p in Path("research/experiments/c75_tmf_mw_ofi_taker").iterdir())
print("  research/alphas/c75_tmf_mw_ofi_taker:", a)
print("  research/experiments/c75_tmf_mw_ofi_taker:", e)
print("  any scorecard.json under either:", any("scorecard" in n for n in a + e))

print("C. regime_lab (local tree)")
rl = sorted(p.name for p in Path("research/tools/regime_lab").glob("*.py"))
text = " ".join(p.read_text() for p in Path("research/tools/regime_lab").glob("*.py")).lower()
print("  .py files:", len(rl))
print("  mentions hmm / hidden markov / GaussianHMM:", any(w in text for w in ("hmm", "hidden markov", "gaussianhmm")))

print("D. official swagger (stored 2026-10-06)")
sw = json.loads((probe / "taifex_openapi_swagger.json").read_text())
paths = sw["paths"]
print("  paths:", len(paths), "| paths with any parameter:", sum(1 for p in paths.values() if p["get"].get("parameters")))


def fields(name):
    comp = sw["components"]["schemas"]

    def deref(node):
        while "$ref" in node:
            node = comp[node["$ref"].split("/")[-1]]
        return node

    node = deref(paths["/" + name]["get"]["responses"]["200"]["content"]["application/json"]["schema"])
    if "items" in node:
        node = deref(node["items"])
    return set(node["properties"])


opt = fields("DailyMarketReportOpt")
print("  DailyMarketReportOpt has StrikePrice, OpenInterest, SettlementPrice:",
      {"StrikePrice", "OpenInterest", "SettlementPrice"} <= opt)
inst = fields("MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")
print("  institutional futures has Item, TradingVolume(Net), OpenInterest(Net):",
      {"Item", "TradingVolume(Net)", "OpenInterest(Net)"} <= inst)
print("  FinalSettlementPrice fields:", sorted(fields("FinalSettlementPrice")))
fut = json.loads((probe / "taifex_openapi_DailyMarketReportFut.json").read_text())
print("  stored DailyMarketReportFut: rows =", len(fut), "| distinct Date =", sorted({r["Date"] for r in fut}))

print("E. official data root (local, outside the repo)")
print("  fut_ticks zips:", len(list((data / "taifex/fut_ticks").glob("*.zip"))),
      "| opt_ticks zips:", len(list((data / "taifex/opt_ticks").glob("*.zip"))))
print("  parsed fut_ticks parquet:", len(list((data / "parsed/fut_ticks").glob("*.parquet"))))
```

Output:

```text
A. T1-E V0 audit summary (ignored file, local only)
  verdict = KILL
  audited_trading_days = 41
  edge_floor_pts = 10.0
  edge_floor_cleared = False
  reason = t1b_kill:median_net_non_positive
  full.events = 30
  full.mean_net_edge_pts_per_trade = 5.5
  full.median_net_after_cost_30m = -6.5
  full.remove_best_1_median_net = -14.0
  full.p10_net_after_cost_30m = -212.6
  full.stop_breach_rate = 0.906
  hard_gate.min_events / events = 80 / 30
  out_of_sample: events, contracts, mean, median = 9 ['TXFD6'] 44.4 16.0
B. c75 files on disk (local tree)
  research/alphas/c75_tmf_mw_ofi_taker: ['CHANGELOG.md', 'README.md', '__init__.py', 'impl.py', 'manifest.yaml']
  research/experiments/c75_tmf_mw_ofi_taker: ['signal.npy']
  any scorecard.json under either: False
C. regime_lab (local tree)
  .py files: 33
  mentions hmm / hidden markov / GaussianHMM: False
D. official swagger (stored 2026-10-06)
  paths: 135 | paths with any parameter: 0
  DailyMarketReportOpt has StrikePrice, OpenInterest, SettlementPrice: True
  institutional futures has Item, TradingVolume(Net), OpenInterest(Net): True
  FinalSettlementPrice fields: ['Contract', 'ContractName', 'DeliveryMonth', 'TheFinalSettlementDay', 'TheFinalSettlementPrice']
  stored DailyMarketReportFut: rows = 2220 | distinct Date = ['20261005']
E. official data root (local, outside the repo)
  fut_ticks zips: 19 | opt_ticks zips: 2
  parsed fut_ticks parquet: 19
```

## 7. Not verified

- Every R36 point estimate. No run stands behind them.
- The state of PR #571 (the call failed). #575 was OPEN when read.
- Whether TWSE's institutional-flow table or ETF daily tables take a date; only the 2330 month table was seen.
- `kbars` retention and quota; whether any past contract can be fetched.
- The recorded-side counts: how many TMFD6 days exist now (the 26 is from 2026-05-06), the front-month ATM
  coverage (a memory figure), the 2330 completeness (a memory figure).
- Today's fee schedule. The cost profile file is read, not re-derived.
- The T1-E, R60 and C74 figures are quoted from ignored files and were not re-run. R60 is in
  `docs/research/round-R60/artifacts/` (ignored by the `artifacts/` rule); its summary says F1 and F3 have
  about 3.8 and 4.6 events a year and expected 0.6-0.7 events in the 58-day span.
- `make agent-docs-check` was not run (no governing document changed).

## 8. Decisions for you

| Decision | Suggestion |
|---|---|
| Gate profile for directions that hold for days (C1, C2, C3, C5) | Register a daily profile with its floors first, or use them only as intraday filters |
| Route to a multi-year daily series | Cheapest: start the daily snapshots now; for years of bars: a manual `futDataDown` download or a login for `kbars` (inventory A13) |
| Whether to want VIX or any external series (C3) | Start with realised volatility from our own ticks |
| C4 | Drop the fade; a settlement-based or multi-day gap only as a new frozen spec |
| C7 | Park until a parent signal exists |
| C10 | Re-derive the cost floor, then authorise one Gate C run |
| C11 | Pick the basis (spot or TXF-TMF residual) before any run |
| Start the daily snapshots at all ("why fetch?") | This note names the beneficiaries: C2, C5 (TXO), the C4 variant |
