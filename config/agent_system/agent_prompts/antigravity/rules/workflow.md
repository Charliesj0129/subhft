# HFT Workflow

## Planning & Design
- Planner: Always assess risk. For complex or high-risk work, produce a plan first.
- Quant Architect: For strategy, execution, infra, or data-flow changes, write a Tech Spec (objective, risks, Darwin Gate baseline).

## Config & Ops Prep
- Config Curator: Own config edits (symbols.list and config files).
- Shioaji Ops: Run sync/preview/validate to confirm broker connectivity and subscriptions.

## Implementation & Performance
- Performance Engineer: Baseline -> profile -> optimize -> verify for hot paths.
- Perf Reviewer: Review LOB, normalizer, recorder performance-sensitive code.

## Verification & Fix
- Test Runner: Execute target test suite.
- Build Fix: Root-cause failures and re-run until green.

## Review & Release
- Code Reviewer: Correctness and regression review.
- Security Reviewer: Secrets and network access checks.
- Release Manager: Changelog, symbols status, rollback notes.

## Deployment & Monitoring
- Deployment Ops: Run the deployment flow.
- Data Flow Checker: Validate metrics and ClickHouse ingestion.

## Knowledge Management
- Doc Updater: Keep docs aligned with changes.
- Librarian: Record patterns/quirks/ADRs in brain/.
