# Telegram LLM Decision Report Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an OpenRouter-backed LLM decision layer to the existing Telegram report flow, with deterministic fallback, manual `/ask` follow-up, and no blocking I/O on the bot event loop.

**Architecture:** Keep the current report pipeline (`build_report()`) as the deterministic sync core. Add a compact dossier builder, an async OpenRouter client, and an LLM reasoner that returns validated structured output. Expose a Telegram-facing async orchestration layer in `reports/pipeline.py`; handlers and scheduler call that wrapper, while CLI/debug paths keep the existing sync entry points.

**Tech Stack:** Python 3.12, `aiohttp`, `python-telegram-bot`, `structlog`, dataclasses with `slots=True`, pytest, unittest.mock

**Spec:** `docs/superpowers/specs/2026-04-07-telegram-llm-report-design.md`

---

## File Structure

### Create

- `src/hft_platform/reports/llm_models.py`
  Responsibility: Typed contracts for `LLMDossier`, `LLMDecisionReport`, `TradePlan`, `EvidenceRef`, and helper methods for validation.
- `src/hft_platform/reports/llm_dossier.py`
  Responsibility: Convert `FactReport` + `ReasoningReport` into a compact, canonicalized dossier with stable evidence keys.
- `src/hft_platform/reports/llm_client.py`
  Responsibility: Async OpenRouter HTTP adapter with timeout, retry, auth headers, and response extraction.
- `src/hft_platform/reports/llm_reasoner.py`
  Responsibility: Prompt assembly, async model invocation, JSON parsing, schema validation, and guardrail enforcement.
- `tests/unit/test_report_llm_models.py`
  Responsibility: Contract-level tests for new LLM dataclasses and validation helpers.
- `tests/unit/test_report_llm_dossier.py`
  Responsibility: Canonicalization and evidence key tests for dossier building.
- `tests/unit/test_report_llm_client.py`
  Responsibility: Async client behavior tests for success, timeout, retry, and auth/header handling.
- `tests/unit/test_report_llm_reasoner.py`
  Responsibility: Prompt/parse/validation/fallback trigger tests.

### Modify

- `src/hft_platform/reports/pipeline.py`
  Responsibility: Add Telegram-facing async orchestration wrappers and deterministic fallback handling.
- `src/hft_platform/reports/composer.py`
  Responsibility: Inject LLM decision sections into the composed Telegram report without breaking existing message splitting.
- `src/hft_platform/reports/models.py`
  Responsibility: Add optional composition hooks only if needed; prefer keeping core report contracts unchanged.
- `src/hft_platform/bot/app.py`
  Responsibility: Add `LatestReportContext` dataclass, in-memory cache for latest manual hybrid report context, and register new Telegram commands.
- `src/hft_platform/bot/handlers.py`
  Responsibility: Wire `/report`, `/report_rule`, `/ask`; cache only the latest manual hybrid report context.
- `src/hft_platform/bot/scheduler.py`
  Responsibility: Switch scheduled report generation to async hybrid orchestration without polluting `/ask` state.
- `tests/unit/test_report_pipeline.py`
  Responsibility: Keep date-resolution tests; add small coverage only if the new async wrappers live here.
- `tests/unit/test_report_pipeline_build.py`
  Responsibility: Preserve sync `build_report()` coverage while adding orchestration/fallback tests.
- `tests/unit/test_report_composer.py`
  Responsibility: Validate LLM section insertion and Telegram-safe splitting.
- `tests/unit/test_bot_app.py`
  Responsibility: Cache state tests if new app-level cache helpers are introduced there.
- `tests/unit/test_bot_handlers.py`
  Responsibility: `/report`, `/report_rule`, `/ask`, cache semantics, owner-only behavior.
- `tests/unit/test_bot_scheduler.py`
  Responsibility: Scheduled hybrid sends, fallback sends, and explicit non-interference with `/ask` cache.
- `tests/unit/test_report_integration.py`
  Responsibility: End-to-end hybrid pipeline smoke tests with mocked LLM layer.

### Do Not Touch

- Hot path trading modules under `feed_adapter/`, `strategy/`, `risk/`, `order/`, `execution/`
- Existing deterministic report extractors unless a dossier input is genuinely missing
- Telegram channel monetization or free/paid routing beyond preserving compatibility

---

### Task 1: Add LLM contracts and validation helpers

**Files:**
- Create: `src/hft_platform/reports/llm_models.py`
- Test: `tests/unit/test_report_llm_models.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_report_llm_models.py`:

```python
from __future__ import annotations

import pytest

from hft_platform.reports.llm_models import (
    EvidenceRef,
    LLMDecisionReport,
    LLMDossier,
    TradePlan,
    canonical_level_label,
)


class TestCanonicalLevelLabel:
    def test_resistance_labels_start_at_r1(self) -> None:
        assert canonical_level_label("resistance", 0) == "R1"
        assert canonical_level_label("resistance", 2) == "R3"

    def test_support_labels_start_at_s1(self) -> None:
        assert canonical_level_label("support", 0) == "S1"
        assert canonical_level_label("support", 1) == "S2"

    def test_invalid_side_raises(self) -> None:
        with pytest.raises(ValueError, match="side"):
            canonical_level_label("neutral", 0)


class TestTradePlan:
    def test_requires_direction_trigger_stop_and_targets(self) -> None:
        plan = TradePlan(
            stance="bullish",
            premise="closing flow remains supportive",
            trigger="reclaim R1 with hold",
            execution_style="buy pullback after reclaim",
            stop="lose session VWAP",
            target_1="R2",
            target_2="R3",
            risk_note="reduce size if opening range expands",
        )
        assert plan.stance == "bullish"

    def test_validate_rejects_missing_trigger_for_directional_plan(self) -> None:
        plan = TradePlan(
            stance="bullish",
            premise="closing flow remains supportive",
            trigger="",
            execution_style="buy pullback after reclaim",
            stop="lose session VWAP",
            target_1="R2",
            target_2="R3",
            risk_note="reduce size if opening range expands",
        )
        with pytest.raises(ValueError, match="trigger"):
            plan.validate()


class TestLLMDecisionReport:
    def test_validate_accepts_complete_report(self) -> None:
        report = LLMDecisionReport(
            market_verdict="偏多延續，但只在回測不破支撐時成立",
            intraday_plan=TradePlan(
                stance="bullish",
                premise="closing segment stayed bull",
                trigger="hold above S1 after open",
                execution_style="buy pullback",
                stop="lose S1",
                target_1="R1",
                target_2="R2",
                risk_note="skip if opening gap exceeds prior range",
            ),
            swing_plan=TradePlan(
                stance="bullish",
                premise="cross-day trend remains up",
                trigger="day close above R1",
                execution_style="hold partial overnight",
                stop="daily close back below S1",
                target_1="R2",
                target_2="R3",
                risk_note="de-risk before major event risk",
            ),
            key_levels=["S1 22300-22320", "R1 22420-22440"],
            invalidations=["lose S1 on expanding sell flow"],
            counter_case="若開盤後賣壓擴大且無法收復 R1，原偏多失效",
            execution_notes=["只做回測接，不追高"],
            confidence=72,
            evidence_refs=[
                EvidenceRef(key="flow.session_ud", detail="1.18"),
                EvidenceRef(key="levels.S1", detail="22300-22320"),
            ],
        )
        report.validate()

    def test_validate_rejects_missing_invalidation(self) -> None:
        report = LLMDecisionReport(
            market_verdict="中性",
            intraday_plan=TradePlan(
                stance="neutral",
                premise="mixed evidence",
                trigger="wait",
                execution_style="observe",
                stop="n/a",
                target_1="n/a",
                target_2="n/a",
                risk_note="do not force entries",
            ),
            swing_plan=TradePlan(
                stance="neutral",
                premise="mixed evidence",
                trigger="wait",
                execution_style="observe",
                stop="n/a",
                target_1="n/a",
                target_2="n/a",
                risk_note="do not force entries",
            ),
            key_levels=[],
            invalidations=[],
            counter_case="counter",
            execution_notes=[],
            confidence=50,
            evidence_refs=[],
        )
        with pytest.raises(ValueError, match="invalidations"):
            report.validate()

    def test_validate_rejects_out_of_range_confidence(self) -> None:
        report = LLMDecisionReport(
            market_verdict="偏多",
            intraday_plan=TradePlan("bullish", "p", "t", "e", "s", "t1", "t2", "r"),
            swing_plan=TradePlan("bullish", "p", "t", "e", "s", "t1", "t2", "r"),
            key_levels=[],
            invalidations=["lose S1"],
            counter_case="counter",
            execution_notes=[],
            confidence=140,
            evidence_refs=[],
        )
        with pytest.raises(ValueError, match="confidence"):
            report.validate()

    def test_validate_rejects_generic_verdict(self) -> None:
        report = LLMDecisionReport(
            market_verdict="市場有漲有跌，請自行判斷風險",
            intraday_plan=TradePlan("bullish", "p", "t", "e", "s", "t1", "t2", "r"),
            swing_plan=TradePlan("bullish", "p", "t", "e", "s", "t1", "t2", "r"),
            key_levels=[],
            invalidations=["lose S1"],
            counter_case="counter",
            execution_notes=[],
            confidence=55,
            evidence_refs=[EvidenceRef(key="flow.session_ud", detail="1.18")],
        )
        with pytest.raises(ValueError, match="generic"):
            report.validate()

    def test_validate_rejects_empty_required_text(self) -> None:
        report = LLMDecisionReport(
            market_verdict="",
            intraday_plan=TradePlan("bullish", "p", "t", "e", "s", "t1", "t2", "r"),
            swing_plan=TradePlan("bullish", "p", "t", "e", "s", "t1", "t2", "r"),
            key_levels=[],
            invalidations=["lose S1"],
            counter_case="counter",
            execution_notes=[],
            confidence=55,
            evidence_refs=[EvidenceRef(key="flow.session_ud", detail="1.18")],
        )
        with pytest.raises(ValueError, match="market_verdict"):
            report.validate()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_report_llm_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hft_platform.reports.llm_models'`

- [ ] **Step 3: Write minimal implementation**

Create `src/hft_platform/reports/llm_models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


def canonical_level_label(side: str, index: int) -> str:
    if side == "resistance":
        return f"R{index + 1}"
    if side == "support":
        return f"S{index + 1}"
    raise ValueError(f"Unknown side {side!r}; expected 'support' or 'resistance'")


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    key: str
    detail: str


@dataclass(frozen=True, slots=True)
class TradePlan:
    stance: str
    premise: str
    trigger: str
    execution_style: str
    stop: str
    target_1: str
    target_2: str
    risk_note: str

    def validate(self) -> None:
        if not self.stance.strip():
            raise ValueError("stance must not be empty")
        if self.stance != "neutral" and not self.trigger.strip():
            raise ValueError("trigger must not be empty for directional plans")
        if self.stance != "neutral" and (
            not self.stop.strip() or
            not self.target_1.strip() or
            not self.target_2.strip()
        ):
            raise ValueError("directional plans require stop, target_1, and target_2")


@dataclass(frozen=True, slots=True)
class LLMDossier:
    symbol: str
    session: str
    date: str
    evidence: dict[str, str]
    narrative: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LLMDecisionReport:
    market_verdict: str
    intraday_plan: TradePlan
    swing_plan: TradePlan
    key_levels: list[str]
    invalidations: list[str]
    counter_case: str
    execution_notes: list[str]
    confidence: int
    evidence_refs: list[EvidenceRef]

    def validate(self) -> None:
        if not self.market_verdict.strip():
            raise ValueError("market_verdict must not be empty")
        self.intraday_plan.validate()
        self.swing_plan.validate()
        if not self.invalidations:
            raise ValueError("invalidations must not be empty")
        if not 0 <= self.confidence <= 100:
            raise ValueError("confidence must be between 0 and 100")
        generic_markers = ("請自行判斷", "有漲有跌", "注意風險")
        if any(marker in self.market_verdict for marker in generic_markers):
            raise ValueError("generic market_verdict is not allowed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_llm_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/llm_models.py tests/unit/test_report_llm_models.py
git commit -m "feat(reports): add llm report contracts"
```

---

### Task 2: Build canonicalized LLM dossier from deterministic reports

**Files:**
- Create: `src/hft_platform/reports/llm_dossier.py`
- Modify: `src/hft_platform/reports/llm_models.py`
- Test: `tests/unit/test_report_llm_dossier.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_report_llm_dossier.py`:

```python
from __future__ import annotations

from hft_platform.reports.composer import _p
from hft_platform.reports.llm_dossier import build_llm_dossier
from tests.unit.test_report_composer import _make_fact_report, _make_reasoning_report


class TestBuildLLMDossier:
    def test_includes_expected_evidence_keys(self) -> None:
        dossier = build_llm_dossier(_make_fact_report(), _make_reasoning_report())

        assert dossier.symbol == "TXFD6"
        assert dossier.evidence["flow.session_ud"] == "1.15"
        assert dossier.evidence["chips.net_ratio"] == "0.625"
        assert "cross_day.trend_direction" in dossier.evidence

    def test_canonicalizes_level_labels_relative_to_close(self) -> None:
        dossier = build_llm_dossier(_make_fact_report(), _make_reasoning_report())

        assert "levels.R1" in dossier.evidence
        assert "levels.S1" in dossier.evidence

    def test_keeps_narrative_compact(self) -> None:
        dossier = build_llm_dossier(_make_fact_report(), _make_reasoning_report())

        assert isinstance(dossier.narrative, tuple)
        assert len(dossier.narrative) >= 1
        assert all(len(line) < 200 for line in dossier.narrative)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_report_llm_dossier.py -v`
Expected: FAIL with `ModuleNotFoundError` for `hft_platform.reports.llm_dossier`

- [ ] **Step 3: Write minimal implementation**

Create `src/hft_platform/reports/llm_dossier.py`:

```python
from __future__ import annotations

from hft_platform.contracts.types import PLATFORM_SCALE
from hft_platform.reports.llm_models import LLMDossier, canonical_level_label


def _price_text(price: int) -> str:
    return f"{price // PLATFORM_SCALE:,}"


def build_llm_dossier(fact_report, reasoning_report) -> LLMDossier:
    session_close = fact_report.session_data.close
    evidence: dict[str, str] = {
        "flow.session_ud": f"{fact_report.flow.session_ud:.2f}",
        "flow.session_net_flow": f"{fact_report.flow.session_net_flow:+,}",
        "flow.eod_drift": f"{fact_report.flow.eod_drift:+.2f}",
        "chips.net_ratio": f"{fact_report.chips.net_ratio:.3f}",
        "cross_day.trend_direction": fact_report.cross_day.trend_direction,
        "rule.bias": reasoning_report.bias.bias,
        "rule.confidence": f"{reasoning_report.bias.confidence:.2f}",
    }

    resistances = sorted(
        [lv for lv in reasoning_report.levels if lv.side == "resistance"],
        key=lambda lv: (abs(lv.price - session_close), lv.price),
    )
    supports = sorted(
        [lv for lv in reasoning_report.levels if lv.side == "support"],
        key=lambda lv: (abs(lv.price - session_close), lv.price),
    )

    for idx, level in enumerate(resistances[:3]):
        evidence[f"levels.{canonical_level_label('resistance', idx)}"] = _price_text(level.price)
    for idx, level in enumerate(supports[:3]):
        evidence[f"levels.{canonical_level_label('support', idx)}"] = _price_text(level.price)

    return LLMDossier(
        symbol=fact_report.session_data.symbol,
        session=fact_report.session_data.session,
        date=fact_report.session_data.date,
        evidence=evidence,
        narrative=tuple(reasoning_report.narrative.storyline[:3]),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_llm_dossier.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/llm_models.py src/hft_platform/reports/llm_dossier.py tests/unit/test_report_llm_dossier.py
git commit -m "feat(reports): add llm dossier builder"
```

---

### Task 3: Add async OpenRouter client with timeout and narrow retry policy

**Files:**
- Create: `src/hft_platform/reports/llm_client.py`
- Test: `tests/unit/test_report_llm_client.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_report_llm_client.py`:

```python
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.reports.llm_client import OpenRouterClient


class TestOpenRouterClient:
    @pytest.mark.asyncio
    async def test_returns_message_content_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_LLM_API_KEY", "secret")
        client = OpenRouterClient(model="demo-model", base_url="https://openrouter.ai/api/v1", timeout_s=5, max_retries=1)

        fake_response = MagicMock()
        fake_response.status = 200
        fake_response.json = AsyncMock(return_value={
            "choices": [{"message": {"content": "{\"market_verdict\": \"偏多\"}"}}]
        })
        fake_response.__aenter__ = AsyncMock(return_value=fake_response)
        fake_response.__aexit__ = AsyncMock(return_value=None)

        fake_session = MagicMock()
        fake_session.post.return_value = fake_response

        result = await client.complete_json_from_session(fake_session, "prompt")
        assert result["market_verdict"] == "偏多"

    @pytest.mark.asyncio
    async def test_raises_when_api_key_missing(self) -> None:
        client = OpenRouterClient(model="demo-model", api_key="", base_url="https://openrouter.ai/api/v1")
        with pytest.raises(RuntimeError, match="HFT_LLM_API_KEY"):
            client._headers()

    @pytest.mark.asyncio
    async def test_retries_429_once_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_LLM_API_KEY", "secret")
        client = OpenRouterClient(model="demo-model", base_url="https://openrouter.ai/api/v1", timeout_s=5, max_retries=2)

        rate_limited = MagicMock()
        rate_limited.status = 429
        rate_limited.json = AsyncMock(return_value={"error": {"message": "rate limit"}})
        rate_limited.__aenter__ = AsyncMock(return_value=rate_limited)
        rate_limited.__aexit__ = AsyncMock(return_value=None)

        ok = MagicMock()
        ok.status = 200
        ok.json = AsyncMock(return_value={"choices": [{"message": {"content": "{}"}}]})
        ok.__aenter__ = AsyncMock(return_value=ok)
        ok.__aexit__ = AsyncMock(return_value=None)

        fake_session = MagicMock()
        fake_session.post.side_effect = [rate_limited, ok]

        result = await client.complete_json_from_session(fake_session, "prompt")
        assert result == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_report_llm_client.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `src/hft_platform/reports/llm_client.py`:

```python
from __future__ import annotations

import asyncio
import os

import aiohttp


class OpenRouterClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 25.0,
        max_retries: int = 2,
    ) -> None:
        self._model = model
        self._api_key = api_key if api_key is not None else os.environ.get("HFT_LLM_API_KEY", "")
        self._base_url = base_url or os.environ.get("HFT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
        self._timeout_s = timeout_s
        self._max_retries = max_retries

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise RuntimeError("HFT_LLM_API_KEY is required")
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _request_json(self, session: aiohttp.ClientSession, payload: dict) -> dict:
        url = f"{self._base_url}/chat/completions"
        for attempt in range(self._max_retries + 1):
            async with session.post(url, json=payload, headers=self._headers(), timeout=self._timeout_s) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                raise RuntimeError(f"OpenRouter request failed with status {resp.status}")
        raise RuntimeError("OpenRouter retries exhausted")

    async def complete_json_from_session(self, session: aiohttp.ClientSession, prompt: str) -> dict:
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        body = await self._request_json(session, payload)
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise RuntimeError("OpenRouter content must be a JSON string")
        import json

        return json.loads(content)

    async def complete_json(self, prompt: str) -> dict:
        async with aiohttp.ClientSession() as session:
            return await self.complete_json_from_session(session, prompt)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_llm_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/llm_client.py tests/unit/test_report_llm_client.py
git commit -m "feat(reports): add async openrouter client"
```

---

### Task 4: Add LLM reasoner with prompt, parsing, and guardrails

**Files:**
- Create: `src/hft_platform/reports/llm_reasoner.py`
- Modify: `src/hft_platform/reports/llm_models.py`
- Test: `tests/unit/test_report_llm_reasoner.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_report_llm_reasoner.py`:

```python
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from hft_platform.reports.llm_models import LLMDossier
from hft_platform.reports.llm_reasoner import LLMReportReasoner


def _make_dossier() -> LLMDossier:
    return LLMDossier(
        symbol="TXFD6",
        session="day",
        date="2026-04-07",
        evidence={
            "flow.session_ud": "1.18",
            "chips.net_ratio": "0.62",
            "levels.S1": "22,300",
            "levels.R1": "22,440",
        },
        narrative=("opening bid held", "closing segment stayed firm"),
    )


class TestLLMReportReasoner:
    @pytest.mark.asyncio
    async def test_returns_validated_report(self) -> None:
        client = AsyncMock()
        client.complete_json.return_value = {
            "market_verdict": "偏多延續",
            "intraday_plan": {
                "stance": "bullish",
                "premise": "closing flow held",
                "trigger": "hold above S1",
                "execution_style": "buy pullback",
                "stop": "lose S1",
                "target_1": "R1",
                "target_2": "R2",
                "risk_note": "avoid chasing gap-up open",
            },
            "swing_plan": {
                "stance": "bullish",
                "premise": "cross-day trend up",
                "trigger": "daily close above R1",
                "execution_style": "hold partial",
                "stop": "daily close below S1",
                "target_1": "R2",
                "target_2": "R3",
                "risk_note": "reduce before event risk",
            },
            "key_levels": ["S1 22,300", "R1 22,440"],
            "invalidations": ["lose S1 with expanding sell flow"],
            "counter_case": "opening failure back below S1 negates the thesis",
            "execution_notes": ["do not chase first impulse"],
            "confidence": 71,
            "evidence_refs": [{"key": "flow.session_ud", "detail": "1.18"}],
        }

        reasoner = LLMReportReasoner(client=client)
        result = await reasoner.generate(_make_dossier())

        assert result.market_verdict == "偏多延續"
        client.complete_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_unknown_evidence_ref(self) -> None:
        client = AsyncMock()
        client.complete_json.return_value = {
            "market_verdict": "偏多",
            "intraday_plan": {"stance": "bullish", "premise": "p", "trigger": "t", "execution_style": "e", "stop": "s", "target_1": "t1", "target_2": "t2", "risk_note": "r"},
            "swing_plan": {"stance": "bullish", "premise": "p", "trigger": "t", "execution_style": "e", "stop": "s", "target_1": "t1", "target_2": "t2", "risk_note": "r"},
            "key_levels": [],
            "invalidations": ["lose S1"],
            "counter_case": "counter",
            "execution_notes": [],
            "confidence": 65,
            "evidence_refs": [{"key": "levels.R99", "detail": "bad"}],
        }

        reasoner = LLMReportReasoner(client=client)
        with pytest.raises(ValueError, match="Unknown evidence ref"):
            await reasoner.generate(_make_dossier())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_report_llm_reasoner.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `src/hft_platform/reports/llm_reasoner.py`:

```python
from __future__ import annotations

import json

from hft_platform.reports.llm_models import EvidenceRef, LLMDecisionReport, TradePlan


class LLMReportReasoner:
    def __init__(self, *, client) -> None:
        self._client = client

    def _build_prompt(self, dossier) -> str:
        return (
            "You are a disciplined market analyst. "
            "Use only the provided evidence. "
            "Return JSON only.\n"
            f"Symbol: {dossier.symbol}\n"
            f"Session: {dossier.session}\n"
            f"Evidence: {json.dumps(dossier.evidence, ensure_ascii=False)}"
        )

    async def generate(self, dossier) -> LLMDecisionReport:
        payload = await self._client.complete_json(self._build_prompt(dossier))

        report = LLMDecisionReport(
            market_verdict=payload["market_verdict"],
            intraday_plan=TradePlan(**payload["intraday_plan"]),
            swing_plan=TradePlan(**payload["swing_plan"]),
            key_levels=list(payload["key_levels"]),
            invalidations=list(payload["invalidations"]),
            counter_case=payload["counter_case"],
            execution_notes=list(payload["execution_notes"]),
            confidence=int(payload["confidence"]),
            evidence_refs=[EvidenceRef(**item) for item in payload["evidence_refs"]],
        )
        report.validate()
        allowed = set(dossier.evidence.keys())
        for ref in report.evidence_refs:
            if ref.key not in allowed:
                raise ValueError(f"Unknown evidence ref: {ref.key}")
        return report
```

Clarification: the client owns OpenRouter response extraction and JSON decoding.
`LLMReportReasoner` receives an already-parsed decision dict and is responsible
only for prompt assembly plus business-rule validation.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_llm_reasoner.py tests/unit/test_report_llm_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/llm_client.py src/hft_platform/reports/llm_reasoner.py src/hft_platform/reports/llm_models.py tests/unit/test_report_llm_reasoner.py tests/unit/test_report_llm_client.py
git commit -m "feat(reports): add llm decision reasoner"
```

---

### Task 5: Add async hybrid orchestration and deterministic fallback in `pipeline.py`

**Files:**
- Modify: `src/hft_platform/reports/pipeline.py`
- Test: `tests/unit/test_report_pipeline_build.py`

- [ ] **Step 1: Write the failing tests**

Extend `tests/unit/test_report_pipeline_build.py`:

```python
class TestBuildHybridReport:
    @pytest.mark.asyncio
    async def test_async_wrapper_runs_build_report_in_thread_and_adds_llm_result(self, mock_session_data, mock_fact_report, mock_reasoning_report):
        mock_collector = MagicMock()
        mock_collector.collect.return_value = mock_session_data
        mock_collector.collect_cross_day.return_value = []

        mock_composer_inst = MagicMock()
        mock_composer_inst.compose.return_value = _make_composed()

        fake_llm_report = MagicMock()
        with (
            patch(_PATCH_COLLECTOR, return_value=mock_collector),
            patch(_PATCH_EXTRACT, return_value=mock_fact_report),
            patch(_PATCH_REASON, return_value=mock_reasoning_report),
            patch(_PATCH_COMPOSER, return_value=mock_composer_inst),
            patch("hft_platform.reports.pipeline.build_llm_dossier") as mock_dossier,
            patch("hft_platform.reports.pipeline.LLMReportReasoner") as MockReasoner,
            patch("hft_platform.reports.pipeline.OpenRouterClient") as MockClient,
        ):
            MockReasoner.return_value.generate = AsyncMock(return_value=fake_llm_report)
            from hft_platform.reports.pipeline import build_hybrid_report_async

            result = await build_hybrid_report_async("day", "2026-03-28", "TXFD6")

        assert result.composed is not None
        assert result.decision is fake_llm_report

    @pytest.mark.asyncio
    async def test_async_wrapper_falls_back_when_llm_fails(self, mock_session_data, mock_fact_report, mock_reasoning_report):
        mock_collector = MagicMock()
        mock_collector.collect.return_value = mock_session_data
        mock_collector.collect_cross_day.return_value = []

        mock_composer_inst = MagicMock()
        mock_composer_inst.compose.return_value = _make_composed()

        with (
            patch(_PATCH_COLLECTOR, return_value=mock_collector),
            patch(_PATCH_EXTRACT, return_value=mock_fact_report),
            patch(_PATCH_REASON, return_value=mock_reasoning_report),
            patch(_PATCH_COMPOSER, return_value=mock_composer_inst),
            patch("hft_platform.reports.pipeline.build_llm_dossier") as mock_dossier,
            patch("hft_platform.reports.pipeline.LLMReportReasoner") as MockReasoner,
            patch("hft_platform.reports.pipeline.OpenRouterClient") as MockClient,
        ):
            MockReasoner.return_value.generate = AsyncMock(side_effect=RuntimeError("bad llm"))
            from hft_platform.reports.pipeline import build_hybrid_report_async

            result = await build_hybrid_report_async("day", "2026-03-28", "TXFD6")

        assert result.composed is not None
        assert result.decision is None
        assert result.llm_error == "bad llm"

    @pytest.mark.asyncio
    async def test_async_wrapper_skips_llm_cleanly_when_disabled(self, monkeypatch: pytest.MonkeyPatch, mock_session_data, mock_fact_report, mock_reasoning_report):
        monkeypatch.setenv("HFT_LLM_ENABLED", "0")
        mock_collector = MagicMock()
        mock_collector.collect.return_value = mock_session_data
        mock_collector.collect_cross_day.return_value = []

        mock_composer_inst = MagicMock()
        mock_composer_inst.compose.return_value = _make_composed()

        with (
            patch(_PATCH_COLLECTOR, return_value=mock_collector),
            patch(_PATCH_EXTRACT, return_value=mock_fact_report),
            patch(_PATCH_REASON, return_value=mock_reasoning_report),
            patch(_PATCH_COMPOSER, return_value=mock_composer_inst),
            patch("hft_platform.reports.pipeline.LLMReportReasoner") as MockReasoner,
        ):
            from hft_platform.reports.pipeline import build_hybrid_report_async

            result = await build_hybrid_report_async("day", "2026-03-28", "TXFD6")

        assert result.composed is not None
        assert result.decision is None
        assert result.llm_error is None
        MockReasoner.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_report_pipeline_build.py::TestBuildHybridReport -v`
Expected: FAIL because `build_hybrid_report_async` does not exist

- [ ] **Step 3: Write minimal implementation**

Add to `src/hft_platform/reports/pipeline.py` module scope:

```python
from dataclasses import dataclass

from hft_platform.reports.composer import ReportComposer
from hft_platform.reports.llm_client import OpenRouterClient
from hft_platform.reports.llm_dossier import build_llm_dossier
from hft_platform.reports.llm_reasoner import LLMReportReasoner


@dataclass(frozen=True, slots=True)
class HybridReportResult:
    composed: ComposedReport | None
    dossier: object | None
    decision: object | None
    llm_error: str | None


async def build_hybrid_report_async(
    session: str,
    date: str,
    symbol: str = "TXFD6",
) -> HybridReportResult:
    fact_report, reasoning_report, composed = await asyncio.to_thread(_build_report_components, session, date, symbol)
    if composed is None:
        return HybridReportResult(composed=None, dossier=None, decision=None, llm_error=None)

    if os.environ.get("HFT_LLM_ENABLED", "0") != "1":
        return HybridReportResult(composed=composed, dossier=None, decision=None, llm_error=None)

    try:
        dossier = build_llm_dossier(fact_report, reasoning_report)
        decision = await LLMReportReasoner(client=OpenRouterClient(model=os.environ.get("HFT_LLM_MODEL", ""))).generate(dossier)
        composed = ReportComposer().compose(fact_report, reasoning_report, llm_decision=decision)
        return HybridReportResult(composed=composed, dossier=dossier, decision=decision, llm_error=None)
    except Exception as exc:
        _log.warning("build_hybrid_report_llm_fallback", symbol=symbol, session=session, date=date, llm_error=str(exc))
        return HybridReportResult(composed=composed, dossier=None, decision=None, llm_error=str(exc))


def _build_report_components(session: str, date: str, symbol: str):
    from hft_platform.reports.collector import DataCollector
    from hft_platform.reports.composer import ReportComposer
    from hft_platform.reports.facts import extract_all
    from hft_platform.reports.reasoner import reason_all

    collector = DataCollector()
    session_data = collector.collect(session, date, symbol)
    if session_data.tick_count == 0:
        return None, None, None
    prev_days = collector.collect_cross_day(symbol, session, date)
    fact_report = extract_all(session_data, prev_days=prev_days)
    reasoning_report = reason_all(fact_report)
    composed = ReportComposer().compose(fact_report, reasoning_report)
    return fact_report, reasoning_report, composed
```

Then make `build_report()` call `_build_report_components()` so deterministic and hybrid paths share the same sync core.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_pipeline_build.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/pipeline.py tests/unit/test_report_pipeline_build.py
git commit -m "feat(reports): add async hybrid report orchestration"
```

---

### Task 6: Extend composer with optional LLM decision sections

**Files:**
- Modify: `src/hft_platform/reports/composer.py`
- Test: `tests/unit/test_report_composer.py`

- [ ] **Step 1: Write the failing tests**

Extend `tests/unit/test_report_composer.py`:

```python
from hft_platform.reports.llm_models import EvidenceRef, LLMDecisionReport, TradePlan


def _make_llm_decision() -> LLMDecisionReport:
    return LLMDecisionReport(
        market_verdict="偏多延續",
        intraday_plan=TradePlan("bullish", "closing flow held", "hold above S1", "buy pullback", "lose S1", "R1", "R2", "avoid chasing"),
        swing_plan=TradePlan("bullish", "trend remains up", "daily close above R1", "hold partial", "close below S1", "R2", "R3", "cut ahead of event risk"),
        key_levels=["S1 22,300", "R1 22,440"],
        invalidations=["lose S1 with expanding sell flow"],
        counter_case="opening rejection and failed reclaim turns thesis wrong",
        execution_notes=["only buy pullbacks"],
        confidence=72,
        evidence_refs=[EvidenceRef(key="flow.session_ud", detail="1.18")],
    )


class TestComposeWithLLM:
    def test_inserts_llm_sections_before_disclaimer(self) -> None:
        cr = ReportComposer().compose(_make_fact_report(), _make_reasoning_report(), llm_decision=_make_llm_decision())
        text_parts = [m.content for m in cr.messages if m.kind == "text"]
        joined = "\n".join(text_parts)
        assert "LLM 市場裁決" in joined
        assert "當日交易計畫" in joined
        assert "1-3 日波段觀點" in joined
        assert "失效條件" in joined

    def test_works_without_llm_decision(self) -> None:
        cr = ReportComposer().compose(_make_fact_report(), _make_reasoning_report(), llm_decision=None)
        text_parts = [m.content for m in cr.messages if m.kind == "text"]
        joined = "\n".join(text_parts)
        assert "LLM 市場裁決" not in joined
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_report_composer.py::TestComposeWithLLM -v`
Expected: FAIL because `compose()` does not accept `llm_decision`

- [ ] **Step 3: Write minimal implementation**

Change `ReportComposer.compose()` signature:

```python
    def compose(self, fr: FactReport, rr: ReasoningReport, llm_decision=None) -> ComposedReport:
```

Add optional helper:

```python
    def _compose_llm_decision(self, decision) -> str:
        lines = [
            "🧠 LLM 市場裁決",
            "",
            f"裁決：{decision.market_verdict}",
            f"信心：{decision.confidence}%",
            "",
            "當日交易計畫",
            f"  方向：{decision.intraday_plan.stance}",
            f"  觸發：{decision.intraday_plan.trigger}",
            f"  執行：{decision.intraday_plan.execution_style}",
            f"  停損：{decision.intraday_plan.stop}",
            f"  目標：{decision.intraday_plan.target_1} / {decision.intraday_plan.target_2}",
            "",
            "1-3 日波段觀點",
            f"  方向：{decision.swing_plan.stance}",
            f"  觸發：{decision.swing_plan.trigger}",
            f"  停損：{decision.swing_plan.stop}",
            "",
            "關鍵價位",
            *[f"  - {level}" for level in decision.key_levels],
            "",
            "失效條件",
            *[f"  - {item}" for item in decision.invalidations],
            "",
            f"反方論點：{decision.counter_case}",
            "執行備註",
            *[f"  - {item}" for item in decision.execution_notes],
        ]
        return "\n".join(lines)
```

Insert it after the free summary block and before deeper paid detail blocks:

```python
        parts.extend(_split_message(self._compose_summary(fr, rr), "free"))
        if llm_decision is not None:
            parts.extend(_split_message(self._compose_llm_decision(llm_decision), "paid"))
        parts.extend(_split_message(self._compose_narrative(rr), "paid"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_composer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/composer.py tests/unit/test_report_composer.py
git commit -m "feat(reports): compose llm decision sections into telegram report"
```

---

### Task 7: Add bot cache, `/report_rule`, and bounded `/ask`

**Files:**
- Modify: `src/hft_platform/bot/app.py`
- Modify: `src/hft_platform/bot/handlers.py`
- Test: `tests/unit/test_bot_app.py`
- Test: `tests/unit/test_bot_handlers.py`

- [ ] **Step 1: Write the failing tests**

Extend `tests/unit/test_bot_handlers.py`:

```python
class TestRuleOnlyAndAsk:
    @pytest.mark.asyncio
    async def test_report_rule_uses_deterministic_path_only(self) -> None:
        from hft_platform.bot.handlers import cmd_report_rule

        update = _make_update(chat_id=12345, text="/report_rule day")
        ctx = _make_context()
        ctx.args = ["day"]

        with (
            patch("hft_platform.reports.pipeline.build_report") as mock_build,
            patch("hft_platform.reports.pipeline.build_hybrid_report_async") as mock_hybrid,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = _make_composed(["rule-only"])
            mock_asyncio.sleep = AsyncMock()
            await cmd_report_rule(update, ctx)

        mock_build.assert_called_once()
        mock_hybrid.assert_not_called()

    @pytest.mark.asyncio
    async def test_report_caches_latest_manual_hybrid_context(self) -> None:
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report day")
        ctx = _make_context()
        ctx.args = ["day"]

        hybrid_result = MagicMock()
        hybrid_result.composed = _make_composed(["hybrid"])
        hybrid_result.dossier = MagicMock(symbol="TXFD6", session="day", date="2026-04-07")
        hybrid_result.decision = MagicMock()
        hybrid_result.llm_error = None

        with (
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock(return_value=hybrid_result)),
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)

        assert bot_app.latest_manual_report_context is not None
        assert bot_app.latest_manual_report_context.symbol == "TXFD6"

    @pytest.mark.asyncio
    async def test_ask_rejects_when_no_manual_hybrid_context(self) -> None:
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_ask

        bot_app.latest_manual_report_context = None
        update = _make_update(chat_id=12345, text="/ask 現在還能追嗎")
        ctx = _make_context()
        ctx.args = ["現在還能追嗎"]

        await cmd_ask(update, ctx)
        update.message.reply_text.assert_called_once()
        assert "先執行 /report" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_ask_rejects_when_latest_context_has_no_decision(self) -> None:
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_ask

        bot_app.latest_manual_report_context = bot_app.LatestReportContext(
            symbol="TXFD6",
            session="day",
            date="2026-04-07",
            dossier=MagicMock(),
            decision=None,
        )
        update = _make_update(chat_id=12345, text="/ask 還能追嗎")
        ctx = _make_context()
        ctx.args = ["還能追嗎"]

        await cmd_ask(update, ctx)
        assert "先重新執行 /report" in update.message.reply_text.call_args[0][0]
```

Also add to `tests/unit/test_bot_app.py`:

```python
def test_latest_report_context_dataclass_roundtrip() -> None:
    import hft_platform.bot.app as bot_app

    ctx = bot_app.LatestReportContext(
        symbol="TXFD6",
        session="day",
        date="2026-04-07",
        dossier=object(),
        decision=object(),
    )
    assert ctx.symbol == "TXFD6"


def test_latest_manual_report_context_defaults_to_none() -> None:
    import hft_platform.bot.app as bot_app
    assert bot_app.latest_manual_report_context is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot_app.py tests/unit/test_bot_handlers.py -v`
Expected: FAIL because cache state and new commands do not exist

- [ ] **Step 3: Write minimal implementation**

Add to `src/hft_platform/bot/app.py` near other shared state:

```python
from dataclasses import dataclass


@dataclass(slots=True)
class LatestReportContext:
    symbol: str
    session: str
    date: str
    dossier: object
    decision: object | None


latest_manual_report_context = None
```

Register new commands in `create_app()`:

```python
    from hft_platform.bot.handlers import (
        cmd_ask,
        cmd_flow,
        cmd_levels,
        cmd_report,
        cmd_report_rule,
        cmd_start,
        cmd_status,
    )

    app.add_handler(CommandHandler("report_rule", cmd_report_rule))
    app.add_handler(CommandHandler("ask", cmd_ask))
```

Add to `src/hft_platform/bot/handlers.py`:

```python
@owner_only
async def cmd_report_rule(update: Any, context: Any) -> None:
    symbol, session = _parse_report_args(context.args or [])
    if session is None:
        now = datetime.now(_TZ)
        session = "day" if 7 <= now.hour < 15 else "night"
    date = resolve_trading_date(session)
    await update.message.reply_text(f"產生規則版報告中... ({symbol} {session} {date})")
    composed = build_report(session, date, symbol)
    if composed is None:
        await update.message.reply_text("該時段無交易資料")
        return
    await _send_composed(update, context, composed)


@owner_only
async def cmd_ask(update: Any, context: Any) -> None:
    import hft_platform.bot.app as bot_app

    if bot_app.latest_manual_report_context is None:
        await update.message.reply_text("目前沒有可追問的 hybrid 報告，請先執行 /report")
        return
    if bot_app.latest_manual_report_context.decision is None:
        await update.message.reply_text("最近一次 /report 沒有成功產生 LLM 判讀，請先重新執行 /report")
        return
    question = " ".join(context.args or []).strip()
    if not question:
        await update.message.reply_text("用法：/ask <問題>")
        return
    answer = await answer_followup_question(bot_app.latest_manual_report_context, question)
    await update.message.reply_text(answer, parse_mode="HTML")
```

Refactor shared send helper:

```python
async def _send_composed(update: Any, context: Any, composed: ComposedReport) -> None:
    chat_id = update.effective_chat.id
    for i, part in enumerate(composed.messages):
        if part.kind == "text":
            await context.bot.send_message(chat_id=chat_id, text=part.content, parse_mode="HTML")
        elif part.kind == "image" and part.image is not None:
            await context.bot.send_photo(chat_id=chat_id, photo=io.BytesIO(part.image), caption=part.caption)
        if i < len(composed.messages) - 1:
            await asyncio.sleep(1.5)
```

Update `cmd_report` to call `build_hybrid_report_async()`, cache only when `decision is not None`, and leave cache untouched on scheduler/fallback paths.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_bot_app.py tests/unit/test_bot_handlers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/bot/app.py src/hft_platform/bot/handlers.py tests/unit/test_bot_app.py tests/unit/test_bot_handlers.py
git commit -m "feat(bot): add hybrid report cache and ask flow"
```

---

### Task 8: Add follow-up answer generation, scheduler hybrid sends, and end-to-end fallback coverage

**Files:**
- Modify: `src/hft_platform/reports/llm_reasoner.py`
- Modify: `src/hft_platform/bot/scheduler.py`
- Modify: `tests/unit/test_bot_scheduler.py`
- Modify: `tests/unit/test_report_integration.py`

- [ ] **Step 1: Write the failing tests**

Extend `tests/unit/test_bot_scheduler.py`:

```python
class TestHybridPush:
    @pytest.mark.asyncio
    async def test_push_uses_hybrid_async_builder(self) -> None:
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.send_photo = AsyncMock()

        hybrid_result = MagicMock()
        hybrid_result.composed = _make_composed(["hybrid"])
        hybrid_result.decision = MagicMock()
        hybrid_result.dossier = MagicMock()
        hybrid_result.llm_error = None

        with (
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock(return_value=hybrid_result)) as mock_hybrid,
            patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await _push_report(ctx, "day")

        mock_hybrid.assert_awaited()

    @pytest.mark.asyncio
    async def test_push_fallback_does_not_touch_manual_ask_cache(self) -> None:
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _push_report

        existing = MagicMock(symbol="TXFD6")
        bot_app.latest_manual_report_context = existing

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.send_photo = AsyncMock()

        hybrid_result = MagicMock()
        hybrid_result.composed = _make_composed(["rule-only"])
        hybrid_result.decision = None
        hybrid_result.dossier = None
        hybrid_result.llm_error = "timeout"

        with (
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock(return_value=hybrid_result)),
            patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await _push_report(ctx, "day")

        assert bot_app.latest_manual_report_context is existing
```

Extend `tests/unit/test_report_integration.py`:

```python
class TestHybridPipelineIntegration:
    @pytest.mark.asyncio
    async def test_hybrid_pipeline_falls_back_to_deterministic_report(self):
        sd = _build_fixture_session()
        mock_collector = MagicMock()
        mock_collector.collect = MagicMock(return_value=sd)
        mock_collector.collect_cross_day = MagicMock(return_value=[])

        with (
            patch("hft_platform.reports.collector.DataCollector", return_value=mock_collector),
            patch("hft_platform.reports.pipeline.LLMReportReasoner") as MockReasoner,
            patch("hft_platform.reports.pipeline.OpenRouterClient") as MockClient,
        ):
            MockReasoner.return_value.generate = AsyncMock(side_effect=RuntimeError("llm down"))
            from hft_platform.reports.pipeline import build_hybrid_report_async

            result = await build_hybrid_report_async("day", "2026-03-27", "TXFD6")

        assert result.composed is not None
        assert result.decision is None
        assert result.llm_error == "llm down"

    @pytest.mark.asyncio
    async def test_openrouter_shaped_payload_is_decoded_before_reasoner_validation(self):
        from hft_platform.reports.llm_client import OpenRouterClient

        fake_response = MagicMock()
        fake_response.status = 200
        fake_response.json = AsyncMock(return_value={
            "choices": [{"message": {"content": "{\"market_verdict\": \"偏多\", \"intraday_plan\": {\"stance\": \"bullish\", \"premise\": \"p\", \"trigger\": \"t\", \"execution_style\": \"e\", \"stop\": \"s\", \"target_1\": \"t1\", \"target_2\": \"t2\", \"risk_note\": \"r\"}, \"swing_plan\": {\"stance\": \"bullish\", \"premise\": \"p\", \"trigger\": \"t\", \"execution_style\": \"e\", \"stop\": \"s\", \"target_1\": \"t1\", \"target_2\": \"t2\", \"risk_note\": \"r\"}, \"key_levels\": [], \"invalidations\": [\"lose S1\"], \"counter_case\": \"counter\", \"execution_notes\": [], \"confidence\": 60, \"evidence_refs\": [{\"key\": \"flow.session_ud\", \"detail\": \"1.18\"}]}"}}]
        })
        fake_response.__aenter__ = AsyncMock(return_value=fake_response)
        fake_response.__aexit__ = AsyncMock(return_value=None)

        fake_session = MagicMock()
        fake_session.post.return_value = fake_response

        client = OpenRouterClient(model="demo-model", api_key="secret", base_url="https://openrouter.ai/api/v1")
        parsed = await client.complete_json_from_session(fake_session, "prompt")
        assert parsed["market_verdict"] == "偏多"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot_scheduler.py tests/unit/test_report_integration.py -v`
Expected: FAIL because scheduler still uses `build_report()` only and no follow-up answer path exists

- [ ] **Step 3: Write minimal implementation**

In `src/hft_platform/reports/llm_reasoner.py`, add bounded follow-up helper:

```python
    async def answer_followup(self, dossier, decision, question: str) -> str:
        payload = await self._client.complete_json(
            "Answer only from the supplied report context.\n"
            f"Question: {question}\n"
            f"Evidence: {json.dumps(dossier.evidence, ensure_ascii=False)}\n"
            f"Verdict: {decision.market_verdict}"
        )
        answer = payload.get("answer", "").strip()
        if not answer:
            raise ValueError("Empty follow-up answer")
        return answer
```

In `src/hft_platform/bot/scheduler.py`, replace `build_report(...)` with `await build_hybrid_report_async(...)`, send `result.composed`, and never mutate `latest_manual_report_context`.

In `src/hft_platform/bot/handlers.py`, add small wrapper:

```python
async def answer_followup_question(latest_context, question: str) -> str:
    from hft_platform.reports.llm_client import OpenRouterClient
    from hft_platform.reports.llm_reasoner import LLMReportReasoner

    reasoner = LLMReportReasoner(client=OpenRouterClient(model=os.environ.get("HFT_LLM_MODEL", "")))
    return await reasoner.answer_followup(latest_context.dossier, latest_context.decision, question)
```

- [ ] **Step 4: Run focused and broad verification**

Run:

```bash
uv run pytest tests/unit/test_report_llm_models.py \
  tests/unit/test_report_llm_dossier.py \
  tests/unit/test_report_llm_client.py \
  tests/unit/test_report_llm_reasoner.py \
  tests/unit/test_report_pipeline_build.py \
  tests/unit/test_report_composer.py \
  tests/unit/test_bot_app.py \
  tests/unit/test_bot_handlers.py \
  tests/unit/test_bot_scheduler.py \
  tests/unit/test_report_integration.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/llm_reasoner.py src/hft_platform/bot/scheduler.py src/hft_platform/bot/handlers.py tests/unit/test_bot_scheduler.py tests/unit/test_report_integration.py
git commit -m "feat(bot): wire hybrid llm reports into scheduler and ask flow"
```

---

## Final Verification

- [ ] Run: `uv run pytest tests/unit/test_report_llm_models.py tests/unit/test_report_llm_dossier.py tests/unit/test_report_llm_client.py tests/unit/test_report_llm_reasoner.py tests/unit/test_report_pipeline_build.py tests/unit/test_report_composer.py tests/unit/test_bot_app.py tests/unit/test_bot_handlers.py tests/unit/test_bot_scheduler.py tests/unit/test_report_integration.py -v`
  Expected: all targeted report/bot/LLM tests pass.

- [ ] Run: `uv run pytest tests/unit/test_report_pipeline.py tests/unit/test_report_models.py tests/unit/test_report_reasoner.py tests/unit/test_report_facts.py -v`
  Expected: existing deterministic report tests still pass.

- [ ] Manual smoke:

```bash
HFT_LLM_ENABLED=0 uv run python -m hft_platform.reports --session day --date 2026-03-27 --dry-run --debug
```

Expected: deterministic report still builds without the LLM layer.

- [ ] Manual smoke with bot command path:

```bash
HFT_TELEGRAM_BOT_TOKEN=fake HFT_TELEGRAM_CHAT_ID=12345 HFT_LLM_ENABLED=1 HFT_LLM_MODEL=test-model uv run pytest tests/unit/test_bot_handlers.py::TestRuleOnlyAndAsk -v
```

Expected: hybrid command path and `/ask` state handling both pass.

---

## Notes for the Implementer

- Keep `build_report()` intact for deterministic callers; layer async orchestration around it rather than replacing it.
- Do not cache scheduled-push contexts for `/ask`.
- Reject follow-up questions if there is no validated manual hybrid report context.
- Validate every evidence ref against dossier keys before accepting model output.
- Keep OpenRouter-specific HTTP details inside `llm_client.py`.
- Do not expand into free/paid monetization work in this phase.
