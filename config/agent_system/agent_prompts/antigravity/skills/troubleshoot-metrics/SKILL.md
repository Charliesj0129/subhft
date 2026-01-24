---
name: troubleshoot-metrics
description: System Sensor. active diagnostics for the HFT platform. Returns structured JSON health data. Use whenever you need to know the state of the infrastructure (Docker, Redis, Network).
tools: Bash
---

# System Sensor (troubleshoot-metrics)

**Mode**: Active Diagnostics
**Output**: JSON Health Report

## Usage
Run the health check script to get a snapshot of system status.

```bash
python3 skills/troubleshoot-metrics/check_health.py
```

## Interpretation
*   **overall_status**: `ok` | `degraded` | `critical`
*   **details**: Specifics on Docker containers, Redis connection, etc.

## Remediation Protocol
*   If `docker.status` == `down`: Run `make start` or `docker compose up -d`.
*   If `redis.status` == `down`: Check `redis` container logs.
