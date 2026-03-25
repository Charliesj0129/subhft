# Reconnect Burn-In Validation Guide

## Purpose

Validate broker reconnection reliability before production go-live. The reconnect subsystem is critical for maintaining continuous market data feeds during network instability, broker maintenance windows, and transient failures. This burn-in procedure ensures reconnection logic is robust under real-world conditions.

## When to Run

- **Before first production deployment** — mandatory gate
- **After broker SDK upgrade** — SDK changes may alter reconnect behavior
- **Quarterly** — as part of operational readiness review
- **After reconnect code changes** — any modification to `feed_adapter/` reconnect logic

## Procedure

### 1. Environment Setup

```bash
# Ensure sim mode
export HFT_MODE=sim
export HFT_ORDER_MODE=sim

# Configure reconnect parameters (use production values)
export HFT_RECONNECT_HOURS="08:30-13:35"
export HFT_RECONNECT_COOLDOWN=60
export HFT_RECONNECT_BACKOFF_S=5
export HFT_RECONNECT_BACKOFF_MAX_S=120
export HFT_QUOTE_FLAP_THRESHOLD=5
export HFT_QUOTE_FLAP_WINDOW_S=60
export HFT_QUOTE_FLAP_COOLDOWN_S=300
```

### 2. Run System for 5 Trading Days

Start the platform in sim mode and let it run through 5 full trading sessions:

```bash
uv run hft run sim
```

During the burn-in period:
- Do not restart the system unless it crashes (document crashes separately)
- Monitor daily via Prometheus/Grafana dashboards
- Run the report script daily: `./scripts/reconnect-burn-in-report.sh`

### 3. Collect Daily Metrics

At the end of each trading day, record the metrics in the report template below.

## Metrics to Observe

| Metric | Description | Source |
|--------|-------------|--------|
| `feed_reconnect_total{result="ok"}` | Successful reconnections | Prometheus |
| `feed_reconnect_total{result="fail"}` | Failed reconnections | Prometheus |
| `feed_reconnect_timeout_total` | Reconnection attempts that timed out | Prometheus |
| Quote staleness duration | Time between last tick and reconnect completion | Grafana dashboard / logs |
| `storm_guard_state` | StormGuard state transitions during reconnects | Prometheus |
| `feed_gap_detected_total` | Feed gaps detected during burn-in | Prometheus |

### Quick Report Script

```bash
./scripts/reconnect-burn-in-report.sh [PROMETHEUS_URL]
```

Defaults to `http://localhost:9091`. Pass a custom URL if Prometheus is on a different host.

## Pass Criteria

All criteria must be met for the burn-in to pass:

| Criterion | Threshold | Rationale |
|-----------|-----------|-----------|
| Reconnect success rate | >= 99% | One failure per 100 reconnects is the maximum acceptable rate |
| P95 recovery time | <= 5 seconds | Must recover before StormGuard HALT threshold |
| Undetected feed gaps | 0 gaps > 30s | Any undetected gap risks stale data in trading decisions |
| Data loss during reconnect | Zero | WAL or direct recording must capture all events |
| Flap detection false positives | 0 | False flap detection would suppress valid reconnect attempts |

## Report Template

Fill in after each trading day:

| Day | Date | Reconnects (ok/fail) | Success Rate | P95 Recovery (s) | Feed Gaps > 30s | Notes |
|-----|------|---------------------|--------------|-------------------|-----------------|-------|
| 1   |      |                     |              |                   |                 |       |
| 2   |      |                     |              |                   |                 |       |
| 3   |      |                     |              |                   |                 |       |
| 4   |      |                     |              |                   |                 |       |
| 5   |      |                     |              |                   |                 |       |

**Overall Result**: PASS / FAIL

**Signed off by**: _______________

**Date**: _______________

## Troubleshooting

### High failure rate (< 99% success)

- Check broker API status and network connectivity
- Review `HFT_RECONNECT_BACKOFF_S` and `HFT_RECONNECT_BACKOFF_MAX_S` — backoff may be too aggressive
- Inspect logs for specific error patterns: `docker compose logs hft-engine | grep reconnect`

### Slow recovery time (P95 > 5s)

- Check if flap detection is throttling reconnects (`HFT_QUOTE_FLAP_*` settings)
- Verify broker API login latency is within normal range
- Consider reducing `HFT_RECONNECT_COOLDOWN` if cooldown is dominating recovery time

### Undetected feed gaps

- Verify `HFT_STORMGUARD_FEED_GAP_HALT_S` is set correctly (default: 30)
- Check quote staleness monitoring is active in `MarketDataService`
- Review whether gaps occur during expected maintenance windows

### Data loss during reconnect

- Verify WAL fallback is active (`HFT_RECORDER_MODE=wal_first`)
- Check `.wal/` directory for files corresponding to reconnect windows
- Review recorder queue depth metrics during reconnect events
