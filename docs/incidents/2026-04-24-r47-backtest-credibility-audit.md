# R47 Backtest Credibility Audit — 2026-04-24

**Status**: AUDIT COMPLETE — follow-up P0/P1 pending
**Scope**: Decide whether R47 Maker's backtest results remain credible evidence for
live deployment, after excluding the deployment and execution bugs identified in
`2026-04-21-r47-backtest-live-divergence.md`.
**Related**: `2026-04-21-r47-backtest-live-divergence.md`,
`2026-04-21-deploy-validation.md`, `docs/architecture/latency-baseline-shioaji-sim-vs-system.md`

## TL;DR — The Correct, Trustworthy Answer

R47 Maker backtest credibility decomposes into three evidence classes. As of
2026-04-24:

| Class | Meaning | Verdict |
|-------|---------|---------|
| **C1** Decision parity | Given identical market state, does live strategy emit the same intents as backtest? | **PARTIAL PASS** (no counter-evidence, but also no formal proof) |
| **C2** Execution parity | Does the live broker fill / cancel orders the way the backtest simulator predicts? | **NOT TRUSTABLE** — latency profile is ~5× underestimated |
| **C3** PnL parity (= C1 × C2) | Does live PnL match backtest PnL? | **CANNOT CLAIM** — post-fix live has no fills (sim mode), pre-fix live is confounded by execution bugs |

**Bottom line** (updated 2026-04-24 after Follow-up D direct probe):

1. The **+7,701 figure is invalidated** as deployment evidence (it came
   from instant-RTT, not live tradability).
2. But the **strategy is NOT dead**. Under **directly-measured live broker
   RTT** (place 395 ms / cancel 59 ms, n=300 direct probe), R47 deployed
   config produces **+2,398 NTD across 31 days (Sharpe +2.80)** — modestly
   positive.
3. The earlier −2,332 "R47 dead" read came from a **symmetric 210/210 ms
   derivation that is wrong**. Live broker has heavily asymmetric latency
   (6.7× slower quote-activation vs cancel), which is **favorable to
   maker strategies**; forcing symmetry destroys this.
4. +2,398 / 31 days / 1 winning day / 39 fills is **still not
   promotion-ready evidence** — small sample, likely outlier-driven, etc.
   But it supports **continuing research on R47** rather than abandoning.

See Follow-up D for the full probe methodology and results.

## Audit Method (A+B+C+D)

Per user authorization, the audit exercised all three evidence classes
end-to-end, read-only, against remote ClickHouse on `charl@100.91.176.126`.

### A. C1 — Decision Parity Indicators

Clean shadow window: **2026-04-23 19:37:20 CST ~ 2026-04-24 10:03:31 CST** (the
window after D1–D6 fixes were deployed; broker in sim mode so no fills).

| Indicator | Count / Value | Reading |
|-----------|---------------|---------|
| Total shadow intents | 2,846 | Strategy active |
| BUY intents | 1,421 | |
| SELL intents | 1,425 | |
| BUY/SELL skew | 1,421/1,425 ≈ 0.997 | **Symmetric** (no one-sided degeneration) |
| Distinct symbols | TXFD6 (primary), TMFD6 | Matches deploy config |
| Intent rate (shadow path) | ~3.3 intents/min | Consistent with MM cadence |

**What passes**: rate, symmetry, symbol coverage, timing pattern all match
backtest behavioral fingerprint.

**What is still missing**:

1. **No CANCEL intent recorded.** `shadow_writer.py` writes only submit-side
   intents; cancel intents go straight to the adapter. A large class of C1
   divergence (cancel cadence drift) is therefore invisible in the shadow
   table. Schema-level gap, not a runtime bug.
2. **No formal replay-diff.** There is no CLI that takes the live
   `market_data` feed + deployed R47 config, replays it in offline simulator,
   and diffs the emitted intent stream against `shadow_orders`. Without this,
   C1 is "no observed counter-evidence" not "proven identical".
3. **Intent-id is not session-scoped.** `OrderIdResolver` uses
   `{strategy_id}:{intent_id}` as its compound key, but `intent_id` is a
   per-process counter that resets on restart. Cross-session dedup relies on
   the audit consumer, not the shadow key itself.

### B. C2 — Execution Parity

Measured live broker behavior on `hft.orders` from 2026-04-15..04-21 (228 fills
in the window; pre-fix lifecycle).

#### Broker RTT from status-transition pairing

176 "fast cancel" pairs — orders SUBMITTED then CANCELLED before any fill.
Total elapsed time = submit_rtt + gap + cancel_rtt; with the gap small this
is ≈ 2 × single-side RTT.

| Quantile | Total (submit→cancelled) | Per-side (÷2) |
|----------|--------------------------|---------------|
| P50 | 197 ms | ~99 ms |
| P95 | 419 ms | ~210 ms |
| P99 | 477 ms | ~239 ms |

#### Configured latency profile (deprecated)

`r47_maker_shioaji_p95_v2026-04-09`: submit_ack 36 ms / modify_ack 43 ms /
cancel_ack 47 ms. These are **Shioaji sim-mode RTTs** — not live.

#### Observed order lifetimes (pre-fix, 2026-04-21, incident memo)

- BUY orders: p95 lifetime 4,546 – 7,057 ms
- SELL orders: p95 lifetime 23,904 – 32,363 ms

Both are 1–2 orders of magnitude larger than the 47 ms cancel_ack assumed in
the backtest. The backtest therefore credits R47 with cancel cadence the live
broker cannot physically deliver.

#### Verdict

C2 is **not trustable** under the current v2026-04-09 profile. A replacement
profile `r47_maker_shioaji_p95_v2026-04-24` has been added in this change:

```yaml
r47_maker_shioaji_p95_v2026-04-24:
  submit_ack_latency_ms: 210.0
  modify_ack_latency_ms: 210.0    # conservative — not directly measured
  cancel_ack_latency_ms: 210.0
  measurement_date: "2026-04-24"
```

R47 backtests that want to claim live-tradability must rerun against this
profile.

### C. C3 — PnL Parity

C3 = C1 × C2. Given:

- **Pre-fix live** (through 2026-04-21): PnL −1,722; execution bugs were the
  dominant driver (cancel-stale short-circuit on gate deny, etc. — see D1–D6
  in `2026-04-21-r47-backtest-live-divergence.md`). Cannot be used for C3 comparison.
- **Post-fix live** (after 2026-04-23 19:37:20): broker in sim mode, zero
  real fills. Cannot be used for C3 comparison.
- **Backtest** (+7,701): built on the 47 ms cancel_ack profile, i.e. a C2
  regime that does not exist in the live broker.

**C3 cannot be claimed in either direction.** Specifically:

- The +7,701 backtest number is not falsified — but it is also not supported
  by any live evidence under a correct latency model.
- We do not have the data to say "R47 would have made +X on live post-fix".

### D. Infrastructure side findings

1. **`client_order_id` column** added on remote (`hft.orders`) and mapper
   reads it, but historical events (pre-2026-04-24) carry empty string. Going
   forward the join key exists; backfill is not feasible.
2. **Local uncommitted changes** (`contracts/execution.py`, `core/order_ids.py`,
   `recorder/mapper.py`, `order/shadow_writer.py`, …) appear to be the
   client_order_id propagation work in progress. Not committed by this
   audit — author should review and commit separately.
3. **Remote git** is ahead 5 / behind 46 vs origin with divergent hashes; a
   reconciliation is required before the next deploy. Out of audit scope.

## Actions

### Completed in this change

- [x] Added `r47_maker_shioaji_p95_v2026-04-24` latency profile with measured
      P95 values and notes.
- [x] Marked `r47_maker_shioaji_p95_v2026-04-09` as DEPRECATED for live
      claims (sim-only).
- [x] This audit document.

### P0 — required before re-citing R47 backtest numbers

- [ ] Rerun R47 backtest under `r47_maker_shioaji_p95_v2026-04-24`; document
      PnL delta vs v2026-04-09. If the new PnL is still positive, it becomes
      the citable number. If not, the +7,701 claim must be retracted.
- [ ] Commit the in-flight `client_order_id` propagation so `hft.orders` and
      `hft.fills` are joinable by deterministic key on the next live window.

### P1 — required before next canary

- [ ] Extend `shadow_writer` schema with CANCEL intent + session_id so C1 is
      measurable on cancel cadence, not just submits.
- [ ] Build a replay-diff CLI that consumes `market_data` + deployed R47
      config and emits per-intent divergence vs `shadow_orders`.
- [ ] Reconcile remote/origin divergence before next deploy.

### P2 — before scaling beyond canary

- [ ] Re-enable `HFT_ORDER_MODE=live` for a 1-lot canary, one session, with
      explicit gate for auto-halt on lifetime P95 > 1,500 ms.
- [ ] Investigate the 2026-04-21 24 h-outlier SUBMITTED latency lifecycle
      anomaly flagged during the audit.
- [ ] Directly measure `modify_ack` (currently assumed = cancel_ack P95).

## P0 execution log — wiring gap discovered 2026-04-24

Attempted to execute the P0 "rerun R47 backtest under `r47_maker_shioaji_p95_v2026-04-24`" and hit a wiring gap that forces a scope revision:

### What works

- `research/backtest/maker_engine.py::MakerEngine` accepts `latency_profile:
  LatencyProfile` in its constructor (D5, commit 930be1e4).
- 6/6 tests in `tests/unit/test_maker_engine_latency.py` pass — the
  place_ns / cancel_ns scheduling logic is functional.
- `LatencyProfile.shioaji_p95()` is a canned 800 ms / 800 ms profile
  derived from the live-observed "~792 ms round-trip" in
  `2026-04-21-r47-backtest-live-divergence.md`.

### What doesn't

- `AlphaManifest.latency_profile` is a **string metadata field**. No code
  path loads the YAML, resolves the string to numbers, and injects it into
  MakerEngine. 28 alpha impls declare it; 0 consume it.
- `research/alphas/c60_tmfd6_r47_minimal_inst_rt/run_t5_backtest.py` and
  `.../c63_txfd6_r47_tight_spread/run_t5_backtest.py` have their own
  `_simulate_day` adapted from `MakerEngine._run_day` — they do not
  instantiate `MakerEngine`, so the LatencyProfile hook is unreachable
  from there.
- `src/hft_platform/alpha/_gate_c.py:175` does call `MakerEngine(...)` but
  omits `latency_profile=`.
- The +7,701 cited in the 04-21 memo was produced by an ad-hoc
  `MakerEngine(...)` call that is not checked in to the repo.

### Reconciling 210 ms (measured) vs 800 ms (D5 canned)

| Source | Value | What it measures |
|--------|-------|------------------|
| Remote `hft.orders` 2026-04-15..04-21, 176 fast-cancel pairs, ÷2 | ~210 ms P95 | Broker ack RTT, one side |
| Same pairs, not divided | 419 ms P95 | submit_rtt + cancel_rtt (full cycle) |
| 04-21 incident memo quote | ~792 ms | Round-trip estimate used for D5 default |
| 04-21 memo "min lifetimes" | 94 / 127 ms | RTT floor (fastest observed) |

The numbers are consistent — they sit on the same log-scale distribution.
Using **210 ms one-side (my `v2026-04-24` profile)** is measured and
honest. Using **800 ms one-side (D5 `shioaji_p95()`)** is conservative
(P99-ish). A full rerun should report PnL under both to show sensitivity.

### Revised P0 scope

Item 1 ("rerun R47 backtest under the new profile") is blocked on a
~20-line wiring patch before the rerun becomes meaningful:

1. Add a helper `src/hft_platform/config/latency_profile_loader.py` (or
   similar) that parses `config/research/latency_profiles.yaml` and maps
   a profile name → `LatencyProfile(place_ns, cancel_ns)`.
2. Teach `_gate_c.py::engine` and the c60/c63 `_simulate_day` runners to
   read `manifest.latency_profile`, resolve it via the loader, and pass
   it through.
3. Rerun c60/c63 with both `r47_maker_shioaji_p95_v2026-04-09` (baseline)
   and `r47_maker_shioaji_p95_v2026-04-24` (new); diff the PnL.

Until this patch lands, the +7,701 figure cannot be re-derived with
realistic latency, and any claim about PnL-under-latency is conjecture.

Item 2 ("commit client_order_id propagation") is tractable but needs
human review of the ~20 uncommitted files — I have not touched any of
those in this session.

## P0 comparison results — 2026-04-24 (after wiring patch)

Wiring patch landed:
- `src/hft_platform/alpha/_gate_c.py:163..206` — resolves
  `manifest.latency_profile` via existing `resolve_profile` and injects a
  `LatencyProfile` into `MakerEngine(...)` (fallback = instant-RTT with a
  structlog warning).
- `scripts/compare_r47_latency.py` — driver that sweeps
  `TmfD6SoloMakerMinimal` (C60 R47-minimal) on TMFD6 across four regimes.

Ran on local ClickHouse, 31 days of TMFD6 data, `qf=0.5`,
`TmfD6SoloMakerMinimal(params=C60Params())` (defaults match deployed R47
minimal config — spread_threshold_pts=5, max_pos=1, QI layer enabled).

| Regime | place (ms) | cancel (ms) | fills | PnL (pts) | PnL (NTD) | Sharpe |
|--------|-----------:|------------:|------:|----------:|----------:|-------:|
| No latency (instant-RTT baseline) | 0 | 0 | 4,481 | **+7,721.6** | **+77,216** | +5.04 |
| v2026-04-09 sim (deprecated) | 36 | 47 | 17 | +542.8 | +5,428 | +2.87 |
| **v2026-04-24 measured live** | 210 | 210 | 25 | **−233.2** | **−2,332** | **−2.98** |
| shioaji_p95 canned (conservative) | 800 | 800 | 23 | −363.2 | −3,632 | −2.98 |

### Reading

1. **Instant-RTT baseline gives +77,216 NTD across 31 days** — roughly 10×
   the +7,701 cited in the 04-21 memo. The memo number is for a single day
   (2026-04-21); the 10× scale is consistent with a ~10-day multiplier.
   The optimism-regime is therefore the full-period scale for the baseline.
2. **Fill count collapses from 4,481 → 17..25 the moment any latency is
   present.** Strategy is extremely latency-sensitive — most backtest fills
   exist only because the simulator allows the order to be in and out in
   zero time.
3. **Under measured live-broker latency (210 ms one-side), 31-day PnL is
   −2,332 NTD.** Sharpe flips from +5.04 to −2.98.
4. **Under the canned 800 ms conservative upper bound, PnL is −3,632 NTD**
   — the direction is robust across the 210 ms..800 ms uncertainty band.

### Verdict — updated

The **+7,701 backtest number cannot be cited as live-tradable evidence**.
Under the actual live-broker RTT regime, the same strategy logic on the
same data produces **negative PnL**. The strategy did not "fail in
deployment"; the strategy was **measurement-failing the moment instant-RTT
was assumed**.

This does not prove the *signal* is useless — it proves the *backtest-as-promotion-evidence*
was built on an execution model the live broker cannot deliver. Whether
R47's signal survives execution-model tightening is an open research
question, not a deployment one.

### Caveats

- C60 `TmfD6SoloMakerMinimal` is the MakerEngine-compatible R47-minimal
  variant, not the exact `src/hft_platform/strategies/r47_maker.py` class.
  C60 defaults match the deployed config (spread≥5, max_pos=1, QI layer).
  Faithful reproduction with the live strategy class needs a separate
  bridge (out of P0 scope).
- `LatencyProfile` models place / cancel activation latency only. It does
  not model modify_ack separately (assumed = cancel), strategy compute
  latency, or event-bus queueing.
- 31-day window is local CK's footprint (roughly 2026-03-mid through
  2026-04-17). 04-21 (the live incident day) lives only on remote.
- Fill counts 17..25 under latency are small; results are noisy but the
  sign and direction are robust across profiles.

## Follow-up A — Non-blocking gateway default (2026-04-24)

### Motivation

Shioaji ADVANCED.md documents a 12× caller-return speedup from
`timeout=0`: 136 ms blocking → 12 ms non-blocking. Audit of
`src/hft_platform/order/adapter.py` shows it never passes a `timeout`
kwarg, so every place/cancel/update call hits gateway's
`timeout: int = 5000` default → full blocking wait.

### Scope

This speedup is **not a direct fix for the −2,332 NTD** result — exchange
activation latency (the component in `LatencyProfile.place_ns/cancel_ns`)
is unchanged. It buys:

- strategy thread no longer blocked ≈ 130 ms/call, so multiple orders can
  be in flight per side without D3's `_inflight_*_oids` having to paper
  over a gateway-level serialization
- higher throughput under burst conditions
- unblocks future work on "multiple in-flight orders per side" that D3
  currently handles at the strategy level rather than the transport level

### Patch

`src/hft_platform/feed_adapter/shioaji/order_gateway.py`:

1. Added `_default_order_timeout_ms()` — env-gated:
   `HFT_SHIOAJI_NONBLOCKING=1` → returns 0; otherwise 5000 (preserves
   current production default).
2. Changed all four entry points (`place_order`, `_place_order_typed`,
   `cancel_order`, `update_order`) from `timeout: int = 5000` to
   `timeout: int | None = None`.
3. `_async_kwargs(timeout, cb)` resolves `None` via the env helper.

### Test results

```
tests/unit/test_shioaji_order_gateway.py     15/15 passed
```
...both with `HFT_SHIOAJI_NONBLOCKING=1` and with it unset. The fixture
now clears the env var so blocking-default tests stay deterministic.

Broader adapter / regression coverage:

```
tests/unit/test_adapter_idempotent_cancel.py                        passed
tests/unit/test_order_adapter_mapping.py                             passed
tests/unit/test_order_adapter_dlq_cancel_amend.py                    passed
tests/unit/test_adapter_pydantic_strict_trade.py                     passed
tests/ -k "shioaji_client or order_gateway or adapter_idempotent"   84 passed, 4 skipped
```

Two pre-existing regression failures
(`test_execution_normalizer_price_scale`, `test_long_strategy_id_fallback`)
are reproducible on `git stash`'d HEAD — unrelated to this change.

### `_active_*_oid` single-order-per-side audit

Read of `src/hft_platform/strategies/r47_maker.py:442..456`: the live
strategy already maintains BOTH `_active_buy_oid` (single-slot, last
SUBMITTED wins — retained for the fast-path) AND `_inflight_buy_oids`
(set-valued, authoritative for cancel-all-stale). The single-slot limit
was documented by the D3 fix already; non-blocking mode lets multiple
orders share the transport but the strategy layer already supports it.
No strategy-side change needed.

### Operational note

The env var is **OFF by default** (`timeout=5000` preserved). To try
non-blocking in a canary session:

```bash
HFT_SHIOAJI_NONBLOCKING=1 uv run hft run live
```

Reverts instantly by unsetting. Full code path unchanged in production
until the env var is set.

## Follow-up C — `spread_threshold_pts` sweep under v2026-04-24 (2026-04-24)

### Question

Under measured live-broker RTT (210 ms), is there a `spread_threshold_pts`
setting where PnL returns to positive? (The deployed value is 5.)

### Method

`scripts/sweep_r47_spread.py`, same MakerEngine wiring as the P0 compare.
C60Params defaults except for the swept field. 31 days of TMFD6,
`QueueDepletionFill(qf=0.5)`, latency profile
`r47_maker_shioaji_p95_v2026-04-24` (210 / 210 ms).

### Results

| `spread_threshold_pts` | fills | PnL (pts) | PnL (NTD) | pnl/fill | winning days | Sharpe |
|-----------------------:|------:|----------:|----------:|---------:|-------------:|-------:|
| **5** (deployed) | 25 | −233.2 | **−2,332** | −9.3 | 0 | −2.98 |
| **7** | **9** | **+330.2** | **+3,302** | **+36.7** | **1** | **+2.88** |
| 10 | 5 | −27.4 | −274 | −5.5 | 0 | −3.13 |
| 15 | 1 | −2.0 | −20 | −2.0 | 0 | −2.90 |

### Reading

1. **There is a local maximum at `spread_threshold_pts=7`** that pulls
   31-day PnL back to positive (+3,302 NTD, Sharpe +2.88).
2. **The result is statistically fragile**: 9 fills / 31 days = 0.29
   fills/day, 1 winning day out of 31, +36.7 pnl/fill is an outlier vs
   the full sweep. One lucky day almost certainly dominates.
3. **The sweep is not monotone** — widening to 10 or 15 drops PnL back
   toward zero / slightly negative. This is not a smooth "wider spread
   → better PnL" curve; it looks like a narrow regime.
4. Under v2026-04-09 (sim-mode, 36/47 ms) the same strategy at
   spread=5 had 17 fills and +5,428 NTD (P0 comparison table). The
   local max at spread=7 in the 210 ms regime is plausibly recovering a
   small part of the simulated-latency optimism, not a structurally
   robust edge.

### Verdict

**A strategy-layer fix is not ruled out, but the sweep does NOT
establish a deploy-ready configuration**. To promote
`spread_threshold_pts=7` under the measured latency regime we would
need:

1. Larger sample — min 60 days (local CK has 31), ideally with OOS split
   that keeps the +3,302 on unseen data.
2. Stability-under-re-tuning: sweep the other C60Params
   (`inventory_skew_tenths`, `qi_skew_threshold`, QI layer off vs on)
   and confirm the positive region survives.
3. Day-level decomposition — identify whether the +3,302 is dominated
   by 1–2 outlier days (trivially kill-by-removal test).
4. Concurrent rerun with the canned 800 ms shioaji_p95 upper bound to
   confirm the sign is robust across the latency uncertainty band.

Until items 1–4 are completed, **+3,302 is a research artifact, not a
promotion candidate**. In particular it does NOT reinstate the
retracted +7,701 claim — both numbers are small-sample, different
configs, and only the newly measured profile reflects live tradability.

### Artifact

`scripts/sweep_r47_spread.py` — invokable for wider sweeps:

```bash
CLICKHOUSE_PASSWORD=... PYTHONPATH=. uv run python scripts/sweep_r47_spread.py
```

## Follow-up D — Direct live-API RTT probe (2026-04-24, n=300)

### Why

Previous 210/210 ms profile was derived from hft.orders status-transition
pairing (176 fast-cancel pairs ÷ 2), a method that implicitly assumes
submit_rtt ≈ cancel_rtt. It had no statistical CI and produced symmetric
values which turn out to be wrong.

User asked for a bulk probe "until statistically significant". We built
one and ran it.

### What we built

`scripts/latency/shioaji_rtt_bulk_probe.py` — safe probe that:
- Gets best bid/ask via snapshot
- Places 1-lot ROD LMT at best_bid − 300 pt (buy) or best_ask + 300 pt
  (sell) — far-from-market, zero fill risk
- Immediately waits for Shioaji callback to populate real `ordno`
- Cancels the order
- Records wall-time for each step

Per-cycle measurements:
- `place_order`: time from API call to broker's blocking return
- `submitted_ack`: time after that, for callback to populate valid ordno
- `cancel_order`: time for cancel API to return

### Execution

- Session: 2026-04-24 16:28 CST, TAIFEX night session (open 15:00)
- Symbol: **TMFE6** (small-mini TAIEX; 1-lot ≈ 390K NTD notional)
- n=300 cycles, 1 s sleep between, zero warmup
- Total elapsed: 403.9 s
- Errors: 0 / 0 / 0 across place/ack/cancel
- Residual open orders after probe: 0
- Positions opened: 0
- Fills aborted: 0

Pre-flight (n=3) first validated the pipeline after the initial TXFE6
variant tripped a broker-side daily quota (`99QB`, 50萬限額) — switching
to TMFE6 cleared it.

### Results

| Metric | P50 | P95 | P99 | max | Bootstrap P95 95% CI |
|--------|----:|----:|----:|----:|---------------------:|
| place_order wall-time | 27.4 ms | 92.7 ms | 185.4 ms | 327.7 ms | [77.6, 110.2] ms |
| submitted_ack (callback propagation) | 166.0 ms | 341.8 ms | 665.1 ms | 1,131 ms | [322.9, 409.3] ms |
| cancel_order wall-time | 20.3 ms | 58.7 ms | 83.8 ms | 270.6 ms | [51.3, 65.6] ms |
| **quote-activation** (place + submitted_ack per sample) | **201.8 ms** | **394.5 ms** | **694.3 ms** | **1,157 ms** | — |

### The big update — asymmetry matters

Directly measured live broker has **heavily asymmetric latency**:
quote-activation (395 ms P95) is ~6.7× slower than cancel (59 ms P95).
My earlier 210/210 derivation forced symmetry, destroying this asymmetry
in the most anti-maker way possible.

New canonical profile `r47_maker_shioaji_p95_v2026-04-24_measured`:
```yaml
submit_ack_latency_ms: 395.0
modify_ack_latency_ms: 395.0   # not directly measured; same bucket as submit
cancel_ack_latency_ms:  59.0
```

### Rerun compare_r47_latency under measured profile

| Regime | place / cancel | fills | PnL (NTD) | Sharpe |
|--------|:--:|:--:|:--:|:--:|
| no_latency baseline | 0 / 0 | 4,481 | +77,216 | +5.04 |
| v2026-04-09 sim | 36 / 47 | 17 | +5,428 | +2.87 |
| v2026-04-24 (derived symmetric) | 210 / 210 | 25 | **−2,332** | **−2.98** |
| **v2026-04-24_measured (direct)** | **395 / 59** | **39** | **+2,398** | **+2.80** |
| shioaji_p95 canned | 800 / 800 | 23 | −3,632 | −2.98 |

The symmetric 210/210 → −2,332 was **wrong direction**. The asymmetric
395/59 → **+2,398** (positive, 31 days, Sharpe +2.80, 1 winning day, 39
fills).

### Rerun spread_threshold sweep under measured profile

| `spread_threshold_pts` | fills | PnL (NTD) | Sharpe |
|-----------------------:|------:|----------:|-------:|
| **5** (deployed) | **39** | **+2,398** | **+2.80** |
| 7 | 7 | −228 | −3.17 |
| 10 | 4 | −80 | −3.70 |
| 15 | 1 | −20 | −2.90 |

The "local max at spread=7" claimed in Follow-up C was an artifact of
the wrong symmetric profile. Under the correct measured profile, the
**deployed configuration (spread=5) is already at the local max**;
widening the gate kills fills without improving PnL.

### Updated verdict — R47 credibility is NOT dead

**Previous (derived profile, WRONG)**: R47 dead under live RTT.

**Updated (measured profile, RIGHT)**: R47 at deployed config produces
**modest positive PnL under realistic live broker latency** — but the
evidence is weak, not a promotion candidate:

Still holds:
- The +7,701 cited from the 04-21 memo is INVALIDATED — it came from
  instant-RTT (place=0/cancel=0) which does not exist on the live broker
  path.
- 31 days, 1 winning day, 39 fills is a small sample. Sharpe 2.80 is
  noisy at that n.
- One or two outlier days probably carry most of the +2,398.

New finding:
- **The strategy survives realistic latency — it just survives at a
  much smaller scale than the instant-RTT fantasy suggested.**
- +2,398 NTD over 31 days ≈ +77 NTD/day. Not meaningful as a standalone
  alpha, but not negative either.
- **The prior conclusion "R47 is dead as deployment evidence" was too
  strong** — the strategy isn't dead; the claimed PnL was the wrong
  number.

### Next-step recommendations (updated)

- **Do not retract R47** — it has small positive survival under correct
  latency. But do retract the **+7,701 figure** specifically.
- Rerun on more days (remote has TMFE6 04-15..04-24, adds 8 days of the
  incident+post-fix window) to see if the signal is stable.
- Decompose the +2,398 per-day — identify whether 1-2 days carry it;
  if so, probe what regime those days share.
- Re-measure live latency during day session (this probe was night
  session — day session may have different RTT distribution due to
  liquidity / quote update cadence).
- Consider re-enabling live `HFT_ORDER_MODE=live` for a bounded canary
  at 1-lot to collect actual-PnL evidence under the fixed D1-D6 cancel
  path.

### Artifacts

- `scripts/latency/shioaji_rtt_bulk_probe.py` — probe script (safe, reusable)
- `outputs/shioaji_rtt_tmfe6_n300_20260424_1628.json` — raw n=300 samples + stats
- `config/research/latency_profiles.yaml` — new profile
  `r47_maker_shioaji_p95_v2026-04-24_measured`, old `v2026-04-24`
  marked SUPERSEDED

## Evidence Inventory

- `config/research/latency_profiles.yaml` — v2026-04-24 added, v2026-04-09
  marked deprecated.
- Remote CH `hft.orders` 2026-04-15..04-21 — 228 fills, 176 fast-cancel
  pairs used for RTT.
- Remote CH `hft.shadow_orders` 2026-04-23 19:37:20 ~ 2026-04-24 10:03:31 —
  2,846 intents, 1,421/1,425 BUY/SELL.
- `docs/incidents/2026-04-21-r47-backtest-live-divergence.md` — D1–D6 design.
- `docs/incidents/2026-04-21-deploy-validation.md` — post-fix validation
  targets (lifetime P95 < 1,500 ms, max < 5,000 ms).

## One-line answer to the original question

> After excluding the known deployment and execution bugs and after
> directly probing the live Shioaji API (n=300 at TMFE6, 2026-04-24 night
> session), R47's backtest **survives realistic latency at modestly
> positive PnL** (+2,398 NTD / 31 days / Sharpe +2.80 at deployed
> spread=5). The **+7,701 figure cited in the 04-21 memo is invalidated**
> — it measured instant-RTT fantasy, not live tradability. The correct
> live number is ~30× smaller but **not negative**. The strategy
> survives execution, the evidence is still small-sample (1 winning day
> out of 31, 39 fills), so it remains a **research-continue** candidate,
> **not a promotion** candidate. Promotion would need a canary with real
> live fills post-D1–D6 fix and more days of data.
