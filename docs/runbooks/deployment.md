# Deployment Runbook

## Overview

The HFT Platform uses a blue-green deployment model with Docker images tagged by git SHA. Deployments are triggered automatically on push to `main` after CI passes, or manually via the deploy script.

## Prerequisites

- Docker installed on deploy host
- SSH access to deploy host configured
- GHCR (GitHub Container Registry) credentials
- `docker-compose.yml` present at `/opt/hft-platform/` on deploy host

## Automated Deployment (GitHub Actions)

### Trigger

The CD pipeline (`.github/workflows/deploy.yml`) runs automatically when:
1. CI workflow completes successfully on `main`
2. Manual dispatch via GitHub Actions UI

### Required Secrets

| Secret | Purpose |
|--------|---------|
| `DEPLOY_HOST` | SSH hostname/IP of production server |
| `DEPLOY_USER` | SSH username for deployment |
| `DEPLOY_KEY` | SSH private key (base64 or raw) |
| `GITHUB_TOKEN` | Auto-provided; used for GHCR push |

### Flow

1. Build Docker image from current commit
2. Tag with git SHA and `latest`
3. Push to GHCR
4. SSH to deploy host, pull image, restart `hft-engine` service
5. Health check: verify `/metrics` endpoint returns HFT metrics
6. On failure: automatic rollback to previous container

## Manual Deployment

### Standard Deploy

```bash
# Deploy current HEAD
./scripts/deploy.sh

# Required env vars
export DEPLOY_HOST=prod.example.com
export DEPLOY_USER=hft
export DEPLOY_KEY=~/.ssh/hft_deploy_key
export GHCR_TOKEN=ghp_xxxxx
```

### Dry Run

```bash
# Build and verify without pushing or deploying
./scripts/deploy.sh --dry-run
```

### Rollback

```bash
# Rollback to a specific git SHA
./scripts/deploy.sh --rollback abc123def

# The image for that SHA must exist in GHCR
```

## Verification Steps

After deployment, verify:

1. **Health endpoint**: `curl -sf http://<host>:9090/metrics | grep hft_`
2. **Container status**: `docker compose ps` — `hft-engine` should show `Up (healthy)`
3. **Logs**: `docker compose logs -f hft-engine` — no crash loops or startup errors
4. **Metrics**: Check Grafana dashboards for:
   - `hft_ticks_processed_total` — increasing
   - `hft_risk_reject_total` — not spiking
   - `hft_gateway_dispatch_latency_ns` — within normal range

## Rollback Procedure

### Automatic (during deploy)

The deploy script automatically rolls back if health checks fail after 3 attempts.

### Manual Rollback

```bash
# Option 1: Redeploy a known-good SHA
./scripts/deploy.sh --rollback <known-good-sha>

# Option 2: Direct Docker rollback on the host
ssh hft@prod.example.com
cd /opt/hft-platform
docker compose up -d --no-deps hft-engine  # Uses previous image
```

### Emergency Rollback

If the system is in a critical state:

```bash
# SSH to host and stop the engine immediately
ssh hft@prod.example.com
cd /opt/hft-platform
docker compose stop hft-engine

# Verify no active orders are in flight
docker compose logs --tail=100 hft-engine | grep -i "order\|fill\|position"

# Restart with known-good image
docker pull ghcr.io/<repo>/hft-engine:<known-good-sha>
HFT_ENGINE_IMAGE=ghcr.io/<repo>/hft-engine:<known-good-sha> docker compose up -d hft-engine
```

## Troubleshooting

| Symptom | Likely Cause | Action |
|---------|-------------|--------|
| Health check fails | Service crash on startup | Check `docker compose logs hft-engine` |
| Image not found | GHCR push failed | Re-run deploy or check GHCR manually |
| SSH connection refused | Firewall or key issue | Verify SSH access independently |
| Metrics missing | Prometheus port not exposed | Check `docker-compose.yml` port mapping |
| High latency after deploy | Config regression | Compare `config/` with previous version |

## Pre-Deploy Checklist

- [ ] CI passes on `main` (lint, typecheck, unit tests, benchmarks)
- [ ] No open critical issues on the release
- [ ] Backup current config: `ssh host 'cp -r /opt/hft-platform/config /opt/hft-platform/config.bak'`
- [ ] Notify team of upcoming deployment
- [ ] Verify deploy host disk space: `ssh host 'df -h /opt/hft-platform'`
- [ ] Check market hours — prefer deploying outside trading hours (TSE: 09:00-13:30 UTC+8)
