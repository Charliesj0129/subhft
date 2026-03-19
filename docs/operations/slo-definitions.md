# SLO Definitions — TXFD6 Production

Service Level Objectives for the TXFD6 + queue_imbalance production deployment.

## SLO Summary

| SLO | Target | Measurement Window | Metric |
|-----|--------|-------------------|--------|
| Availability | 99.5% | Rolling 30 days | Uptime during market hours |
| Order-to-Fill Latency | P95 < 50ms | Per trading session | `order_to_fill_latency_ms` |
| Data Integrity | 99.9% | Rolling 30 days | Recorder success ratio |

## SLO-1: Availability (99.5%)

**Definition**: The platform is "available" when all of the following are true during TWSE/TAIFEX market hours (08:45-13:45 TST, weekdays):
- Feed events are being received (`rate(feed_events_total[30s]) > 0`)
- Strategy is processing events (`strategy_latency_ns` updating)
- Order path is functional (no circuit breaker tripped)
- StormGuard is NOT in HALT state due to infrastructure failure

**Exclusions**:
- Planned maintenance windows (announced 24h in advance)
- Exchange outages or halt events
- StormGuard HALT triggered by risk limits (this is correct behavior, not an outage)

**Measurement**:
```promql
# Availability = fraction of 30s windows with feed events during market hours
avg_over_time(
  (rate(feed_events_total[30s]) > bool 0)[30d:30s]
)
```

**Error budget**: 99.5% over 30 days = ~3.6 hours of allowed downtime per month during market hours (~21 trading days x 5 hours = 105 hours).

**Alerting**:
- Warning: availability drops below 99.7% over trailing 7 days
- Critical: availability drops below 99.5% over trailing 7 days

## SLO-2: Order-to-Fill Latency (P95 < 50ms)

**Definition**: Time from `OrderCommand` dispatch to `FillEvent` receipt, measured end-to-end including broker API round-trip.

**Context**: Shioaji simulation API RTT is on the order of tens of milliseconds (see `docs/architecture/latency-baseline-shioaji-sim-vs-system.md`). The 50ms P95 target accounts for broker API latency.

**Measurement**:
```promql
histogram_quantile(0.95,
  sum(rate(order_to_fill_latency_ms_bucket[1h])) by (le)
)
```

**Breakdown targets**:
| Stage | P95 Target |
|-------|-----------|
| Strategy decision -> OrderCommand | < 1ms |
| Risk validation | < 0.5ms |
| Order dispatch to broker API call | < 2ms |
| Broker API round-trip (Shioaji) | < 45ms |
| Fill event normalization | < 1ms |

**Alerting**:
- Warning: P95 > 40ms sustained for 5 minutes
- Critical: P95 > 50ms sustained for 2 minutes (SLO breach)

## SLO-3: Data Integrity (99.9%)

**Definition**: Fraction of market data events and execution events that are successfully persisted to ClickHouse (directly or via WAL replay).

**Measurement**:
```promql
# Success ratio over 24h
(
  sum(increase(recorder_insert_batches_total{result=~"success_no_retry|success_after_retry"}[24h]))
  /
  sum(increase(recorder_insert_batches_total[24h]))
)
```

**Includes**:
- Market data (tick, bidask, LOB stats)
- Order events (intents, commands, fills)
- Position snapshots

**Exclusions**:
- Events intentionally dropped by `put_nowait()` overflow (these are counted separately as `recorder_queue_dropped_total`)
- Events during planned maintenance windows

**Error budget**: 99.9% = at most 0.1% of batches may fail permanently. Over a typical trading day with ~10,000 recorder batches, at most 10 batches may be permanently lost.

**Alerting**:
- Warning: `RecorderInsertFailedRatioHigh` (>0.5% over 24h)
- Critical: `RecorderFailure` (any write failure in 5m window)

## SLO Review Process

1. **Weekly**: Review SLO dashboards in Grafana. Flag any SLO at risk of breach.
2. **Monthly**: Calculate 30-day SLO compliance. Update error budget burn rate.
3. **Quarterly**: Review SLO targets. Tighten targets if consistently exceeded by large margin.

## Incident Classification

| Severity | Criteria | Response Time |
|----------|----------|---------------|
| P1 (Critical) | Any SLO breached, StormGuard HALT (infra), data loss | Immediate (< 5 min) |
| P2 (High) | SLO at risk (>50% error budget burned), degraded performance | < 30 min |
| P3 (Medium) | Warning alerts firing, minor metric anomalies | < 2 hours |
| P4 (Low) | Informational, optimization opportunities | Next business day |

## Related Documents

- `docs/operations/production-launch-checklist.md` — launch procedure
- `docs/operations/incident-response-protocol.md` — incident handling
- `docs/architecture/latency-baseline-shioaji-sim-vs-system.md` — latency baseline
- `config/monitoring/alerts/rules.yaml` — Prometheus alert rules
- `config/monitoring/alerts/alertmanager.prod.example.yml` — alert routing
