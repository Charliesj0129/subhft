# Latency Baseline: System Internal vs Shioaji Simulation API (Research Realism Guard)

Date: 2026-02-26
Status: Active baseline (must be revisited after broker/network/runtime changes)
Scope: Research backtest assumptions, strategy parameterization, runtime realism guard

## 1. Purpose (Why this exists)

This document defines a **non-optimistic latency baseline** for this project.

It compares:
1. internal system stage latency (market data -> feature -> strategy -> gateway/risk)
2. real **Shioaji simulation account** API RTT (`place_order / update_order / cancel_order`)

Use this baseline when operating the research system to avoid over-optimistic assumptions.

## 2. Measurement Scope and Constraints

### 2.1 API measurement constraint (required)

Broker API latency measurements in this baseline were collected **only in Shioaji simulation mode**.
No live-order API RTT is included in this document.

### 2.2 Data sources / artifacts

- `outputs/perf_gate_latency_snapshot.clean.json`
- `outputs/latency_stage_probe_custom_nonorder.json`
- `outputs/shioaji_sim_rtt_probe_ab_20.json`

### 2.3 Important interpretation limits

1. Shioaji simulation RTT is not equal to real market RTT.
2. `latency_test.py`-style E2E benchmarks do not include broker API RTT.
3. Internal stage numbers below are steady-state microbench/probe numbers (not full queueing under load).

## 3. Measured Internal System Latency (steady-state, simple-path baseline)

### 3.1 Stage-level latency (microseconds)

| Stage | Metric | Mean | Source |
|---|---:|---:|---|
| Shioaji callback dispatch | `dispatch_tick_cb` | `0.611 us` | perf gate |
| MarketData callback parse | `_on_shioaji_event` | `0.904 us` | perf gate |
| Normalizer + LOB | combined | `27.844 us` | custom probe |
| FeatureEngine (Python backend) | `process_lob_update` | `8.358 us` | perf gate |
| EventBus publish | `publish_nowait` | `1.362 us` | custom probe |
| EventBus consume | `consume_batch` (generic) | `1.042 us` | custom probe |
| StrategyRunner (noop, metrics on) | `process_event` | `4.882 us` | perf gate |
| GatewayService typed path | `_process_envelope` | `9.602 us` | perf gate |

### 3.2 Approx. internal pipeline lower-bound (non-overlapping estimate)

For a simple typed path (where `GatewayService` benchmark already includes gateway+risk path work), the measured internal compute lower-bound is:

- **~54.61 us/event**

Computation uses:
- callback dispatch + callback parse + normalizer/LOB + feature + event bus publish/consume + strategy + gateway typed path

This is a **lower-bound** for research realism. Real strategies (multiple subscribed strategies, richer logic, more metrics, queueing) will be slower.

## 4. Measured Shioaji Simulation API RTT (real sim account, 20 iterations)

Probe conditions:
- Product: `TAIFEX TXFD6`
- Flow: `place_order -> update_order(price) -> cancel_order`
- Mode: `simulation=True`
- Iterations: `20` (plus warmup)

### 4.1 Wrapper path (`ShioajiClient.*`) RTT (milliseconds)

| Operation | Mean | P50 | P95 | P99 | Notes |
|---|---:|---:|---:|---:|---|
| `place_order` API RTT | `30.786 ms` | `30.644` | `35.166` | `37.424` | from `_record_api_latency` |
| `update_order` API RTT | `38.357 ms` | `38.192` | `42.355` | `49.654` | price modify |
| `cancel_order` API RTT | `39.530 ms` | `38.009` | `46.146` | `46.422` | cancel |

### 4.2 Wrapper overhead vs direct API (`place_order` split)

Measured on the same simulation account (`TXFD6`) with 20 iterations:

- `ShioajiClient.place_order` wrapper wall mean: **30.936 ms**
- wrapper internal recorded API RTT mean: **30.786 ms**
- direct `api.place_order(contract, order)` mean: **29.806 ms**
- direct order-build mean (`FuturesOrder` object build): **0.058 ms**
- wrapper-only overhead mean: **0.150 ms** (~**0.48%** of wrapper wall time)

### 4.3 Ratio: API RTT vs internal compute (why research gets over-optimistic)

Using the internal lower-bound (~`54.61 us`) vs wrapper API RTT means:

- `place_order`: **~564x** slower than internal compute
- `update_order`: **~702x** slower than internal compute
- `cancel_order`: **~724x** slower than internal compute

**Implication**: For Shioaji-driven strategies, external API RTT dominates; local micro-optimizations do matter for determinism and throughput, but they do not justify zero-latency trading assumptions in backtests.

## 5. Mandatory Research / Backtest Latency Guardrails (Must Follow)

### 5.1 Hard rules (Do not violate)

1. **Do not assume zero-latency order submission / cancel / modify.**
2. **Do not use a single latency value for all order operations.** `place`, `update`, and `cancel` must be modeled separately.
3. **Do not use mean-only API RTT** for production promotion decisions. Use at least `P95`; use `P99` for stress tests.
4. **Do not assume sim RTT == live RTT.** Apply a live uplift factor (see below) or explicitly justify why not.
5. **Do not promote alpha/strategy claims that depend on edge half-life shorter than broker RTT.**

### 5.2 Recommended backtest parameter baselines (Shioaji/TW market, sim-derived)

Use these as defaults unless you have stronger empirical evidence.

#### A. Local pipeline (system internal) delay before broker call

| Parameter | Lower-bound measured | Recommended default | Conservative | Stress |
|---|---:|---:|---:|---:|
| `local_decision_pipeline_latency_us` | `~55 us` | `250 us` | `500 us` | `1000 us` |

Notes:
- `~55 us` is a simple-path lower-bound (noop strategy-class benchmark conditions).
- Use `250~500 us` for realistic research defaults unless strategy path is proven simpler.

#### B. Broker API RTT (simulation-derived)

| Parameter | Sim P50 | Sim P95 | Sim P99 | Recommended default | Conservative | Stress |
|---|---:|---:|---:|---:|---:|---:|
| `submit_ack_latency_ms` (`place_order`) | `30.64` | `35.17` | `37.42` | `36` | `42` | `56` |
| `modify_ack_latency_ms` (`update_order`) | `38.19` | `42.36` | `49.65` | `43` | `52` | `75` |
| `cancel_ack_latency_ms` (`cancel_order`) | `38.01` | `46.15` | `46.42` | `47` | `56` | `70` |

How these were chosen:
- `default`: near sim `P95`, rounded up
- `conservative`: ~`P95 * 1.2`, rounded
- `stress`: ~`P99 * 1.5`, rounded

#### C. Live uplift factor (until live RTT probes exist)

Use a **live uplift multiplier** on top of sim-derived API latencies:

- `live_uplift_factor = 1.2` (minimum)
- `live_uplift_factor = 1.5` (safer default for promotion/risk review)

Example:
- `submit_ack_latency_ms_live = 36 * 1.5 = 54 ms` (promotion stress profile)

### 5.3 Strategy design constraints (to avoid over-optimistic alpha claims)

1. **Aggressive/taker strategies**:
   - If expected edge half-life is comparable to `place_order` P95 (~35 ms sim) or lower, treat results as highly optimistic unless proven via shadow/live evidence.

2. **Quote/cancel/replace strategies**:
   - If profitability depends on sub-`50 ms` cancel/modify reaction, backtests must use at least conservative/stress cancel & modify RTT.

3. **Feature/decision micro-optimizations**:
   - Improvements of `5~20 us` are useful, but they do **not** justify ignoring `30~50 ms` API RTT in Shioaji mode.

## 6. Required Recording in Research Artifacts (Governance)

Every promoted experiment/backtest report should record:

1. `feature_set_id`
2. `feature_profile_id` (if applicable)
3. `latency_profile_id` (e.g., `sim_p95_v2026-02-26`, `sim_stress_v2026-02-26`)
4. `local_decision_pipeline_latency_us`
5. `submit_ack_latency_ms`
6. `modify_ack_latency_ms`
7. `cancel_ack_latency_ms`
8. `live_uplift_factor` (if used)

If missing, the experiment should be considered **non-promotion-ready**.

## 7. Recalibration Policy

Re-run this probe set when any of the following changes:

1. Broker SDK version / Shioaji package version changes
2. Network environment changes (ISP/VPS/host location)
3. Order adapter / gateway execution path changes
4. Major feature/risk/gateway instrumentation changes
5. At least once per month for active deployment environments

## 8. Quick Summary (TL;DR)

- Internal compute lower-bound: **~55 us**
- Shioaji sim API RTT: **~31 ms place**, **~38 ms update**, **~40 ms cancel**
- API RTT is **~560x–720x** larger than internal compute
- Research/backtests must model `place/update/cancel` separately and use **P95/P99**, not mean-only
- Use conservative/stress latency presets to avoid over-optimistic strategy promotion
