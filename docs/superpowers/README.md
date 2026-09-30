# Superpowers: Design Specs and Implementation Plans

Dated design specs and implementation plans (2026-03 onward). They are the
historical record of why a subsystem looks the way it does. The current
behavior is described in `docs/architecture/` and `docs/modules/`; if a plan
here disagrees with the code, the code wins.

Files are named `YYYY-MM-DD-<topic>.md`. Do not rename or move them: code,
`AGENTS.md` and `.agent/` reference these paths.

## Specs

| File | Title |
|---|---|
| [specs/2026-03-22-agent-teams-design.md](specs/2026-03-22-agent-teams-design.md) | Agent Teams Design — HFT Platform |
| [specs/2026-03-23-production-rollout-design.md](specs/2026-03-23-production-rollout-design.md) | Production Rollout Design — HFT Platform |
| [specs/2026-03-23-smart-symbols-refresh-design.md](specs/2026-03-23-smart-symbols-refresh-design.md) | Smart Symbols Refresh Design — HFT Platform |
| [specs/2026-03-24-live-feasibility-validation-design.md](specs/2026-03-24-live-feasibility-validation-design.md) | Live Feasibility Validation Design Spec |
| [specs/2026-03-25-clickhouse-backup-safety-design.md](specs/2026-03-25-clickhouse-backup-safety-design.md) | ClickHouse Backup & Data Safety Layer — Design Spec |
| [specs/2026-03-25-cross-day-position-continuity-design.md](specs/2026-03-25-cross-day-position-continuity-design.md) | Cross-Day Position State Continuity — Design Spec |
| [specs/2026-03-25-operational-readiness-assessment.md](specs/2026-03-25-operational-readiness-assessment.md) | Operational Readiness Assessment: Independent Quant Team Foundation |
| [specs/2026-03-27-infrastructure-hardening-roadmap.md](specs/2026-03-27-infrastructure-hardening-roadmap.md) | Infrastructure Hardening Roadmap |
| [specs/2026-03-27-pipeline-determinism-async-defense-design.md](specs/2026-03-27-pipeline-determinism-async-defense-design.md) | Pipeline Determinism & Async Defense — Design Spec |
| [specs/2026-03-28-market-analysis-report-service-design.md](specs/2026-03-28-market-analysis-report-service-design.md) | Market Analysis Report Service — Design Spec |
| [specs/2026-03-28-phase1-inf1-inf3-design.md](specs/2026-03-28-phase1-inf1-inf3-design.md) | Phase 1 Infrastructure: INF-1 Trade Classification + INF-3 Multi-Window Aggregation |
| [specs/2026-03-28-remote-host-daily-governance-design.md](specs/2026-03-28-remote-host-daily-governance-design.md) | Remote Host Daily Governance — Design Spec |
| [specs/2026-03-28-shadow-trading-fix-design.md](specs/2026-03-28-shadow-trading-fix-design.md) | Shadow Trading Fix + Hardening (v2) |
| [specs/2026-03-29-multi-instrument-infrastructure-design.md](specs/2026-03-29-multi-instrument-infrastructure-design.md) | Multi-Instrument Infrastructure Design |
| [specs/2026-03-29-telegram-analysis-product-design.md](specs/2026-03-29-telegram-analysis-product-design.md) | Telegram Analysis Product — Design Spec |
| [specs/2026-03-29-telegram-bot-service-design.md](specs/2026-03-29-telegram-bot-service-design.md) | Telegram Bot Service Design |
| [specs/2026-03-29-telegram-report-optimization-design.md](specs/2026-03-29-telegram-report-optimization-design.md) | Telegram Report Optimization — Three-Layer Analysis Architecture |
| [specs/2026-03-30-infrastructure-audit-design.md](specs/2026-03-30-infrastructure-audit-design.md) | Infrastructure Audit & Upgrade Roadmap — Design Spec |
| [specs/2026-03-30-quote-connection-pool-design.md](specs/2026-03-30-quote-connection-pool-design.md) | Quote Connection Pool Design Spec |
| [specs/2026-03-30-tmf-cbs-live-design.md](specs/2026-03-30-tmf-cbs-live-design.md) | TMF CBS Live Design |
| [specs/2026-03-30-txo-options-infrastructure-design.md](specs/2026-03-30-txo-options-infrastructure-design.md) | TXO Options Trading Infrastructure — Design Spec |
| [specs/2026-04-02-alpha-research-team-redesign.md](specs/2026-04-02-alpha-research-team-redesign.md) | Alpha Research Agent Team Redesign |
| [specs/2026-04-05-backtest-risk-engine-design.md](specs/2026-04-05-backtest-risk-engine-design.md) | Backtest Risk Engine Integration |
| [specs/2026-04-05-canary-live-metrics-writer-design.md](specs/2026-04-05-canary-live-metrics-writer-design.md) | Canary Live Metrics Writer |
| [specs/2026-04-05-e2e-pipeline-tests-design.md](specs/2026-04-05-e2e-pipeline-tests-design.md) | E2E Pipeline Tests — 7-Plane Complete Chain Verification |
| [specs/2026-04-06-rejection-consumer-design.md](specs/2026-04-06-rejection-consumer-design.md) | Rejection Queue Consumer Design |
| [specs/2026-04-07-telegram-llm-report-design.md](specs/2026-04-07-telegram-llm-report-design.md) | Telegram LLM Decision Report — Design Spec |
| [specs/2026-04-08-per-connection-isolation-design.md](specs/2026-04-08-per-connection-isolation-design.md) | Per-Connection Isolation for QuoteConnectionPool |
| [specs/2026-04-15-standardized-backtest-design.md](specs/2026-04-15-standardized-backtest-design.md) | Standardized Backtest Engine — Design Spec |
| [specs/2026-04-15-track-c-autonomy-foundation-design.md](specs/2026-04-15-track-c-autonomy-foundation-design.md) | Track C: Autonomy Foundation Design |
| [specs/2026-04-16-unified-backtest-framework-design.md](specs/2026-04-16-unified-backtest-framework-design.md) | Unified hftbacktest Backtest Framework for TAIFEX |
| [specs/2026-04-17-alpha-research-autonomous-loop-design.md](specs/2026-04-17-alpha-research-autonomous-loop-design.md) | Alpha Research Autonomous Maker/Taker Loop — Design |
| [specs/2026-04-17-project-convergence-cleanup-design.md](specs/2026-04-17-project-convergence-cleanup-design.md) | Project Convergence Cleanup — Design Spec |
| [specs/2026-04-17-vulture-report.md](specs/2026-04-17-vulture-report.md) | 2026-04-17-vulture-report |
| [specs/2026-04-19-agent-config-best-practices-alignment-design.md](specs/2026-04-19-agent-config-best-practices-alignment-design.md) | Agent Config Best-Practices Alignment — Design |
| [specs/2026-04-19-agent-config-hygiene-report.md](specs/2026-04-19-agent-config-hygiene-report.md) | Agent Config Hygiene Report (2026-04-19) |
| [specs/2026-05-12-agents-md-onboarding-design.md](specs/2026-05-12-agents-md-onboarding-design.md) | AGENTS.md Onboarding Rewrite - Design |
| [specs/2026-05-19-regime-conditioned-t1-revalidation-design.md](specs/2026-05-19-regime-conditioned-t1-revalidation-design.md) | Regime-Conditioned T1 Revalidation — Design Spec |
| [specs/2026-05-19-t1a-zero-event-diagnostic-design.md](specs/2026-05-19-t1a-zero-event-diagnostic-design.md) | T1-A Zero-Event Diagnostic — Design Spec |
| [specs/2026-06-12-alpha-candidate-loop-v1-maker-gate-design.md](specs/2026-06-12-alpha-candidate-loop-v1-maker-gate-design.md) | Alpha Candidate Loop v1.0 + Maker-Aware Cost Gate — Implementation Design |
| [specs/2026-06-14-candidate-loop-v1.1-governor-design.md](specs/2026-06-14-candidate-loop-v1.1-governor-design.md) | Candidate Loop v1.1 — Governor Design |
| [specs/2026-06-14-research-refinement-route-correctness-design.md](specs/2026-06-14-research-refinement-route-correctness-design.md) | Research Refinement Route Correctness Design |
| [specs/2026-07-10-agent-system-institutionalization-design.md](specs/2026-07-10-agent-system-institutionalization-design.md) | Agent System Institutionalization — 15-Point Design |
| [specs/2026-07-13-shioaji-156-quote-only-design.md](specs/2026-07-13-shioaji-156-quote-only-design.md) | Shioaji 1.5.6 Quote-Only Candidate Design |
| [specs/2026-07-14-agent-system-v3-design.md](specs/2026-07-14-agent-system-v3-design.md) | Agent System v3 Design — 分層演進(Layered Evolution) |

## Plans

| File | Title |
|---|---|
| [plans/2026-03-22-agent-teams-implementation.md](plans/2026-03-22-agent-teams-implementation.md) | Agent Teams Implementation Plan |
| [plans/2026-03-23-production-phase1-implementation.md](plans/2026-03-23-production-phase1-implementation.md) | Phase 1: Solo-Operator Automation & Hardening — Implementation Plan |
| [plans/2026-03-24-production-phase2-shadow-trading.md](plans/2026-03-24-production-phase2-shadow-trading.md) | Phase 2: Shadow Trading — Implementation Plan |
| [plans/2026-03-25-clickhouse-backup-safety.md](plans/2026-03-25-clickhouse-backup-safety.md) | ClickHouse Backup & Data Safety Layer — Implementation Plan |
| [plans/2026-03-25-cross-day-position-continuity.md](plans/2026-03-25-cross-day-position-continuity.md) | Cross-Day Position State Continuity — Implementation Plan |
| [plans/2026-03-25-futures-tca-implementation.md](plans/2026-03-25-futures-tca-implementation.md) | Futures TCA Implementation Plan |
| [plans/2026-03-25-live-feasibility-validation.md](plans/2026-03-25-live-feasibility-validation.md) | Live Feasibility Validation Implementation Plan |
| [plans/2026-03-25-p0-operational-readiness.md](plans/2026-03-25-p0-operational-readiness.md) | P0 Operational Readiness Implementation Plan |
| [plans/2026-03-25-p1-safety-resilience.md](plans/2026-03-25-p1-safety-resilience.md) | P1-A + P1-E: Solo Operator Safety + Resilience Docs |
| [plans/2026-03-25-p1-tca-pnl-attribution.md](plans/2026-03-25-p1-tca-pnl-attribution.md) | P1-B+C: TCA Pipeline + PnL Attribution Implementation Plan |
| [plans/2026-03-27-infrastructure-hardening.md](plans/2026-03-27-infrastructure-hardening.md) | Infrastructure Hardening Implementation Plan |
| [plans/2026-03-27-pipeline-determinism-async-defense.md](plans/2026-03-27-pipeline-determinism-async-defense.md) | Pipeline Determinism & Async Defense — Implementation Plan |
| [plans/2026-03-28-market-analysis-report-service.md](plans/2026-03-28-market-analysis-report-service.md) | Market Analysis Report Service — Implementation Plan |
| [plans/2026-03-28-phase1-inf1-inf3.md](plans/2026-03-28-phase1-inf1-inf3.md) | Phase 1: INF-1 Trade Classification + INF-3 Multi-Window Aggregation |
| [plans/2026-03-28-remote-host-daily-governance.md](plans/2026-03-28-remote-host-daily-governance.md) | Remote Host Daily Governance Implementation Plan |
| [plans/2026-03-28-shadow-trading-fix.md](plans/2026-03-28-shadow-trading-fix.md) | Shadow Trading Fix + Hardening Implementation Plan |
| [plans/2026-03-29-multi-instrument-phase1-foundation.md](plans/2026-03-29-multi-instrument-phase1-foundation.md) | Multi-Instrument Phase 1: Foundation — Implementation Plan |
| [plans/2026-03-29-telegram-analysis-product-phase1.md](plans/2026-03-29-telegram-analysis-product-phase1.md) | Telegram Analysis Product — Phase 1 Implementation Plan |
| [plans/2026-03-29-telegram-bot-service.md](plans/2026-03-29-telegram-bot-service.md) | Telegram Bot Service Implementation Plan |
| [plans/2026-03-29-telegram-report-optimization.md](plans/2026-03-29-telegram-report-optimization.md) | Telegram Report Optimization — Implementation Plan |
| [plans/2026-03-30-infra-audit-p0-critical.md](plans/2026-03-30-infra-audit-p0-critical.md) | Infrastructure Audit P0 — Fix Broken Things |
| [plans/2026-03-30-infra-audit-p1-high.md](plans/2026-03-30-infra-audit-p1-high.md) | Infrastructure Audit P1 — Close Key Gaps |
| [plans/2026-03-30-infra-audit-p2-medium.md](plans/2026-03-30-infra-audit-p2-medium.md) | Infrastructure Audit P2 — Structural Improvements |
| [plans/2026-03-30-quote-connection-pool.md](plans/2026-03-30-quote-connection-pool.md) | QuoteConnectionPool Implementation Plan |
| [plans/2026-03-30-tmf-cbs-rollout-blockers.md](plans/2026-03-30-tmf-cbs-rollout-blockers.md) | TMF CBS Rollout Blockers Implementation Plan |
| [plans/2026-03-30-txo-phase1-data-pricing.md](plans/2026-03-30-txo-phase1-data-pricing.md) | TXO Phase 1: Data Foundation + Pricing Core — Implementation Plan |
| [plans/2026-04-02-alpha-research-team-redesign.md](plans/2026-04-02-alpha-research-team-redesign.md) | Alpha Research Agent Team Redesign — Implementation Plan |
| [plans/2026-04-05-backtest-risk-engine.md](plans/2026-04-05-backtest-risk-engine.md) | Backtest Risk Engine Implementation Plan |
| [plans/2026-04-05-canary-live-metrics.md](plans/2026-04-05-canary-live-metrics.md) | Canary Live Metrics Writer Implementation Plan |
| [plans/2026-04-05-e2e-pipeline-tests.md](plans/2026-04-05-e2e-pipeline-tests.md) | E2E Pipeline Tests Implementation Plan |
| [plans/2026-04-07-telegram-llm-report-phase1.md](plans/2026-04-07-telegram-llm-report-phase1.md) | Telegram LLM Decision Report Phase 1 Implementation Plan |
| [plans/2026-04-08-per-connection-isolation.md](plans/2026-04-08-per-connection-isolation.md) | Per-Connection Isolation Implementation Plan |
| [plans/2026-04-14-orphan-recovery-halt-resilience.md](plans/2026-04-14-orphan-recovery-halt-resilience.md) | Orphan Recovery Halt Resilience Implementation Plan |
| [plans/2026-04-15-multi-strategy-position-isolation.md](plans/2026-04-15-multi-strategy-position-isolation.md) | Multi-Strategy Position Isolation & Manual Order Coexistence |
| [plans/2026-04-15-standardized-backtest.md](plans/2026-04-15-standardized-backtest.md) | Standardized Backtest Engine — Implementation Plan |
| [plans/2026-04-16-planA-calibration-research.md](plans/2026-04-16-planA-calibration-research.md) | Plan A: Calibration Research Implementation Plan |
| [plans/2026-04-16-planB-ck-streaming-adapter.md](plans/2026-04-16-planB-ck-streaming-adapter.md) | Plan B: ClickHouse Streaming Adapter Implementation Plan |
| [plans/2026-04-16-planC-engine-unification.md](plans/2026-04-16-planC-engine-unification.md) | Plan C: Engine Unification Implementation Plan |
| [plans/2026-04-16-track-c-autonomy-foundation.md](plans/2026-04-16-track-c-autonomy-foundation.md) | Track C: Autonomy Foundation Implementation Plan |
| [plans/2026-04-17-alpha-research-autonomous-loop.md](plans/2026-04-17-alpha-research-autonomous-loop.md) | Alpha Research Autonomous Maker/Taker Loop Implementation Plan |
| [plans/2026-04-17-project-convergence-cleanup.md](plans/2026-04-17-project-convergence-cleanup.md) | Project Convergence Cleanup Implementation Plan |
| [plans/2026-04-19-agent-config-best-practices-alignment.md](plans/2026-04-19-agent-config-best-practices-alignment.md) | Agent Config Best-Practices Alignment Implementation Plan |
| [plans/2026-05-03-slice-a-promotion-gate-hardening.md](plans/2026-05-03-slice-a-promotion-gate-hardening.md) | Slice A — Promotion Gate Hardening Implementation Plan |
| [plans/2026-05-04-slice-c-replay-parity-gate.md](plans/2026-05-04-slice-c-replay-parity-gate.md) | Slice C — Replay-Diff Parity Gate Implementation Plan |
| [plans/2026-05-05-slice-b-maker-realism.md](plans/2026-05-05-slice-b-maker-realism.md) | Slice B — Maker Realism (per-slice plan) |
| [plans/2026-05-05-slice-d-alpha-factory.md](plans/2026-05-05-slice-d-alpha-factory.md) | Slice D — Alpha Factory MVP (kill ledger + screener + cluster + DSL) |
| [plans/2026-05-12-agents-md-onboarding.md](plans/2026-05-12-agents-md-onboarding.md) | AGENTS.md Onboarding Rewrite Implementation Plan |
| [plans/2026-05-19-regime-conditioned-t1-revalidation.md](plans/2026-05-19-regime-conditioned-t1-revalidation.md) | Regime-Conditioned T1 Revalidation Implementation Plan |
| [plans/2026-05-19-t1a-zero-event-diagnostic.md](plans/2026-05-19-t1a-zero-event-diagnostic.md) | T1-A Zero-Event Diagnostic Implementation Plan |
| [plans/2026-06-14-candidate-loop-v1.1-governor.md](plans/2026-06-14-candidate-loop-v1.1-governor.md) | Candidate Loop v1.1 Governor Implementation Plan |
| [plans/2026-06-14-research-refinement-route-correctness.md](plans/2026-06-14-research-refinement-route-correctness.md) | Research Refinement Route Correctness Implementation Plan |
| [plans/2026-07-13-shioaji-156-quote-only.md](plans/2026-07-13-shioaji-156-quote-only.md) | Shioaji 1.5.6 Quote-Only Implementation Plan |
