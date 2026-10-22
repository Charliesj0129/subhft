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
| `HFT_REPORT_ENABLED` | `0` | `1` = allow the CLI entry point (`python -m hft_platform.reports`) to run without `--dry-run` or `--debug` (`pipeline.py:300`). The bot's `/report` and its scheduled pushes do not read it |
| `HFT_REPORT_SYMBOLS` | `TXFR1` | Comma-separated symbol list; read by the bot (`bot/app.py:93`) |
| `HFT_LLM_ENABLED` | `0` | `1` = send the dossier to OpenRouter (`pipeline.py:178`). Anything else keeps the report rule-based and sends nothing |
| `HFT_LLM_API_KEY` | — | OpenRouter API key (`llm_client.py:52`). Without it every LLM call raises and the report falls back to rule-based |
| `HFT_LLM_MODEL` | empty | OpenRouter model id (`pipeline.py:183`, `llm_reasoner.py:210`) |
| `HFT_CLICKHOUSE_HOST` | `localhost` | ClickHouse for data collection |

## LLM egress: what leaves the host

With `HFT_LLM_ENABLED=1` (default `0`), the hybrid path sends text to a third party,
`https://openrouter.ai/api/v1` (the only LLM endpoint in the code; `llm_client.py:21`). With it unset or `0`,
nothing is sent: `build_hybrid_report_async` returns the rule-based report before any client is built.

| Trigger | What is sent | Source |
|---------|--------------|--------|
| `/report` in the bot, and the scheduled day/night push (`bot/scheduler.py`) | The dossier as JSON: `symbol`, `session`, `date`, an `evidence` map of market-derived values, and the first 3 narrative lines | `llm_reasoner.py::_build_prompt`, `llm_dossier.py::build_llm_dossier` |
| `/ask <question>` after a hybrid `/report` | The question text as typed, the dossier's `evidence` map (not its narrative lines or its symbol, session and date), and the model's earlier decision | `llm_reasoner.py::_build_followup_prompt`, `bot/handlers.py::cmd_ask` |

The `evidence` map holds session flow (`flow.session_ud`, `flow.session_net_flow`, `flow.eod_drift`, strongest buy and
sell bars), `chips.net_ratio` and the buy and sell zones, the cross-day trend direction, failed-breakout count, session
high and low, the closing segment, the rule engine's `rule.bias` and `rule.confidence`, and the computed price levels. It
has no account, position, order, PnL or credential field: `build_llm_dossier` copies only the keys above, and
`LLMDossier.validate()` runs before the prompt is built. The `/ask` question is free text, so its contents are whatever
the operator types.

Outside the package, only `bot/handlers.py` and `bot/scheduler.py` import `hft_platform.reports`; the trading engine never calls this path. In compose the bot is
the `hft-bot` service, and it reads `.env` (`env_file` in the shared `x-hft-common` block), so a `HFT_LLM_*` line in
`.env` reaches it when the container is created.

Off switch: leave `HFT_LLM_ENABLED` unset or `0`, and keep `HFT_LLM_API_KEY` out of `.env`. `env_file` values are read when
the container is created, so editing `.env` alone does not change a running bot. Whether sending this
data to OpenRouter is acceptable is a policy decision for the operator; this page records what is sent, not whether it
may be.

Limits and failure behavior: one request per call, 30 s timeout, one retry on status 429, 500, 502, 503 and 504 (`llm_client.py`). A failed
call is logged as `build_hybrid_report_llm_fallback` and the rule-based report is sent instead; `cmd_ask` has no
fallback and catches only `ImportError`, so any other exception propagates out of the handler.
