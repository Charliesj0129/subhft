# Escalation Matrix (WU-12)

Defines who is contacted, when, and how for each incident severity level.

## Severity x Role x Contact x Response Time

| Severity | Role | Contact Method | Response Time | Responsibility |
|---|---|---|---|---|
| **P0 - Critical** | On-call Engineer | Telegram + Phone | 5 min | Acknowledge, begin triage, activate kill switch if needed |
| **P0 - Critical** | Secondary On-call | Telegram + Phone | 10 min | Backup if primary unresponsive |
| **P0 - Critical** | Engineering Lead | Telegram + Phone | 15 min | Incident commander, coordinate response |
| **P0 - Critical** | CTO | Phone | 30 min | Executive decision-making, external communication |
| **P1 - High** | On-call Engineer | Telegram + Slack | 15 min | Acknowledge, begin investigation |
| **P1 - High** | Secondary On-call | Telegram | 20 min | Backup if primary unresponsive |
| **P1 - High** | Engineering Lead | Slack | 30 min | Review mitigation plan, approve risky changes |
| **P2 - Medium** | On-call Engineer | Slack | 30 min | Acknowledge, schedule investigation |
| **P2 - Medium** | Engineering Lead | Slack | 2 hours | Review if needed |
| **P3 - Low** | Assigned Engineer | Slack | Next business day | Investigate and resolve |

## Auto-Escalation Rules

These rules are enforced by AlertManager routing and monitoring tooling.

| Rule | Trigger | Action |
|---|---|---|
| **AE-1** | P0 not acknowledged within 5 min | Page secondary on-call via phone |
| **AE-2** | P0 not acknowledged within 15 min | Page engineering lead via phone |
| **AE-3** | P0 not mitigated within 30 min | Notify CTO, assemble war room |
| **AE-4** | P1 not acknowledged within 15 min | Page secondary on-call via Telegram |
| **AE-5** | P1 not mitigated within 1 hour | Escalate to engineering lead |
| **AE-6** | P2 not acknowledged within 2 hours | Escalate to engineering lead via Slack |
| **AE-7** | Any P0/P1 resolved | Auto-create post-incident review ticket within 24 hours |
| **AE-8** | Same alert fires 3x in 24 hours | Auto-escalate severity by one level |

## On-Call Rotation

| Week | Primary | Secondary |
|---|---|---|
| Odd weeks | Engineer A | Engineer B |
| Even weeks | Engineer B | Engineer A |

Update this table in the on-call management tool. The rotation above is a template; maintain the authoritative schedule externally.

## Contact Directory

> **NOTE**: Do not commit actual contact details. Maintain the authoritative directory in your team's secure channel or password manager.

| Role | Placeholder | Notes |
|---|---|---|
| On-call Engineer | Defined by rotation | Check on-call schedule tool |
| Engineering Lead | `@eng-lead` | Slack handle |
| CTO | `@cto` | Phone for P0 only |
| Infra/DevOps | `@infra-team` | Slack handle, for infrastructure issues |
| Broker Support | Broker hotline | For broker-side issues (Shioaji/Fubon) |

## Severity Upgrade Criteria

An incident MUST be upgraded if any of the following occur:

| From | To | Condition |
|---|---|---|
| P2 | P1 | Impact expands to multiple symbols or affects order execution |
| P1 | P0 | Financial loss confirmed or all trading capability lost |
| Any | P0 | Kill switch activated (manual or automatic) |
| Any | P0 | StormGuard enters HALT and cannot auto-recover within 5 min |
| P2 | P1 | Data loss confirmed (WAL not catching writes) |

## Severity Downgrade Criteria

| From | To | Condition |
|---|---|---|
| P0 | P1 | Immediate financial risk eliminated, trading resumed with limits |
| P1 | P2 | Root cause identified, mitigation in place, monitoring stable |
| P2 | P3 | No trading impact confirmed, fix scheduled |
