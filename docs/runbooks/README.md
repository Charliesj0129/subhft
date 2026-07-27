# Runbooks

## Workflow

| Workflow | Runbook |
|----------|---------|
| Alpha development (canonical, end-to-end) | [alpha-development-workflow.md](alpha-development-workflow.md) |
| **Production deploy (canonical — Deploy Laws D1–D10)** | [deployment.md](deployment.md) |
| Live-trading activation (`HFT_ORDER_MODE=live` red line) | [live-trading-activation-sop.md](live-trading-activation-sop.md) |
| Daily pre-open / post-close checks | [daily-ops-checklist.md](daily-ops-checklist.md) |

## Alert → Runbook

| Alert | Runbook |
|-------|---------|
| FeedGapCritical | [FeedGapCritical.md](FeedGapCritical.md) |
| StormGuardHalt | [StormGuardHalt.md](StormGuardHalt.md) |
| RecorderFailure | [RecorderFailure.md](RecorderFailure.md) |
| OrphanedFillDetected | [OrphanedFillDetected.md](OrphanedFillDetected.md) |
| TelegramAlertTriage | [TelegramAlertTriage.md](TelegramAlertTriage.md) |
