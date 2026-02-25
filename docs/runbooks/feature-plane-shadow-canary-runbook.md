# Feature Plane Shadow / Canary Runbook (Prototype)

Date: 2026-02-24  
Status: Prototype runbook (supports current `FeatureEngine` Python/Rust shadow parity scaffolding)

## 1. Scope

This runbook covers rollout and incident handling for the Feature Plane (`LOBEngine -> FeatureEngine`) during:
1. shadow parity validation
2. canary enablement
3. reset/gap/schema mismatch incidents

Current runtime support (prototype):
1. feature-plane update/latency/quality metrics
2. shadow parity check/mismatch metrics names registered
3. `MarketDataService` sampled shadow compare path (primary vs shadow `FeatureEngine`)

## 2. Key Environment Flags

### Primary feature engine
- `HFT_FEATURE_ENGINE_ENABLED=1`
- `HFT_FEATURE_ENGINE_BACKEND=python|rust`
- `HFT_FEATURE_ENGINE_EMIT_EVENTS=1|0`

### Shadow parity compare
- `HFT_FEATURE_SHADOW_PARITY=1`
- `HFT_FEATURE_SHADOW_BACKEND=python|rust`
- `HFT_FEATURE_SHADOW_SAMPLE_EVERY=<N>`
- `HFT_FEATURE_SHADOW_WARN_EVERY=<N>`
- `HFT_FEATURE_SHADOW_ABS_TOL=<float>`

### Feature observability sampling
- `HFT_FEATURE_METRICS_SAMPLE_EVERY=<N>`
- `HFT_FEATURE_LATENCY_SAMPLE_EVERY=<N>`

## 3. Metrics to Watch

### Core runtime
- `feature_plane_updates_total{result,feature_set}`
- `feature_plane_latency_ns`
- `feature_quality_flags_total{flag}`

### Shadow parity
- `feature_shadow_parity_checks_total{feature_set,result}`
- `feature_shadow_parity_mismatch_total{feature_set,feature_id}`

### Related pipeline safety (existing)
- `raw_queue_depth`
- `raw_queue_dropped_total`
- `pipeline_latency_ns{stage=...}`

## 4. Rollout Procedure (Recommended)

## Phase A: Shadow (No Trading Behavior Change)

1. Enable primary FeatureEngine with current stable backend (usually `python`)
2. Enable shadow parity compare with alternate backend (e.g. `rust`)
3. Start with sparse sampling:
   - `HFT_FEATURE_SHADOW_SAMPLE_EVERY=256` (or higher in busy markets)
4. Observe for at least one session segment (open, mid, close if possible)

Success criteria (prototype recommendation):
1. `feature_shadow_parity_mismatch_total` remains `0` for promoted feature set
2. `feature_quality_flags_total{flag=out_of_order|gap|partial}` does not spike unexpectedly
3. no measurable regression in market-data loop latency / raw queue depth

## Phase B: Canary (Selected Strategy/Symbols)

1. Enable feature-consuming strategy on a small symbol subset
2. Keep legacy strategy-computed feature path available (A/B or shadow compute in strategy if needed)
3. Monitor strategy decisions, reject reasons, and order outcomes

Canary rollback triggers:
1. sustained parity mismatches in promoted feature IDs
2. strategy decision divergence unexplained by known data-granularity limits
3. feature-plane latency causing loop lag or queue growth

## 5. Incident Playbooks

## 5.1 Gap / Reset Storm

Symptoms:
1. `feature_quality_flags_total{flag=gap}` rising quickly
2. `feature_quality_flags_total{flag=state_reset}` repeated bursts
3. sudden strategy feature values dropping to warmup/default-like values

Immediate actions:
1. confirm feed gap / reconnect events (`feed_reconnect_total`, `feed_resubscribe_total`)
2. reduce strategy dependence on feature plane (disable feature-consuming strategy or fallback to legacy local compute path if available)
3. keep `FeatureEngine` enabled only if quality flags are expected and downstream logic handles warmup correctly

Follow-up:
1. inspect symbol-level gaps, timestamps, and reconnect sequence
2. verify reset semantics are correct (session boundary vs unexpected reconnect)

## 5.2 Out-of-Order / Timestamp Disorder

Symptoms:
1. `feature_quality_flags_total{flag=out_of_order}` rising
2. parity mismatches concentrated in stateful features (OFI/EMA)

Immediate actions:
1. verify source timestamp quality and normalization order
2. compare primary/shadow backend values on affected symbol
3. temporarily increase shadow sample rate for diagnosis

## 5.3 Feature Schema Mismatch (`feature_set_id` / version drift)

Symptoms:
1. strategy sees unexpected `feature_set_id`
2. feature consumer cannot find expected feature IDs
3. shadow parity checks tagged with different feature sets

Immediate actions:
1. stop canary / disable feature-consuming strategy
2. verify deployed config and strategy expectations (`feature_set_id`, schema version)
3. roll back to previous feature set or strategy bundle so IDs match

Prevention:
1. deploy feature-set changes with explicit version bump
2. keep strategy feature subscriptions version-aware

## 6. Recommended Canary Criteria (Initial)

These are operational criteria, not hard-coded enforcement (yet).

Promote canary only if all are true:
1. `feature_shadow_parity_mismatch_total == 0` for promoted feature IDs across representative session samples
2. no feature-plane induced queue growth (`raw_queue_depth`, `pipeline_latency_ns`) beyond baseline tolerance
3. strategy behavior parity or expected bounded divergence is documented
4. feature quality flags remain within expected rates for market conditions

## 7. Rollback Procedure

Fast rollback order:
1. disable feature-consuming strategies (or switch to legacy local feature compute)
2. disable shadow parity if it contributes overhead during incident debugging
3. set `HFT_FEATURE_ENGINE_ENABLED=0` if needed for full rollback
4. restart services with known-good config

Post-incident:
1. export metrics and logs around incident window
2. record root cause (data quality, kernel parity, schema mismatch, rollout config)
3. update feature spec and parity tests before retry

