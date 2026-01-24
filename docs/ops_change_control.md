# Ops Change Control

This document defines the minimum change approval process for infra and config.

## Scope
- Deployment manifests and Docker compose files
- `config/` changes that affect live trading
- Ops scripts in `ops/`

## Workflow
1) Create a short change note (what/why/risk/rollback).
2) Review by a second person (or automated check).
3) Apply in staging (or sim) first.
4) Verify metrics: latency, errors, message loss, reconnects.
5) Roll forward or rollback within 5 minutes if anomalies appear.

## Rollback
- Keep last known-good image tag in `HFT_IMAGE`.
- Preserve previous `.env.prod` as `.env.prod.bak`.
- Document rollback steps in the release note.
