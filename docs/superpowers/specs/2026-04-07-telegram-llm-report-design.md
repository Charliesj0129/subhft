# Telegram LLM Decision Report — Design Spec

**Date**: 2026-04-07
**Status**: Reviewed, ready for user review
**Scope**: Phase 1 self-use quality build, with future monetization preserved

## 1. Overview

Extend the existing Telegram report pipeline with an LLM decision layer that
consumes structured deterministic market facts and produces a higher-order
trading judgment.

The goal is not to replace the current reports stack. The goal is to turn the
existing `FactReport` and `ReasoningReport` into a more professional,
decision-oriented report that can later be packaged into a monetizable product.

### Goals

1. Produce a materially stronger report than the current rule-only output
2. Generate both:
   - next-session / intraday trade plan
   - 1-3 day swing view
3. Deliver the report through the existing Telegram bot via:
   - scheduled push
   - manual `/report` trigger
   - bounded follow-up Q&A
4. Keep the system safe by making LLM failures non-fatal and fully fallbackable
5. Preserve a clean path to later free-vs-paid report tiering

### Non-Goals

- No changes to live trading hot path
- No autonomous order execution from LLM output
- No billing, membership management, or channel commercialization in Phase 1
- No unrestricted chatbot behavior detached from report evidence

## 2. Product Positioning

Phase 1 serves a single operator only. The purpose is quality building, not
distribution scale.

The report should feel closer to a professional discretionary trading note than
to a generic market summary. It must provide:

- a market verdict
- a concrete intraday plan
- a concrete 1-3 day swing view
- explicit invalidation conditions
- key levels in executable trading language
- counter-arguments and execution constraints

This Phase 1 output becomes the base asset for later monetization. Only after
quality is proven should the same pipeline be split into free summary and paid
depth variants.

Future free-vs-paid tiering, provider abstraction expansion, and cheaper-model
routing are preserved design constraints only. They are not Phase 1 delivery
requirements.

## 3. Architecture

### 3.1 Hybrid Pipeline

The existing reports stack remains the source of truth for deterministic facts.
The LLM is inserted as a cold-path decision layer between rule reasoning and
final composition.

```text
collector
  -> facts
  -> rule reasoner
  -> llm dossier builder
  -> llm decision engine
  -> hybrid composer
  -> Telegram bot / distributor
```

### 3.2 Key Decision

The LLM must not infer directly from raw ticks or arbitrary prose. It should
consume a compact, structured dossier derived from:

- `FactReport`
- `ReasoningReport`
- precomputed support/resistance and scenario signals

This keeps token usage bounded, improves reproducibility, and makes evidence
traceable in the final report.

### 3.3 Failure Boundary

LLM generation is optional at runtime. If the LLM layer fails for any reason,
the report system must still emit the existing deterministic report.

Failure cases include:

- provider timeout
- OpenRouter 429 / 5xx
- malformed JSON
- schema validation failure
- self-contradictory decision output
- missing required sections

### 3.4 Async Boundary

The implementation should not refactor the entire existing reports module into a
fully async pipeline.

Phase 1 async contract:

- deterministic report stages remain sync internally
- bot handlers and scheduler stay async entry points
- blocking deterministic work is offloaded from the bot event loop via
  `asyncio.to_thread(...)`
- OpenRouter calls are performed asynchronously via `aiohttp`
- final Telegram send path remains async

Recommended ownership:

- `reports/pipeline.py` exports Telegram-facing async orchestration entry points
- existing sync helpers remain available for CLI/debug use
- `handlers.py` and `scheduler.py` call the async orchestration entry points

This means Phase 1 should introduce a dedicated async orchestration layer for
Telegram-facing report generation rather than attempting a broad async rewrite
of collector/facts/reasoner/composer internals.

## 4. File Layout

```text
src/hft_platform/reports/
├── llm_models.py        # LLM dossier + validated decision result contracts
├── llm_dossier.py       # FactReport/ReasoningReport -> LLMDossier
├── llm_client.py        # OpenRouter HTTP adapter
├── llm_reasoner.py      # Prompt assembly, parsing, validation, guardrails
├── composer.py          # Insert LLM decision block into composed report
└── pipeline.py          # Orchestrates hybrid build path

src/hft_platform/bot/
├── handlers.py          # /report hybrid path, /ask, rule-only/debug paths
└── scheduler.py         # scheduled hybrid pushes
```

No changes are required in latency-sensitive runtime modules. This work stays in
`reports/` and `bot/`.

## 5. Data Model

### 5.1 LLMDossier

`LLMDossier` is a compact, structured representation of the session intended for
LLM consumption. It should contain only high-value evidence rather than the full
raw report object graph.

Suggested fields:

```python
@dataclass(frozen=True, slots=True)
class LLMDossier:
    symbol: str
    session: str
    date: str
    session_summary: SessionSummary
    flow: FlowEvidence
    chips: ChipEvidence
    structure: StructureEvidence
    cross_day: CrossDayEvidence
    levels: list[LevelEvidence]
    narrative: list[str]
    rule_bias: RuleBiasEvidence
```

Important inputs include:

- session U/D ratio
- session net flow
- end-of-day drift
- strongest buy/sell bars
- segment summaries
- chip net ratio
- cluster zones
- failed breakouts
- support/resistance levels
- cross-day trend and reversal context
- rule-based bias and confidence

The dossier builder is responsible for token discipline. It must summarize,
filter, and normalize evidence rather than dumping whole objects into prompts.

### 5.2 LLMDecisionReport

The LLM output must be schema-validated and cannot be accepted as free-form
text.

Suggested shape:

```python
@dataclass(frozen=True, slots=True)
class LLMDecisionReport:
    market_verdict: MarketVerdict
    intraday_plan: TradePlan
    swing_plan: TradePlan
    key_levels: list[DecisionLevel]
    invalidations: list[str]
    counter_case: str
    execution_notes: list[str]
    confidence: int
    evidence_refs: list[EvidenceRef]
```

Where `TradePlan` contains at least:

- directional stance
- setup premise
- trigger conditions
- preferred execution style
- stop / invalidation
- target 1 / target 2
- risk note

### 5.3 Evidence Referencing

Every major recommendation should be traceable back to dossier evidence.

`EvidenceRef` should reference stable dossier keys such as:

- `flow.session_ud`
- `flow.eod_drift`
- `chips.net_ratio`
- `segments.closing`
- `cross_day.trend_direction`
- `levels.R1`
- `levels.S1`

This enables both debugability and later quality review.

Level references must be canonicalized by the dossier builder, not inferred ad
hoc by the model. Canonical ordering rules:

- resistances are sorted by nearest price above session close, then labeled
  `R1`, `R2`, `R3`
- supports are sorted by nearest price below session close, then labeled
  `S1`, `S2`, `S3`
- only the canonical labels emitted by the dossier may be cited in
  `evidence_refs`

## 6. OpenRouter Integration

### 6.1 Provider Strategy

Phase 1 uses OpenRouter as the single provider endpoint:

```bash
HFT_LLM_ENABLED=1
HFT_LLM_PROVIDER=openrouter
HFT_LLM_BASE_URL=https://openrouter.ai/api/v1
HFT_LLM_API_KEY=<secret>
HFT_LLM_MODEL=<model_name>
HFT_LLM_TIMEOUT_S=25
HFT_LLM_MAX_RETRIES=2
```

The code should still isolate provider details behind `llm_client.py` so model
or provider changes do not leak into report orchestration.

### 6.2 Client Behavior

`OpenRouterClient` should provide:

- async HTTP via `aiohttp`
- explicit timeout
- narrow retry policy for transient failures only
- structured logging without leaking API keys
- response extraction isolated from business logic

Synchronous `requests` must not be used on the main bot event loop.

The client is async-only. Any sync compatibility required by CLI entry points
should be handled at the orchestration layer rather than by adding a second
blocking HTTP implementation.

### 6.3 Model Configuration

The model name is environment-driven. No model should be hardcoded in report
logic.

Phase 1 uses one high-quality model for report generation. Later phases may
route:

- premium model for scheduled full reports
- cheaper model for bounded follow-up Q&A

## 7. Prompting and Guardrails

### 7.1 Prompt Contract

The prompt must instruct the model to behave like a disciplined market analyst,
not a chat assistant.

Required behaviors:

- use only dossier evidence
- do not fabricate news, macro, or unseen market context
- when evidence conflicts, reduce confidence and prefer wait/neutral framing
- always include invalidation conditions
- write in professional Traditional Chinese suitable for Telegram delivery
- return machine-parseable JSON only

### 7.2 Output Sections

The final report should consistently contain six sections:

1. Market verdict
2. Intraday trade plan
3. 1-3 day swing view
4. Key levels map
5. Counter-case / what would prove the thesis wrong
6. Execution notes

This structure creates a direct path to future monetization:

- public summary can expose verdict + partial levels
- paid report can expose the full plans, invalidations, and execution notes

### 7.3 Validation Rules

The parsed `LLMDecisionReport` is rejected if any of the following are true:

- missing required fields
- invalid JSON
- confidence outside expected range
- no invalidation conditions
- references nonexistent evidence keys
- plan contains target/stop without directional logic
- content is empty, generic, or obviously template-spam

### 7.4 Fallback Rules

If validation fails, the pipeline must:

1. log the failure with sanitized details
2. mark the LLM stage as degraded
3. continue using the deterministic report only

No scheduled report should be dropped because the LLM layer failed.

## 8. Report Composition

### 8.1 Hybrid Composition Strategy

`composer.py` remains the final report assembly point. The existing deterministic
messages stay intact and are supplemented with an LLM decision block.

Recommended ordering:

1. existing summary / session facts
2. LLM market verdict
3. LLM intraday plan
4. LLM swing plan
5. LLM key levels map
6. LLM counter-case and execution notes
7. existing deeper rule-based detail blocks
8. disclaimer

This preserves transparency. The user can still see the rule layer underneath
the higher-order interpretation.

### 8.2 Message Length Handling

The LLM block must be composed into Telegram-safe chunks and pass through the
existing message splitting logic.

Rules:

- respect Telegram max length
- keep section boundaries intact where possible
- do not allow malformed HTML
- prefer concise, high-density wording over long narrative prose

## 9. Telegram Bot Behavior

### 9.1 Scheduled Push

Existing day/night scheduling remains unchanged. Scheduled jobs should now build
the hybrid report.

Behavior:

- run deterministic stages first
- attempt LLM decision generation
- fallback automatically if needed
- send final hybrid report to owner chat

### 9.2 Manual Trigger

`/report` becomes the default hybrid report entry point.

Recommended supporting commands:

- `/report` -> hybrid report
- `/report_rule` -> deterministic report only
- `/report_llm` -> optional debug path for LLM-enhanced output comparison

The debug paths are valuable in Phase 1 because quality is the primary goal.

### 9.3 Follow-Up Q&A

Add bounded follow-up via `/ask <question>`.

This is not a free chat session. The answer must be grounded in:

- the most recent manually generated hybrid report context
- that report's `LLMDossier`
- that report's `LLMDecisionReport`

Constraints:

- owner-only
- limited to the latest manually generated hybrid `/report` context
- no broader market speculation without evidence
- answer should cite relevant dossier evidence where possible

Scheduled pushes do not become `/ask` context automatically. This avoids
ambiguous multi-symbol state after a scheduled batch.

If the latest manual `/report` fell back to deterministic-only output and has no
validated `LLMDecisionReport`, `/ask` must refuse and instruct the user to rerun
`/report` when the LLM layer is available.

### 9.4 Session State

The bot process should cache the latest successful manual hybrid report context
in memory so `/ask` can work without rerunning the full pipeline every time.

Suggested cached object:

```python
@dataclass(slots=True)
class LatestReportContext:
    symbol: str
    session: str
    date: str
    dossier: LLMDossier
    decision: LLMDecisionReport
```

Scheduled reports may maintain their own passive cache for observability or
debugging, but that cache is not consulted by `/ask` in Phase 1.

## 10. Cost and Quality Strategy

Phase 1 prioritizes judgment quality, but the implementation should still be
cost-aware.

Guidelines:

- keep prompts dossier-based and compact
- avoid passing redundant rule text
- keep follow-up Q&A separate from full report generation
- log model name, latency, and approximate token usage when available
- support turning the LLM layer off via env without code changes

Later optimization path:

- full report uses stronger model
- follow-up Q&A uses cheaper model
- public/free tier uses lighter generation or rule-only summaries

## 11. Testing Strategy

### 11.1 Unit Tests

Add tests for:

- dossier building from representative fact inputs
- response parsing and schema validation
- evidence reference validation
- fallback on timeout / 429 / malformed JSON
- message composition and Telegram chunk splitting

### 11.2 Bot Tests

Add tests for:

- `/report` invoking hybrid build path
- `/report_rule` bypassing LLM
- `/ask` using latest cached context only
- unauthorized users receiving `未授權`

### 11.3 Manual Evaluation

For the same session, compare:

- deterministic report
- hybrid LLM report

Review dimensions:

- specificity of plan
- clarity of invalidation
- coherence between verdict and evidence
- usefulness for next-session execution
- usefulness for 1-3 day swing positioning

Phase 1 success is qualitative but should be evidence-driven. The report should
be noticeably more actionable than the current rule-only output.

## 12. Rollout Plan

### Phase 1: Self-Use Quality Build

- owner-only Telegram delivery
- scheduled push + manual trigger + bounded `/ask`
- one OpenRouter model
- deterministic fallback always enabled

### Phase 2: Monetization Readiness

- split free summary and paid depth
- tune prompt and composer for tiered output
- add quality review process across sessions
- optionally add lower-cost model routing

### Phase 3: Commercial Distribution

- enable public/private channel split
- operationalize subscription workflow outside the report core

## 13. Risks

1. LLM output may sound confident while being weakly grounded
2. Unstructured prompts can drift into generic investment prose
3. Token bloat can raise cost and latency without improving judgment
4. Free-form `/ask` can degrade into hallucinated commentary if not bounded
5. Overwriting the deterministic layer would make failures harder to audit

This design addresses those risks by enforcing:

- structured dossier inputs
- structured validated outputs
- explicit invalidations
- evidence references
- deterministic fallback

## 14. Acceptance Criteria

This design is complete when implementation can deliver all of the following:

1. Scheduled Telegram reports include an LLM decision layer when enabled
2. `/report` returns a hybrid report
3. `/ask` answers based only on the latest manually generated hybrid `/report`
   context
4. LLM failures never prevent deterministic report delivery
5. Output includes intraday plan, swing plan, key levels, and invalidations
6. OpenRouter configuration is fully env-driven
7. Tests cover schema validation and fallback behavior
