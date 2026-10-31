# Market structure notes: where the official rows are, what calendar spreads carry, the equity tick stream, and an old basis question

Analysis only. No code, config or data was changed. The futures numbers were measured on 2026-10-18 over the
19 parsed official futures days 2026-09-07 to 2026-10-05. Notes on row concentration, calendar-spread
rows, the recorded equity tick stream, and two older research questions (ETF close flow, TXF-TMF basis).

## Short answers

| question | answer |
|---|---|
| Where are the official rows? | Three products hold 65.27% of the 13,480,206 rows: TMF 35.84%, MTX 21.00%, TX 8.42%. There are 380 product codes, but 18 of them reach 80% of the rows and 82 reach 95%. From rank 18 down a product has under 0.6% of the rows, so "380 products" overstates the universe a cross-product study can use. |
| Are calendar-spread rows a term-structure series? | Not in this sample. There are 230,414 spread rows (1.71%). In TX, 80.1% of them (28,325 of 35,345) fall on two days, 2026-09-14 and 2026-09-15, and 96.1% (33,958) are the 202609/202610 pair: a roll-week event, not a daily series. |
| What does a spread row's price mean? | Exactly `far_price - near_price` on every row where it is not zero (227,784 rows); the other 2,630 rows are zero. |
| Can the leg prices be used as outright prices? | The near leg usually, the far leg rarely. For TX spread rows, the near-leg price equals an outright TX print of that month within 5 s on 97.3% of rows; the far-leg price on 23.4% (an outright far-month print exists within 5 s on 73.3%). Use the spread `price`, not the far leg. |
| Why are 39,414 spread rows negative? | `far - near < 0` means the far month traded below the near month, which is legal. 4,791 of them (12.2%) are in one product, CHF, where 96% of its spread rows are negative; in TX only 1 of 35,345 is. |
| Is the recorded equity Tick stream complete? | No, according to a memory note (2330 in September: 3-10% of the official transaction count). **Not re-measured here**: no local ClickHouse server is running and no credentials are set in this shell. The official side of the comparison is stored locally: 43,438 to 198,779 transactions per day. |
| ETF close flow (00631L / 00632R)? | Depends on equity data, so it is limited by the line above. Use BidAsk or official daily data. |
| TXF-TMF basis, re-run with the underlying price and bid side? | An answer already exists: **C74, the TXF-TMF basis mean-reversion maker, was run on 10 days and killed at T6 on 2026-04-19, on cost.** The repo's own manifest, README and pipeline index still say "prototype". See "The basis question". |

## What was measured

Source: `~/hft_research_data/official/parsed/fut_ticks/<day>.parquet`, written by the official-data parser on
the unmerged branch `rp/official` (PR #555). A row is one executed trade. `spread` is true when the expiry
contains a `/` (`research/data_pipeline/official.py:918` on that branch), for example `202609/202610`; such a
row carries `near_price` and `far_price` as well as `price`. The script reads the parsed files only, does not
import the parser, and is read-only.

```python
import glob
import os
from collections import Counter, defaultdict

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = os.path.expanduser("~/hft_research_data/official/parsed/fut_ticks")
files = sorted(glob.glob(f"{ROOT}/*.parquet"))

rows = Counter()  # (product, is_spread) -> rows
negative = Counter()  # product -> spread rows priced below zero
convention = Counter()  # how a spread row's price relates to its two leg prices
tx_pairs = Counter()  # TX spread rows by expiry pair
tx_spread_by_day = Counter()
leg = Counter()  # TX spread rows: do the leg prices appear on the outright tape?
total = 0
for f in files:
    t = pq.read_table(f)
    total += t.num_rows
    day = os.path.basename(f)[:10]
    for r in t.group_by(["product", "spread"]).aggregate([("second", "count")]).to_pylist():
        rows[(r["product"], r["spread"])] += r["second_count"]
    sp = t.filter(t["spread"]).to_pydict()
    for p, e, price, near, far, sec, date in zip(sp["product"], sp["expiry"], sp["price"], sp["near_price"], sp["far_price"], sp["second"], sp["date"]):
        negative[p] += price < 0
        if price == 0:
            convention["price is 0"] += 1
        elif abs(price - (far - near)) < 1e-9:
            convention["price = far - near"] += 1
        elif abs(price - (near - far)) < 1e-9:
            convention["price = near - far"] += 1
        else:
            convention["neither"] += 1
    # TX: is each leg price on the outright tape of that expiry within +-5 s?
    tx = t.filter(pc.and_(pc.equal(t["product"], "TX"), pc.invert(t["spread"]))).to_pydict()
    tape = defaultdict(list)
    for e, sec, price, date in zip(tx["expiry"], tx["second"], tx["price"], tx["date"]):
        tape[(e, date)].append((sec, price))
    tape = {k: (np.array(sorted(v))[:, 0], np.array(sorted(v))[:, 1]) for k, v in tape.items()}
    txs = t.filter(pc.and_(pc.equal(t["product"], "TX"), t["spread"])).to_pydict()
    for e, near, far, sec, date in zip(txs["expiry"], txs["near_price"], txs["far_price"], txs["second"], txs["date"]):
        tx_pairs[e] += 1
        tx_spread_by_day[day] += 1
        for name, expiry, price in (("near", e.split("/")[0], near), ("far", e.split("/")[1], far)):
            seconds, prices = tape.get((expiry, date), (np.array([]), np.array([])))
            lo = np.searchsorted(seconds, sec - 5, side="left")
            hi = np.searchsorted(seconds, sec + 5, side="right")
            window = prices[lo:hi]
            leg[f"{name} leg: outright prints within 5 s"] += len(window) > 0
            leg[f"{name} leg: price equals one of them"] += bool(np.isclose(window, price).any())

products = {p for p, _ in rows}
per_product = Counter()
for (p, _), n in rows.items():
    per_product[p] += n
spread_rows = sum(n for (_, s), n in rows.items() if s)
print(f"days {len(files)}  rows {total}  product codes {len(products)}")
print(f"spread rows {spread_rows} ({100 * spread_rows / total:.2f}%)  priced below zero {sum(negative.values())}")
cum = 0
print("share of all rows, cumulative:")
for rank, (p, n) in enumerate(per_product.most_common(), 1):
    cum += n
    if rank <= 5 or (cum >= 0.8 * total > cum - n) or (cum >= 0.95 * total > cum - n):
        print(f"  #{rank:<3d} {p:4s} {100 * n / total:5.2f}%  cumulative {100 * cum / total:5.2f}%  spread rows {rows[(p, True)]}")
print("all rows of TX / MTX / TMF:", {p: per_product[p] for p in ("TX", "MTX", "TMF")})
print("spread rows of TX / MTX / TMF:", {p: rows[(p, True)] for p in ("TX", "MTX", "TMF")})
print("negative spread rows of TX / MTX / TMF:", {p: negative[p] for p in ("TX", "MTX", "TMF")})
print("negative spread rows: CHF", negative["CHF"], "of", rows[("CHF", True)], "| all others", sum(negative.values()) - negative["CHF"])
print("spread price vs legs:", dict(convention))
print("TX spread pairs:", tx_pairs.most_common(3))
print("TX spread rows on the 3 busiest days:", tx_spread_by_day.most_common(3))
for k in sorted(leg):
    print(f"  {k}: {leg[k]} of {sum(tx_pairs.values())}")
```

```text
days 19  rows 13480206  product codes 380
spread rows 230414 (1.71%)  priced below zero 39414
share of all rows, cumulative:
  #1   TMF  35.84%  cumulative 35.84%  spread rows 1255
  #2   MTX  21.00%  cumulative 56.84%  spread rows 2390
  #3   TX    8.42%  cumulative 65.27%  spread rows 35345
  #4   CHF   2.38%  cumulative 67.64%  spread rows 4979
  #5   CCF   1.93%  cumulative 69.58%  spread rows 19560
  #18  CAF   0.56%  cumulative 80.43%  spread rows 2003
  #82  CZF   0.09%  cumulative 95.04%  spread rows 1078
all rows of TX / MTX / TMF: {'TX': 1135689, 'MTX': 2830632, 'TMF': 4831605}
spread rows of TX / MTX / TMF: {'TX': 35345, 'MTX': 2390, 'TMF': 1255}
negative spread rows of TX / MTX / TMF: {'TX': 1, 'MTX': 14, 'TMF': 0}
negative spread rows: CHF 4791 of 4979 | all others 34623
spread price vs legs: {'price = far - near': 227784, 'price is 0': 2630}
TX spread pairs: [('202609/202610', 33958), ('202610/202611', 932), ('202610/202612', 206)]
TX spread rows on the 3 busiest days: [('2026-09-14', 15857), ('2026-09-15', 12468), ('2026-09-11', 2321)]
  far leg: outright prints within 5 s: 25904 of 35345
  far leg: price equals one of them: 8257 of 35345
  near leg: outright prints within 5 s: 35172 of 35345
  near leg: price equals one of them: 34380 of 35345
```

## Findings

1. **The official universe is three products.** TMF, MTX and TX make 65.27% of the rows. Rows are trades,
   not contracts and not quotes, so this says where the trades are, not where the volume or the recorded
   BidAsk rows are.
2. **Spread rows are a roll-week feature.** In TX they are 3.1% of the product's rows (35,345 of 1,135,689),
   80.1% of them sit on 2026-09-14 and 2026-09-15, and 96.1% belong to one pair. Those are the two trading days
   before 2026-09-16, the third Wednesday of September 2026 and the usual last trading day of a TAIFEX monthly
   contract; that rule was not checked against the exchange calendar. A daily term-structure series from these
   rows would be empty most days.
3. **The spread `price` is the observation; the far leg is derived.** The near leg lands on the outright tape
   almost always, the far leg rarely, which is what a far leg computed from the near leg and the spread
   would look like. That is an inference from the match rates, not something read from an exchange rule.
4. **Negative spreads are legal and mostly outside the three products the platform trades.** 39,414 rows, 34,623
   of them outside CHF; TX, MTX and TMF together have 15 (1, 14, 0). They are not a data error: the R01 integrity
   gates on the unmerged branch `feat/research-official-tick-gates` list `negative_spread_price` as information
   only, and its table counts the same 39,414 of 230,414 and 15 for the three products.
5. **The recorded equity Tick stream is incomplete** (memory, not re-measured). The same note says pre-open rows
   (08:30-09:00) carry cumulative or trial volumes and recommends equity BidAsk instead of Tick flow.

## The equity tick stream (A11) and the ETF close flow (C6)

The claim comes from the auto-memory note `feed_timing_and_direction_properties_2026_10` (outside the repo):
for 2330 in September the recorded Tick stream holds 3-10% of the official transaction count.

What is available here is the official side: TWSE's September daily table for 2330, stored at
`~/hft_research_data/official/probe/twse_STOCK_DAY_2330_202609.json` (outside the repo), 20 trading days,
43,438 transactions on 2026-09-04 up to 198,779 on 2026-09-02. What is missing is the recorded side: one
`count()` of 2330 Ticks per day from `hft.market_data`. It needs a ClickHouse that can be queried, which
this session does not have, so 3-10% stays a memory figure.

C6 (`docs/research/r36_20_directions.md:14`, priority B, "0-10 pts, MEDIUM risk") trades the close flow of two
leveraged ETFs, so it needs equity trade data. Until the recorded Tick stream is shown to be complete, it
should be built on BidAsk or on official daily data, and any feature built on the recorded Tick flow should
say so.

## The basis question (C11)

The inventory asks to re-run the TXF-TMF basis and lead-lag using the underlying price and the bid side.
The repo already holds three answers about this family:

| what | verdict | where |
|---|---|---|
| `TX-TMF-leadlag` (R26, R28) | killed: "2.47 pts edge vs 7.4 pts cost; D6-only artifact"; the strategy file is still there | `src/hft_platform/strategies/tx_tmf_leadlag.py`; the quoted reason is in the R57 F2 checklist (local only, below) |
| R57 F2 `TXF_TMF_microprice_basis_taker_signal_v1` | rejected at Tier 0 (`mechanism_bounded_by_half_spread`); edge 6-8 pt against a total cost of 8 pt | R57 summary and F2 checklist (local only, below) |
| **C74** `c74_txf_tmf_basis_mean_reversion`, maker version | **KILL at T6, 2026-04-19.** 10 days (2026-03-31 to 2026-04-14), 36,760 trips, mean gross 797 NTD per trip (3.98 pt in TXF terms); break-even round trip about 3.2 pt for both legs, against about 6.0 pt retail; the Devil's Advocate gate note puts the independent daily t-statistic at 1.155 with 9 degrees of freedom and calls the Sharpe of 5.8 a small-sample artifact | T5 scorecard and Gate C note (local only, below) |

Three things follow.

- **The repo's own records disagree with the evidence.** `research/alphas/c74_txf_tmf_basis_mean_reversion/manifest.yaml`
  has `status: prototype` (the README says "prototype (T2 APPROVED)"), and `research/reports/pipeline_index.json`
  lists it as `PROTOTYPE` with `run_count: 0` and an empty `latest_run_id`. The README and the manifest still give
  the basis as `mid_txf - 20 x mid_tmf`; the scorecard and the Gate C note record that formula as wrong (the T5 run
  first saw a basis mean near -675,000 pt) and the fix as `mid_txf - mid_tmf`, which is what `impl.py` computes
  (`_compute_basis`, line 358). The README also still carries the T2 cost line (600 NTD per trip, "cost_drag 20%")
  while the T5 scorecard puts the retail cost at 6.0 pt for the pair.
- **The evidence is not in git.** The scorecard and the Gate C note live under `outputs/`, which `.gitignore:86`
  excludes; the R57 files live under `artifacts/` (`.gitignore:83`); the index is covered by `research/*`
  (`.gitignore:92`). On a fresh clone none of them exist. The numbers in this section come from the copies on this
  machine and cannot be reproduced from the repository.
- **The verdict is a cost verdict, and its cost basis is a memory note.** The scorecard judged the strategy at an
  unconfirmed institutional 3.0 pt round trip (Gate C: conditional pass, +213,910 NTD per day) and at the retail
  6.0 pt it takes from the memory note `feedback_taifex_fee_structure` (Gate D: fail, -1,991,690 NTD per day). The
  separate cost correction of 2026-04-18 (`r6_cost_model_correction`: TXF round trip about 3 pt, not 0.48 pt)
  predates the C74 run, so C74 was judged on the corrected basis. Whether today's fees still match was **not
  checked**.

"Underlying price" would add a spot basis (futures against the index), which is a different quantity from C74's
residual (one index future against another of a different size). C11 as worded mixes the two; which one is meant
is a decision.

## Where the evidence lives

| evidence | in git? |
|---|---|
| `docs/research/r36_20_directions.md`, `research/alphas/c74_txf_tmf_basis_mean_reversion/` (README, manifest, `impl.py`), `src/hft_platform/strategies/tx_tmf_leadlag.py` | yes, on `origin/main` |
| `research/data_pipeline/official.py` | no, branch `rp/official` (#555) |
| `docs/research/official-tick-gates.md` | no, branch `feat/research-official-tick-gates` |
| `outputs/team_artifacts/alpha-research/archive/halted-2026-04-19-inst-options/round-10/artifacts/executor_t5_scorecard.md` and `da_t6_gate_c.md` | no, ignored (`.gitignore:86`) |
| `docs/research/round-R57/artifacts/T2_R57_summary.md` and `T2_R57_F2_killchecklist.md` | no, ignored (`.gitignore:83`) |
| `research/reports/pipeline_index.json` | no, ignored (`.gitignore:92`) |
| `~/hft_research_data/official/probe/twse_STOCK_DAY_2330_202609.json` | outside the repo |
| auto-memory notes | outside the repo |

## Not verified

- The recorded equity Tick stream against the official counts (needs a queryable ClickHouse).
- Whether 2026-09-14 and 2026-09-15 are the roll days out of the September TX contract.
- The CHF product's meaning and why its spreads are almost all negative.
- Anything outside the 19 days; one month of one regime.
- The C74 numbers are quoted from ignored local files and were not re-run.
- Whether the C74 KILL survives today's cost assumptions.

## Decisions for Charlie

| decision | suggestion |
|---|---|
| Bring the C74 record in line with its evidence (manifest status, README formula, pipeline index) and keep the T5 and Gate C notes somewhere tracked | Yes, but these are frozen research manifests, so it needs your explicit instruction and a review; this note changes none of them. |
| Is C11 a spot basis (needs the underlying price) or the TXF-TMF residual (already killed on cost)? | Decide which before any run; the residual should only be re-run if the cost basis changed. |
| Re-measure the equity Tick stream | When a ClickHouse is available; one count per day against the 20 official numbers. |
| Use calendar-spread rows for a term-structure series | Not from this sample; derive it from outright prints of two expiries instead, and treat roll weeks separately. |
