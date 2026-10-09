# Seven-plane runtime audit

Read after an incident or near-miss, before a go-live window, or quarterly
(pair with `make reliability-monthly-pack`). Sweep every plane, not just the one
that failed; persistence and observability cause silent data loss. For each
check, read the source and show the evidence. Record findings in one atomic
commit or report, plane by plane.

For each plane check **instrumentation**, **fail-safety**, and **backpressure**.

| Plane | Check | Where |
|---|---|---|
| Control | loop-lag metric emitted; every bounded queue has a depth gauge; each service task supervised; HALT blocks new orders and allows cancels | `services/system.py` (`_supervise`), `observability/metrics.py`, `risk/storm_guard.py` |
| Market data | drop-rate burst detection on the raw queue; subscription accounting matches actual; reconnect backoff and flap detection; feed gap triggers STORM as designed (a gap alone cannot HALT) | `services/market_data.py`, `feed_adapter/subscription_state.py`, `HFT_RECONNECT_BACKOFF_S`, `HFT_QUOTE_FLAP_*`, `HFT_STORMGUARD_FEED_GAP_STORM_S` |
| Feature | schema version and feature count match the registry; warmup guard; Rust/Python parity | `feature/registry.py`, `feature/engine.py`, `feature/parity.py` |
| Decision | RiskFeedback complete (`side` present; pending counters drain on HALT); strategy sees positions before the first fill after recovery; no pending-exposure leak on reject or timeout | `risk/engine.py`, `strategy/runner.py` |
| Execution | OrderCommand preserves intent fields; rejected orders feed back to risk; checkpoints written on schedule; positions reconciled at boot | `order/adapter.py`, `execution/checkpoint.py`, `execution/startup_recon.py` |
| Persistence | ClickHouse failure routes to WAL; batched timestamps parse; DLQ files tracked with a metric | `recorder/writer.py`, `recorder/worker.py`, `make wal-dlq-status` |
| Observability | every queue and stage has latency and depth; alert rules cover every CRITICAL path; health endpoint returns 200 | `observability/metrics.py`, `config/monitoring/alerts/`, `make pre-market-check` |

Automated pass first: `make pre-market-check`, `make check`, `make test`, `make drill-ck-down`.
