# R47 Exploration Summary + Research Roadmap (2026-04-24)

> Companion to `docs/incidents/2026-04-24-r47-backtest-credibility-audit.md`.
> That doc is about **credibility** (does the backtest evidence hold).
> This doc is about **promotion** (what research work converts the
> now-understood evidence into a deploy-ready claim).

## 1. Journey — what we found, in order

Nine layers of discovery, each shifting the answer.

| # | Layer | What we did | What it told us |
|---|-------|-------------|-----------------|
| 1 | **Frame** | Decomposed credibility into C1 (decision parity), C2 (execution parity), C3 (PnL parity = C1×C2) | Where to focus per-layer instead of arguing about PnL directly |
| 2 | **C1 spot check** | 2,846 shadow intents post-fix, BUY/SELL 1,421/1,425 | C1 "partial pass" — no counter-evidence of divergence, but also no formal replay diff |
| 3 | **Derived RTT (WRONG)** | 176 fast-cancel pairs from remote `hft.orders` 04-15..04-21 ÷ 2 = 210/210 ms | Wrong answer in the right direction — gave a symmetric profile |
| 4 | **Wiring gap** | 28 alpha impls declare `manifest.latency_profile`; zero consume it. `_gate_c.py:175` instantiates `MakerEngine` without `latency_profile=` | Profile YAML was metadata-only — backtest infrastructure wasn't actually using it |
| 5 | **Wiring patch + first PnL run** | Wired `_gate_c.py` via `resolve_profile` + bridge to `LatencyProfile`. Ran `scripts/compare_r47_latency.py` | Under derived 210/210: **R47 = −2,332 NTD / Sharpe −2.98**. Flag raised: "R47 dead" |
| 6 | **Shioaji skill audit** | `platform never passes timeout=0` → 100% blocking. Non-blocking mode exists and is 12× faster at caller return. | Added env-gated non-blocking default. Doesn't change exchange activation latency; improves throughput + D3 multi-inflight |
| 7 | **Spread sweep (WRONG profile)** | `spread_threshold_pts` ∈ {5, 7, 10, 15} under 210/210 | "spread=7 +3,302 local max" — statistically weak; later shown to be artifact of wrong profile |
| 8 | **DB source reconciliation** | Local CK has 31 days TMFD6 complete; remote has 7 days + newer TMFE6. Incident day 04-21 was TMFE6 post contract roll | Clarified that my backtests used TMFD6 (pre-roll deployed contract), derivation used remote (post-roll incident data) |
| 9 | **Direct live API probe (n=300)** | Built `scripts/latency/shioaji_rtt_bulk_probe.py`, ran TMFE6 night session, zero errors / residuals | **True live RTT: place 395 ms / cancel 59 ms P95 — 6.7× asymmetric**. Rerun R47 under measured profile: **+2,398 NTD / Sharpe +2.80**. Credibility answer FLIPPED from "dead" to "alive but weak". |

## 2. Tools — reusable artifacts

All durable going forward; none are one-shots.

| Tool | Purpose | How to rerun |
|------|---------|-------------|
| `scripts/latency/shioaji_rtt_bulk_probe.py` | Safe bulk RTT probe for any futures symbol. Far-from-market + immediate cancel + fill circuit-breaker + bootstrap CIs | `uv run python scripts/latency/shioaji_rtt_bulk_probe.py --mode real --iters 300 --symbol TMFE6 --offset-ticks 300 --out outputs/...json` (env: SHIOAJI_API_KEY + SECRET + CA_CERT_PATH→SHIOAJI_CA_PATH + CA_PASSWORD→SHIOAJI_CA_PASSWORD) |
| `scripts/compare_r47_latency.py` | R47 MakerEngine PnL across 5 latency regimes | `CLICKHOUSE_PASSWORD=... PYTHONPATH=. uv run python scripts/compare_r47_latency.py` |
| `scripts/sweep_r47_spread.py` | `spread_threshold_pts` sweep under any named profile | Edit `_PROFILE_NAME`, rerun same as compare |
| `src/hft_platform/alpha/_gate_c.py` (patched) | MakerEngine now receives `LatencyProfile` resolved from `manifest.latency_profile`. **Unlocks** per-alpha latency-aware Gate C | Used automatically by Gate C runs |
| `src/hft_platform/feed_adapter/shioaji/order_gateway.py` (patched) | Non-blocking mode available via `HFT_SHIOAJI_NONBLOCKING=1`; default preserves blocking | `HFT_SHIOAJI_NONBLOCKING=1 uv run hft run live` |
| `config/research/latency_profiles.yaml` | Canonical profile store. New: `r47_maker_shioaji_p95_v2026-04-24_measured` | Read via `hft_platform.alpha.latency_profiles.resolve_profile(name)` |
| `outputs/shioaji_rtt_tmfe6_n300_20260424_1628.json` | Raw 300-sample probe output with all timing arrays + bootstrap CIs | Source of the canonical profile |

## 3. Results — key numbers

### Measured live broker RTT (TMFE6, night session, 2026-04-24)

```
place_order wall-time:    P50 27.4ms   P95 92.7ms   P99 185.4ms
submitted_ack callback:   P50 166ms    P95 342ms    P99 665ms
cancel_order wall-time:   P50 20.3ms   P95 58.7ms   P99 83.8ms
quote-activation (place+ack per sample): P50 202ms  P95 395ms  P99 694ms
```

### R47 PnL under each regime (C60, TMFD6, 31 days, spread=5)

```
no_latency baseline:       fills 4481  PnL +77,216  Sharpe +5.04  (the +7,701 fantasy)
v2026-04-09 sim:           fills   17  PnL  +5,428  Sharpe +2.87
v2026-04-24 derived 210:   fills   25  PnL  −2,332  Sharpe −2.98  (WRONG)
v2026-04-24_measured:      fills   39  PnL  +2,398  Sharpe +2.80  (CORRECT)
shioaji_p95 canned 800:    fills   23  PnL  −3,632  Sharpe −2.98
```

### Spread sweep (under correct measured profile)

```
spread_thr=5 (deployed):   fills 39  PnL +2,398  Sharpe +2.80  ← local max
spread_thr=7:              fills  7  PnL   −228  Sharpe −3.17
spread_thr=10:             fills  4  PnL    −80  Sharpe −3.70
spread_thr=15:             fills  1  PnL    −20  Sharpe −2.90
```

### One-line bottom line

> +7,701 is invalidated. True live-RTT PnL is +2,398 / 31 days /
> Sharpe +2.80 — **modestly positive, not promotion-ready**. Asymmetric
> broker latency (place 395 / cancel 59 ms) is favorable to makers,
> which is why the strategy survives at all.

## 4. Research roadmap — what's needed to promote

Three buckets by what blocks promotion.

### Bucket A — Sample & stability (the cheapest wins)

**A1. Day-level PnL decomposition** *(immediate)*
The +2,398 over 31 days with only 1 winning day is almost certainly
dominated by 1–2 outlier days. If the result collapses when those are
removed, the "survives latency" story collapses too.

- **Action**: modify `compare_r47_latency.py` to emit per-day PnL under
  the measured profile; sort by contribution; mark days contributing
  > 50% of total PnL.
- **Decision**: if ≥ 80% of +2,398 comes from < 3 days → R47 is regime-
  specific, not a general edge. If contribution is diffuse (no single
  day > 20% of total) → the signal is real at small scale.
- **ETA**: < 1 hour.

**A2. Extend to remote TMFE6 days (04-15..04-24)**
31 local days + 8 remote days (including incident + post-fix window) =
39 days of coverage. Crucially includes the live incident and post-fix
validation windows.

- **Action**: modify `ClickHouseSource` caller to point at remote or run
  the compare against remote directly. Unify TMFD6 (pre-04-15) + TMFE6
  (post-04-15) via contract-roll handling.
- **Expected yield**: statistical significance on daily Sharpe; ±15%
  CI shrink.
- **ETA**: 2-3 hours.

**A3. Day session latency re-measure**
Current probe is night session only. Day session (08:45–13:45) has 4×
the tick volume; broker RTT distribution may differ significantly.

- **Action**: run the same `shioaji_rtt_bulk_probe.py` n=300 during day
  session. Compare distributions.
- **Decision lever**: if day P95 ≫ night P95, R47's backtest positive
  PnL may be night-session-only — need time-of-day gating.
- **ETA**: 20 min probe + analysis = 1 hour.

### Bucket B — Execution model realism (medium cost, high leverage)

**B1. Measure `update_order` (modify) RTT**
Current profile uses 395 ms for both submit and modify. Shioaji's
`api.update_order(trade, price=…)` is a different RPC; unmeasured.
If modify is much faster than place, R47 can modify in place instead
of cancel+replace — saving ~454 ms per quote update.

- **Action**: extend `shioaji_rtt_bulk_probe.py` with `update_order`
  measurement, add `modify_ack_samples_us` to output, rerun n=300.
- **Yield**: if modify is close to cancel (~60 ms), a rewrite of R47
  to use modify for price updates could replicate what the platform
  does today with cancel+new in roughly 1/7 the latency.
- **ETA**: 2 hours probe + analysis.

**B2. Cancel exchange-ack callback latency**
We measured cancel_order (broker return), not when the order actually
disappears from the orderbook. There's an analogous callback to
`submitted_ack` for cancels. The real "order gone from book" time is
cancel_order + cancel_ack, not just cancel_order.

- **Action**: extend probe with cancel callback wait (similar to
  `_wait_for_ordno` but waiting for Status.Cancelled on the trade).
- **Expected**: cancel_ack adds 50–200 ms. Still much less than place's
  342 ms.
- **ETA**: 2 hours.

**B3. Non-blocking mode empirical test**
`HFT_SHIOAJI_NONBLOCKING=1` patch is done. Need to measure actual PnL
impact — does the 12× caller-return speedup translate to better fills
in live?

- **Action**: requires canary (live fills). Without canary, we can
  simulate: construct a backtest variant where multiple orders are
  in-flight per side, see if PnL increases.
- **ETA**: 4 hours modeling + canary (separate gate).

### Bucket C — Alpha signal research (expensive, high variance)

**C1. Layer decomposition — which R47 layer actually contributes?**
R47 has D1 (Permutation Entropy), D2 (Queue Survival), D3 (MFG
Inventory), plus the QI layer and the spread gate. Under realistic
latency, we don't know which of these is contributing the +2,398 vs
which is dead weight or actively costing.

- **Action**: ablation sweep with C60Params — run {all on, QI off, D1
  only, D2 only, D3 only, all off (naive maker)} under measured profile.
- **Decision**: if "all off" (naive constant-spread maker) also produces
  small positive PnL → R47's signal adds nothing. If specific layers
  contribute, that's the real alpha.
- **ETA**: 1 day for full sweep + analysis.

**C2. Max_pos sweep under correct latency**
Memory: TXFD6 shows V-shape where max_pos=3 wins; TMFD6 deployed at
max_pos=1. Under correct latency, does the V-shape still hold?

- **Action**: sweep max_pos ∈ {1, 2, 3, 5} for both TMFD6 and TXFE6.
- **Yield**: if max_pos=3 on larger instruments gives 3-5× PnL scale
  without destroying Sharpe, this is direct capacity multiplier.
- **ETA**: 4 hours.

**C3. Cross-instrument replication**
Run the same R47 logic on TXFE6 (big TAIEX, 20× notional of TMFE6).
If the signal survives, PnL scales with notional — same Sharpe × 20×
size = promotable.

- **Action**: adapt config for TXFE6, run compare + sweep under measured
  profile. Careful: quota limit for TXFE6 may prevent live canary.
- **ETA**: 1 day.

**C4. Regime identification — can we pick profitable days in advance?**
Memory mentions TMFD6 spread regime shift Jan-Feb (28-68 pt) vs Mar
(3 pt). If R47's profitable days correlate with a specific regime
signature observable pre-session, we can gate it on.

- **Action**: extract per-day features (opening spread, realized vol,
  range, traded volume, ATR, regime indicator from microstructure).
  Correlate with R47 daily PnL from A1 output. Train simple classifier.
- **Yield**: if classifier picks 30% of days that contain 80% of PnL,
  effective daily PnL goes from +77 NTD to ~+250 NTD on traded days.
- **ETA**: 2-3 days with proper OOS.

**C5. Queue-position-aware cancel (C72 prototype)**
Currently R47 cancels on spread threshold breach. C72 in the research
tree has queue-position-aware logic — cancel only when our place in
queue degrades. Under asymmetric latency (cancel 59 ms, place 395 ms),
this should reduce wasted latency on "still good" queue positions.

- **Action**: port C72's queue-position logic into a C60 variant, test.
- **ETA**: 2-3 days.

### Bucket D — Execution evidence (the only path to actual promotion)

**D1. Canary at 1-lot**
Ultimate validation: actual live fills post-D1–D6 fix. Required for
C3 (PnL parity) — without live evidence, backtest numbers are only
"consistent with not being ruled out".

- **Preconditions**: Bucket A done (confirms the +2,398 is not a single-
  day outlier). Bucket C1 done (know which layers contribute).
- **Action**: `HFT_ORDER_MODE=live`, limit to 1-lot max_pos=1, hard
  auto-halt on lifetime P95 > 1,500 ms, run for 5 consecutive days
  minimum.
- **Target**: at least 15 trips, at least 2 winning days, PnL sign
  consistent with measured backtest at ±50%.
- **Blast radius**: bounded by 1-lot × day → max loss ≈ margin × 5 days.
- **ETA**: 5 days calendar time.

## 5. Prioritization

My recommendation, by decreasing yield-per-hour:

| Priority | Item | Cost | Yield |
|:--------:|------|:----:|-------|
| **P0** | A1 day-level decomposition | 1 h | Either confirms signal is diffuse (continue) or kills R47 cleanly (stop) |
| **P0** | A2 remote TMFE6 extension | 3 h | +8 days incl. incident window — bigger sample for the same analysis |
| **P1** | C1 layer ablation | 1 d | Identifies what signal is actually adding value |
| **P1** | B1 update_order measurement | 2 h | Small cost, potentially unlocks major design change (modify-in-place) |
| **P1** | A3 day session probe | 1 h | Confirms night-session number generalizes |
| **P2** | C2 max_pos sweep | 4 h | Capacity multiplier if V-shape holds |
| **P2** | C3 cross-instrument | 1 d | PnL scale-up if signal transfers to TXFE6 |
| **P2** | B2 cancel exchange-ack | 2 h | Completes the execution model |
| **P3** | C4 regime identification | 2-3 d | Big yield but high variance, deep research |
| **P3** | C5 queue-position cancel | 2-3 d | Architectural win under asymmetric latency |
| **Gate** | D1 canary | 5 d | Only after P0+P1 confirm; actual promotion evidence |

Nothing in this list should be skipped as "too small". A1 alone could
kill R47 in an afternoon and save weeks of wasted research.

## 6. Cost-benefit summary

Known investments to reach R47 promotion readiness:

```
Minimum research path (A1 + A2 + C1 + B1 + A3): ~2 days
+ Optional gates (C2 + C3 + B2):               ~3 days
+ Canary (D1):                                  5 days calendar
= Total ~ 2 weeks to promotion decision (yes/no)
```

Compare against:
- **Writing off R47 now**: 0 days, but loses the modest +2,398 signal
  that survived latency (rare; most alphas don't)
- **Deploying without more evidence**: ~5 days canary, higher risk of
  real loss if the +2,398 is outlier-driven

## 7. Environmental notes for next runs

- Current HFT_ORDER_MODE: `sim` (post-04-23 fix window)
- TAIFEX day session: 08:45-13:45 CST / night: 15:00-05:00 CST
- Current account daily trading quota: 50 萬 NTD (hit on TXFE6, not on TMFE6)
- Remote ClickHouse: `charl@100.91.176.126` (password `changeme` for CK user)
- Local ClickHouse: TMFD6 complete 2026-01-26 through 2026-04-15 (31 days)
- CA cert: `./certs/Sinopac.pfx` with password in `.env:CA_PASSWORD`
