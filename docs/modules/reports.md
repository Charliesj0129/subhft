# reports — Daily Market Report Pipeline

> **Package**: `src/hft_platform/reports/`
> **Files**: 9

## Overview

Automated daily market report pipeline: collect data, extract facts, reason about patterns, compose narrative, and distribute via notifications.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `pipeline.py` | `ReportingPipeline` | Main report orchestrator |
| `collector.py` | `ReportCollector` | Data collection from ClickHouse |
| `composer.py` | `ReportComposer` | Report narrative composition |
| `distributor.py` | `ReportDistributor` | Report delivery (Telegram, etc.) |
| `fact_extractor.py` | `FactExtractor` | Pattern extraction from data |
| `reasoner.py` | `ReportReasoner` | Pattern reasoning and analysis |
| + 3 more | — | Rules: informed_flow, scenario_rules, support_resistance |

## Pipeline

```
ClickHouse → ReportCollector → FactExtractor → ReportReasoner → ReportComposer → ReportDistributor
```

## Report Sections

- PnL summary (realized + unrealized)
- Trade statistics (buys, sells, fills)
- Position status
- Reconciliation status
- Latency metrics (P95)
- Reconnect count
- StormGuard state
- Memory usage
