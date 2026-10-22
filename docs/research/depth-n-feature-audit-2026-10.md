# Depth-N feature audit (2026-10-17)

Scope: every feature the platform's `FeatureEngine` emits (`feature/registry.py`, sets `lob_shared_v1` to `v3`, 27 features,
indices 0-26) checked against the rule in `.agent/rules/70-research-data.md` ("Book depth is variable length, never assume 5"):

> Any depth-N feature must filter `length(bids_price) >= N` (and the ask side independently), or fall back to L1/L2.
> Futures and equities may be treated as full depth; options may not.

Source: `origin/main` `53f8b815`. Read-only audit; no code or test was changed. Line numbers are `file:line` at that commit.
Not covered: the research-side feature code under `research/`, and the Rust fused feature pipeline beyond the point noted in F4.

## 1. Classification

"Depth" here means how many priced levels a feature reads. The live book event carries 0 to 5 levels per side
(`len(event.bids)`); the totals the engine receives (`bid_depth`, `ask_depth`) are sums over however many levels exist
(`feed_adapter/lob_engine.py:245-275` in Python, `rust_core/src/fast_lob/stats.rs:18-30` in Rust).

Indices follow `feature/registry.py`: v1 is 0-15, v2 adds 16-21, v3 adds 22-26.

| idx | feature | reads | depth rule status |
|---|---|---|---|
| 0-3 | `best_bid`, `best_ask`, `mid_price_x2`, `spread_scaled` | L1 price | n/a (L1) |
| 4 | `bid_depth` | **sum over all priced levels** | **unfiltered variable-N sum** (F1) |
| 5 | `ask_depth` | same | **unfiltered variable-N sum** (F1) |
| 6 | `depth_imbalance_ppm` | the two sums above (`engine.py:804-808`) | **unfiltered variable-N** (F1) |
| 7 | `microprice_x2` | L1 price and L1 qty | n/a (L1); see F2 for the qty fallback |
| 8-9 | `l1_bid_qty`, `l1_ask_qty` | L1 qty (`_extract_l1_qty`, `engine.py:1302`) | n/a (L1); fall back to the total depth sum when the top qty is unreadable (F2) |
| 10 | `l1_imbalance_ppm` | L1 qty | n/a (L1) |
| 11-13 | `ofi_l1_raw`, `ofi_l1_cum`, `ofi_l1_ema8` | L1 price and qty | n/a (L1) |
| 14 | `spread_ema8_scaled` | spread | n/a |
| 15 | `depth_imbalance_ema8_ppm` | **`l1_imbalance_ppm`**, not the depth sums (`engine.py:862`) | n/a (L1), but the name says depth (F3) |
| 16 | `ofi_depth_norm_ppm` | OFI EMA over the average **L1** qty (`engine.py:958-963`) | n/a (L1), but the name says depth (F3) |
| 17 | `ret_autocov_5s_x1e6` | mid price | n/a |
| 18 | `tob_survival_ms` | L1 prices | n/a |
| 19 | `impact_surprise_x1000` (ISS) | OFI, mid, and `bid_depth + ask_depth` as the baseline impact coefficient (`engine.py:1019-1020`) | **unfiltered variable-N sum** (F1) |
| 20 | `deep_depth_momentum_x1000` (MLDM) | L2-L5 quantities | guards fewer than 2 levels only; an absent level reads as 0 (F5) |
| 21 | `toxicity_ema50_x1000` | trade tape | n/a |
| 22-26 | `ofi_l1_ema5s`, `ofi_l1_ema30s`, `imbalance_ema5s_ppm`, `spread_ema30s`, `spread_ema300s` | L1 | n/a (L1) |

## 2. Findings

**F1 - `bid_depth`, `ask_depth`, `depth_imbalance_ppm`, and ISS's baseline use a sum whose N changes per event.**
Severity: low for the live universe, high for options. The sum covers the levels present, so a book of five 10-lot levels
reads 50, and the same book after losing its two outer levels reads 30, with no flag that N changed.
`depth_imbalance_ppm` is a ratio of two such sums, and the bid and ask sides can differ in N
(`70-research-data.md`: only 29.4% of option BidAsk rows have equal depth on both sides).
For futures and equities the rule says to treat depth as full (96-99% of rows have 5 levels), and the live loop is
frozen to `r47_tmf_v1` (one future, `config/loops/r47_tmf_v1.yaml`), so the live universe is not materially affected.
For options (55% full L5 above 500K rows/symbol, 0% below 10K rows, per the rule) these features are not comparable across
time or strikes. This is a **precondition to record**, not a live defect: before any option alpha reads
`bid_depth`, `ask_depth`, `depth_imbalance_ppm` or ISS, filter on level count or use L1.

**F2 - `_extract_l1_qty` substitutes the total depth sum for the top quantity when the top is unreadable** (`engine.py:1302-1318`).
`_top_qty` (`engine.py:88-107`) returns `None` when the side is `None` or when reading it raises (a broad `except`
that swallows the error), and then `l1_bid_qty` becomes `bid_depth`: all levels under an L1 name. When `event` itself is `None` the
same substitution applies. Everything built on L1 qty (`microprice_x2`, `l1_imbalance_ppm`, OFI, `ofi_depth_norm_ppm`) then mixes
scales for that event. An empty side correctly returns 0, so this fires only for an absent or malformed side. How often that happens
in production is **NOT VERIFIED** (it needs the fallback counted on a running engine).

**F3 - two names say "depth" but read L1.** `depth_imbalance_ema8_ppm` is an EMA of `l1_imbalance_ppm`, and `ofi_depth_norm_ppm`
divides by the average **L1** quantity. Neither violates the rule (L1 exists whenever a book does), but a reader who takes the names
literally will assume they react to deeper levels. A docstring note in `registry.py` would be enough; renaming a feature id changes
the schema and the parity fixtures, so it is not proposed.

**F4 - with `HFT_FEATURE_ENGINE_BACKEND=rust` every feature after index 15 is zero.** `feature/engine.py:1236-1240` pads the
16-feature Rust output with zeros, and the constructor logs `rust_feature_backend_v1_gap` once (`engine.py:306-316`). Under the
Rust backend ISS and MLDM are constant 0, so the findings above do not apply to it. The default backend is `python`.

**F5 - MLDM treats an absent level as 0 lots.** The `thin_book` guard (`engine.py:1111`) already zeroes the signal for fewer than
2 levels per side and is pinned by `tests/unit/test_feature_engine_v2.py:225`. What is not guarded is a level count that changes
between 2 and 5 while the BBO does not move: the missing outer level reads as its whole quantity leaving the book.
Measured offline on the local L5 exports (`research/data/l5`, `research/data/l5_v2`: untracked, present only in the main checkout; padded 5-level arrays; a level counts as present when its
price is above 0; the engine's `deep_net` was recomputed per consecutive pair, with no BBO shift and not thin, and compared with a
version that sums only levels present in both snapshots; the script was a scratch file and is not committed):

| file | rows where `deep_net` is computed | level count changed | artifact share of the feature's total absolute magnitude |
|---|---|---|---|
| `l5/TXFD6_l5.npy` | 2,013,575 | 63 | 0.11% |
| `l5_v2/TXFE6_l5.npy` | 3,223,386 | 215 | 1.07% |
| `2330`, `2317` stock files | 0.5M-1.0M each | 0 | 0% |

Limits: **TMF, the instrument R47 trades, was not measured**; the TXF files never show fewer than 3 levels, so they may have been
filtered; level counts come from the padded array, not from live `len(event.bids)`. No live alpha reads this feature
(`research/alphas/c75_tmf_mw_ofi_taker/README.md:58` drops it on purpose).

## 3. What to do with it

| finding | proposed action | needs |
|---|---|---|
| F1 | none now; record the precondition for option features; optionally add a `quality_flags` bit when `len(bids) != len(asks)` | your decision (changes schema) |
| F2 | count the fallback (a new counter, e.g. `feature_l1_qty_fallback_total`) before deciding anything | a small code change, Tier 3 |
| F3 | docstring note in `registry.py` | trivial, optional |
| F4 | none; already warned | n/a |
| F5 | none now; if MLDM is ever consumed, count only levels present in both consecutive snapshots | your decision (changes feature values and parity) |

## 4. Reproducing the code reading

```
sed -n 130,170p src/hft_platform/feature/registry.py          # v1 specs, incl. bid_depth, depth_imbalance_*
sed -n 796,870p src/hft_platform/feature/engine.py            # v1 values: l1 vs depth sums
sed -n 1016,1022p src/hft_platform/feature/engine.py          # ISS baseline uses bid_depth + ask_depth
sed -n 1058,1116p src/hft_platform/feature/engine.py          # MLDM and the thin_book guard
sed -n 1302,1318p src/hft_platform/feature/engine.py          # _extract_l1_qty fallback
sed -n 245,275p src/hft_platform/feed_adapter/lob_engine.py   # python depth sums
```
