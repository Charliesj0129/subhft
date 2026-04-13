# reports — Daily Market Report Pipeline

> **Package**: `src/hft_platform/reports/`
> **Files**: 17 (core pipeline + rules + LLM layer)
> **Runtime Plane**: Cold (CLI-invoked or bot-triggered, not in trading loop)

## Overview

Automated daily market analysis pipeline: collect data from ClickHouse → extract facts → reason about patterns → compose narrative → distribute via Telegram. Two report modes:

1. **Rule-based** (`build_report`): Deterministic fact extraction + rule reasoning. Fast, no external API calls.
2. **Hybrid LLM** (`build_hybrid_report_async`): Rule-based facts + LLM dossier + LLM reasoning + composition. Uses OpenRouter API. Falls back to rule-based on LLM failure.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `pipeline.py` | `build_report`, `build_hybrid_report_async`, `resolve_trading_date`, `HybridReportResult` | Main orchestrator. Date resolution (day/night session logic). CLI entry point |
| `collector.py` | `DataCollector` | ClickHouse queries → `SessionData` aggregate. Day/night time filters. 5-day lookback for cross-day context |
| `facts.py` | `extract_all` | Pattern extraction from `SessionData` → `FactResult` (flow, segments, volume profile, price levels) |
| `reasoner.py` | `ReportReasoner`, `LevelReasoner` | Rule-based analysis: informed flow detection, scenario matching, support/resistance levels |
| `composer.py` | `ReportComposer` | Assembles `ComposedReport` from facts + reasoning. HTML formatting with Telegram-safe output |
| `distributor.py` | `ReportDistributor` | Delivery via `NotificationDispatcher`. Multi-part message pacing |
| `models.py` | `ComposedReport`, `MessagePart`, `SessionData`, `FactResult` | Data structures for the pipeline |
| `heatmap.py` | `render_heatmap` | Flow heatmap image generation (PNG bytes) for visual reports |
| `llm_client.py` | `OpenRouterClient` | OpenRouter API client (async, configurable model/temperature) |
| `llm_dossier.py` | `build_llm_dossier` | Converts `SessionData` + `FactResult` into structured LLM dossier |
| `llm_models.py` | `LLMDossier`, `LLMDecision` | Data structures for LLM input/output |
| `llm_reasoner.py` | `LLMReportReasoner`, `answer_followup_question` | LLM prompt → structured decision. Follow-up Q&A for `/ask` command |

### Rules Subdirectory (`reports/rules/`)

| File | Purpose |
|------|---------|
| `informed_flow.py` | Informed flow detection rules (large order clustering, sweep patterns) |
| `support_resistance.py` | Support/resistance level identification (volume profile, price action, cross-day) |

## Pipeline Architecture

### Rule-Based Path

```
ClickHouse
  → DataCollector.collect_core(symbol, time_filter, session, date)
    → SessionData (ticks, volumes, OHLC, flow bars)
  → extract_all(session_data, prev_days)
    → FactResult (flow, segments, volume_profile, levels, ...)
  → ReportReasoner.analyze(fact_result)
    → ReasoningResult (scenario, insights, warnings)
  → ReportComposer.compose(fact_result, reasoning_result)
    → ComposedReport (list[MessagePart])
  → ReportDistributor / Telegram Bot
```

### Hybrid LLM Path

```
Rule-Based Path (same as above, produces FactResult)
  → build_llm_dossier(session_data, fact_result)
    → LLMDossier (structured context for LLM)
  → LLMReportReasoner.reason(dossier)
    → LLMDecision (interpretation, scenarios, confidence)
  → ReportComposer.compose_hybrid(fact_result, llm_decision)
    → ComposedReport (richer narrative + LLM insights)
```

LLM failure is **non-fatal** — falls back to rule-based report with a warning.

## Session & Date Resolution

| Session | Trading Hours (CST) | Date Logic |
|---------|---------------------|------------|
| Day | 08:45–13:45 | Always today |
| Night | 15:00–05:00 | Before 15:00 → yesterday's date; after 15:00 → today's date |

`resolve_trading_date(session)` handles weekend/holiday edge cases.

## Report Sections

A `ComposedReport` contains ordered `MessagePart` items:

| Section | Content | Part Type |
|---------|---------|-----------|
| Header | Symbol, session, date, OHLC summary | `text` |
| PnL summary | Realized + unrealized P&L (if trading data available) | `text` |
| Flow analysis | U/D ratio, net flow, strongest bars, segment breakdown | `text` |
| Flow heatmap | Time × price volume density visualization | `image` |
| Support/resistance | Key price levels with strength and sources | `text` |
| Informed flow | Large order clustering, sweep pattern alerts | `text` |
| Scenario | Rule-based or LLM scenario assessment | `text` |
| Latency/health | P95 latency, reconnect count, StormGuard state | `text` |

## Invocation

### From Telegram Bot

```
/report [symbol] [day|night]       → build_hybrid_report_async()
/report_rule [symbol] [day|night]  → build_report()
/levels [symbol]                   → extract_all() → LevelReasoner
/flow [symbol]                     → extract_all() → flow summary
```

### From CLI

```bash
python -m hft_platform.reports --session day --date 2026-04-12 --symbol TXFD6
python -m hft_platform.reports --session day --date 2026-04-12 --dry-run  # no send
```

### From DailyReportService (automated)

Scheduled by `bot/scheduler.py`: day 13:50 CST, night 05:05 CST.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_REPORT_ENABLED` | `0` | `1` = allow pipeline execution in production |
| `HFT_REPORT_SYMBOLS` | `TXFD6` | Comma-separated symbol list |
| `HFT_OPENROUTER_API_KEY` | — | OpenRouter API key for LLM hybrid reports |
| `HFT_OPENROUTER_MODEL` | — | Model selection for LLM reasoning |
| `HFT_CLICKHOUSE_HOST` | `localhost` | ClickHouse for data collection |
