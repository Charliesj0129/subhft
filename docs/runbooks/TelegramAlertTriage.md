# Telegram Alert Triage

Use this runbook when Telegram receives Alertmanager notifications that are noisy,
duplicated, or unclear. The goal is to separate live trading risk from alerting
surface problems before re-arming the platform.

## Scope

This covers alerts delivered through Alertmanager to the engine webhook:

```text
Alertmanager -> hft-engine:8081/webhook/alertmanager -> Telegram
```

Primary alerts in this incident class:

| Alert | Meaning | First classification |
|-------|---------|----------------------|
| `PlatformReduceOnlyActive` | Opening orders are blocked by platform degrade state. | Live trading control-plane risk |
| `ManualRearmRequired` | Operator review is required before normal trading resumes. | Live trading control-plane risk |
| `FeatureQualityFlagsSpike` | Feature pipeline emitted partial or degraded quality flags. | Usually input/feed quality risk |
| `AlphaSignalSilent` | A strategy stopped emitting alpha signals after it had emitted before. | Strategy liveness risk |
| `HostDiskSpaceCritical` | Node exporter host filesystem signal crossed threshold. | Host operations risk |

## Safety Rules

- Do not paste full container environments into tickets or chat.
- Do not paste full `docker compose config` output into public channels.
- Redact token, secret, password, api key, auth, bearer, and broker credentials.
- Do not run manual rearm until the triggering condition is understood and current
  metrics confirm it is clear.

## Fast Triage

Run from the production host. If using SSH, keep command output targeted and avoid
environment dumps.

```bash
docker compose ps
docker compose logs --tail=200 hft-engine
curl -fsS http://localhost:9091/api/v1/alerts
curl -fsS http://localhost:9091/api/v1/targets
curl -fsS http://localhost:9093/api/v2/alerts
curl -fsS http://localhost:8081/metrics >/tmp/hft-engine.metrics
```

Check the specific metrics used by the autonomy alerts:

```bash
grep -E '^(platform_reduce_only_active|manual_rearm_required|autonomy_transitions_total)' /tmp/hft-engine.metrics
```

The metric names must not be duplicated. If output contains names such as
`platform_reduce_only_activeplatform_reduce_only_active`, the partial metrics
fallback is corrupting the scrape surface and Prometheus may be evaluating stale
or missing data.

## Reduce-Only and Manual Rearm

Use this path for `PlatformReduceOnlyActive` or `ManualRearmRequired`.

1. Inspect current persisted autonomy state:

   ```bash
   uv run hft ops autonomy-status
   ```

2. Inspect the runtime state file directly only when the CLI is unavailable:

   ```bash
   sed -n '1,160p' outputs/production_rollout/autonomy/runtime_state.json
   ```

3. Identify the reason. Common reasons:

   | Reason | Check |
   |--------|-------|
   | `clickhouse_unhealthy` | Recorder health, ClickHouse health, WAL backlog, recent recorder failures |
   | `feed_reconnect_unhealthy` | Feed reconnect metrics and Shioaji quote logs |
   | `queue_depth_exceeded` | Queue metrics and engine logs |
   | `reconciliation_drift` | Reconciliation run output and broker/platform position state |

4. Confirm the condition is currently clear. For `clickhouse_unhealthy`, check:

   ```bash
   uv run hft recorder status
   grep -E '^(recorder_failures_total|recorder_wal_writes_total|recorder_insert_batches_total)' /tmp/hft-engine.metrics
   ```

5. Rearm only after the reason is clear and a human has accepted the state:

   ```bash
   uv run hft ops rearm-platform
   ```

6. Confirm the live engine consumed the rearm request:

   ```bash
   curl -fsS http://localhost:8081/metrics >/tmp/hft-engine.metrics
   grep -E '^(platform_reduce_only_active|manual_rearm_required)' /tmp/hft-engine.metrics
   uv run hft ops autonomy-status
   ```

Expected result after successful rearm:

```text
platform_reduce_only_active 0.0
manual_rearm_required{scope="platform"} 0.0
```

## Alertmanager Retries and Duplicate Telegram Messages

Duplicate Telegram messages can be expected when Alertmanager cannot deliver to
`hft-engine:8081` during an engine restart. Confirm whether this is delivery
retry behavior before treating it as a new incident:

```bash
docker compose logs --tail=200 alertmanager
docker compose logs --tail=200 hft-engine | grep -E 'alertmanager|webhook|telegram'
```

If Alertmanager is retrying while the engine is unavailable, the duplicate
delivery is expected. Continue investigating the underlying alert state instead
of silencing the receiver immediately.

## Node Exporter and Host Alerts

`HostDiskSpaceCritical` and other host alerts depend on Prometheus scraping the
Compose service name:

```bash
curl -fsS http://localhost:9091/api/v1/targets | grep -E 'node|node-exporter'
```

Expected target:

```text
node-exporter:9100
```

If Prometheus reports `localhost:9100`, it is scraping inside the Prometheus
container and not the host exporter service. Fix the Prometheus target and confirm
the node exporter shares a Docker network with Prometheus in every active Compose
overlay.

## Shioaji Session Exhaustion

When Telegram alerts coincide with broker login or quote degradation, inspect
Shioaji logs before rearming:

```bash
docker compose logs --tail=500 hft-engine | grep -Ei 'shioaji|too many connections|login|quote_pool'
grep -E '^(hft_quote_pool_degraded|hft_quote_pool_degraded_fraction|shioaji_api_errors_total)' /tmp/hft-engine.metrics
```

Classify findings:

| Evidence | Classification |
|----------|----------------|
| `Too Many Connections` | Broker/session exhaustion. Reduce reconnect pressure before restarting repeatedly. |
| `Login retries exhausted` | Broker login path failed. Do not rearm until feed and order paths are healthy. |
| `quote_pool_degraded` | Quote pool health risk. Confirm subscriptions recover before normal trading. |

Avoid repeated container restarts while broker sessions are exhausted; this can
increase connection churn and make the condition worse.

## Feature and Alpha Alerts

For `FeatureQualityFlagsSpike`:

```bash
grep -E '^feature_quality_flags_total' /tmp/hft-engine.metrics
docker compose logs --tail=500 hft-engine | grep -Ei 'feature|partial|normalize|feed'
```

Treat partial feature flags as secondary if feed or Shioaji health is already
degraded. Fix the feed/session layer first.

For `AlphaSignalSilent`:

```bash
grep -E '^alpha_last_signal_ts' /tmp/hft-engine.metrics
grep -E '^strategy_intents_total' /tmp/hft-engine.metrics
```

The alert rule has an `alpha_last_signal_ts > 0` gate. A value of `0` means a
strategy has not emitted yet and should not fire the alert. If it does fire with
`0`, investigate metric naming, stale Prometheus samples, or rule reload state.

## When to Silence

Silence only after the live risk is understood:

- Silence during planned maintenance or expected engine restart windows.
- Do not silence `PlatformReduceOnlyActive` or `ManualRearmRequired` while the
  state still requires operator action.
- Prefer short silences with a reason and owner.

## Evidence to Save

For every Telegram alert incident, save:

- Alert name, first seen time, and resolved time.
- `uv run hft ops autonomy-status` output with secrets absent.
- Relevant metric snippets from `/tmp/hft-engine.metrics`.
- Relevant redacted engine log lines.
- Whether the alert was live risk, stale state, scrape/config issue, or delivery
  retry noise.
