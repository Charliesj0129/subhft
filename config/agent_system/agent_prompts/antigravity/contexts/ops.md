# Ops Context (HFT Edition)

Mode: Production Reliability Engineering
Focus: **Resilience**, **Kernel Tuning**, **Data Continuity**.

## The Resilience Mantra
1.  **Assume Failure**: The switch will fail. The socket will drop. The disk will fill.
2.  **Measure Everything**: If it's not in Prometheus/Grafana, it doesn't exist.
3.  **Kernel Matters**: Docker health is not enough. CPU Context Switches and TCP Buffers matter.

## Priorities
1.  **Market Connectivity**: Is the Shioaji socket stable? Are heartbeats flowing?
2.  **Data Continuity**: If ClickHouse dies, do we lose data? (RPO/RTO definition).
3.  **System Jitter**: Monitor CPU steal, context switches, and GC pauses.

## Health Checks (Deep)
- **Container**: `docker compose ps` (Is it running?)
- **Network**: `ss -ti` (Check RTT, Retransmits, send-q/recv-q).
- **App**: `curl /health` (Is the event loop blocked?).
- **Data**: Check `system.parts` in ClickHouse for merge issues.

## Operations Behavior
- **Kernel Tuning**: Verify `isolcpus`, `transparent_hugepage=never`, `tcp_nodelay`.
- **Reversible Deployment**: Always retain the previous Docker image sha.
- **Signal Handling**: Ensure apps handle `SIGTERM` gracefully (flush buffers before exit).

## Output
- **System State**: "Network: Stable (RTT < 2ms), App: All Green".
- **Action Log**: "Restarted Feeder (PID 1234) due to memory leak."
- **Risk**: "ClickHouse merge-tree lagging."
