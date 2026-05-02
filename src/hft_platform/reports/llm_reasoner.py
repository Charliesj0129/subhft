"""Business-boundary reasoner for LLM decision reports."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from hft_platform.reports.llm_client import OpenRouterClient
from hft_platform.reports.llm_models import EvidenceRef, LLMDecisionReport, LLMDossier, TradePlan

__all__ = ["LLMReportReasoner", "answer_followup_question"]


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        msg = f"{field_name} must be an object"
        raise ValueError(msg)
    invalid_keys = [key for key in value if not isinstance(key, str)]
    if invalid_keys:
        msg = f"{field_name} must use string keys"
        raise ValueError(msg)
    return value


def _require_sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)):
        msg = f"{field_name} must be a JSON array"
        raise ValueError(msg)
    if not isinstance(value, Sequence):
        msg = f"{field_name} must be a JSON array"
        raise ValueError(msg)
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        msg = f"{field_name} must be a string"
        raise ValueError(msg)
    return value


def _require_int(value: object, field_name: str) -> int:
    if type(value) is not int:
        msg = f"{field_name} must be an int"
        raise ValueError(msg)
    return value


def _coerce_text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    return tuple(
        _require_text(item, f"{field_name}[{index}]") for index, item in enumerate(_require_sequence(value, field_name))
    )


def _coerce_trade_plan(value: object, field_name: str) -> TradePlan:
    payload = _require_mapping(value, field_name)
    return TradePlan(
        stance=_require_text(payload.get("stance"), f"{field_name}.stance"),
        premise=_require_text(payload.get("premise"), f"{field_name}.premise"),
        trigger=_require_text(payload.get("trigger"), f"{field_name}.trigger"),
        execution_style=_require_text(payload.get("execution_style"), f"{field_name}.execution_style"),
        stop=_require_text(payload.get("stop"), f"{field_name}.stop"),
        target_1=_require_text(payload.get("target_1"), f"{field_name}.target_1"),
        target_2=_require_text(payload.get("target_2"), f"{field_name}.target_2"),
        risk_note=_require_text(payload.get("risk_note"), f"{field_name}.risk_note"),
    )


def _coerce_evidence_refs(value: object) -> tuple[EvidenceRef, ...]:
    refs: list[EvidenceRef] = []
    for index, item in enumerate(_require_sequence(value, "evidence_refs")):
        payload = _require_mapping(item, f"evidence_refs[{index}]")
        refs.append(
            EvidenceRef(
                key=_require_text(payload.get("key"), f"evidence_refs[{index}].key"),
                detail=_require_text(payload.get("detail"), f"evidence_refs[{index}].detail"),
            )
        )
    return tuple(refs)


class LLMReportReasoner:
    """Convert parsed client JSON into validated report contracts."""

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    def _build_prompt(self, dossier: LLMDossier) -> str:
        dossier.validate()
        serialized_dossier = json.dumps(
            {
                "symbol": dossier.symbol,
                "session": dossier.session,
                "date": dossier.date,
                "evidence": dict(dossier.evidence),
                "narrative": list(dossier.narrative),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return (
            "You are producing a trading decision report.\n"
            "Use only the dossier evidence provided below. Do not invent facts, levels, or evidence refs.\n"
            "Return JSON only. Do not wrap the JSON in markdown.\n"
            "The JSON object must contain these keys: "
            "market_verdict, intraday_plan, swing_plan, key_levels, invalidations, "
            "counter_case, execution_notes, confidence, evidence_refs.\n"
            "Each evidence_refs item must contain key and detail, and every key must come from dossier.evidence.\n"
            f"Symbol: {dossier.symbol}\n"
            f"Session: {dossier.session}\n"
            f"Evidence JSON: {serialized_dossier}"
        )

    def _build_followup_prompt(
        self,
        dossier: LLMDossier,
        decision: LLMDecisionReport,
        question: str,
    ) -> str:
        serialized_decision = json.dumps(
            {
                "market_verdict": decision.market_verdict,
                "intraday_plan": asdict(decision.intraday_plan),
                "swing_plan": asdict(decision.swing_plan),
                "key_levels": list(decision.key_levels),
                "invalidations": list(decision.invalidations),
                "counter_case": decision.counter_case,
                "execution_notes": list(decision.execution_notes),
                "confidence": decision.confidence,
                "evidence_refs": [asdict(ref) for ref in decision.evidence_refs],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return (
            "Answer only from the supplied report context. Do not invent facts.\n"
            "Return JSON only with keys: answer, evidence_refs.\n"
            "evidence_refs must be a non-empty array of keys from dossier.evidence.\n"
            f"Question: {question}\n"
            f"Evidence: {json.dumps(dict(dossier.evidence), ensure_ascii=False, sort_keys=True)}\n"
            f"Decision JSON: {serialized_decision}"
        )

    async def generate(self, dossier: LLMDossier) -> LLMDecisionReport:
        dossier.validate()
        prompt = self._build_prompt(dossier)
        payload = _require_mapping(await self._client.complete_json(prompt), "payload")
        report = LLMDecisionReport(
            market_verdict=_require_text(payload.get("market_verdict"), "market_verdict"),
            intraday_plan=_coerce_trade_plan(payload.get("intraday_plan"), "intraday_plan"),
            swing_plan=_coerce_trade_plan(payload.get("swing_plan"), "swing_plan"),
            key_levels=_coerce_text_tuple(payload.get("key_levels"), "key_levels"),
            invalidations=_coerce_text_tuple(payload.get("invalidations"), "invalidations"),
            counter_case=_require_text(payload.get("counter_case"), "counter_case"),
            execution_notes=_coerce_text_tuple(payload.get("execution_notes"), "execution_notes"),
            confidence=_require_int(payload.get("confidence"), "confidence"),
            evidence_refs=_coerce_evidence_refs(payload.get("evidence_refs")),
        )
        report.validate()
        unknown_refs = tuple(ref.key for ref in report.evidence_refs if ref.key not in dossier.evidence)
        if unknown_refs:
            msg = f"unknown evidence refs: {', '.join(unknown_refs)}"
            raise ValueError(msg)
        return report

    async def answer_followup(
        self,
        dossier: LLMDossier,
        decision: LLMDecisionReport,
        question: str,
    ) -> str:
        dossier.validate()
        decision.validate()
        question_text = question.strip()
        if not question_text:
            msg = "question must be non-empty"
            raise ValueError(msg)

        payload = _require_mapping(
            await self._client.complete_json(self._build_followup_prompt(dossier, decision, question_text)),
            "payload",
        )
        answer = _require_text(payload.get("answer"), "answer").strip()
        if not answer:
            msg = "answer must be non-empty"
            raise ValueError(msg)
        evidence_refs = _coerce_text_tuple(payload.get("evidence_refs"), "evidence_refs")
        if not evidence_refs:
            msg = "evidence_refs must be non-empty"
            raise ValueError(msg)
        unknown_refs = tuple(ref for ref in evidence_refs if ref not in dossier.evidence)
        if unknown_refs:
            msg = f"unknown evidence refs: {', '.join(unknown_refs)}"
            raise ValueError(msg)
        return answer


async def answer_followup_question(report_context: Any, question: str) -> str:
    dossier = getattr(report_context, "dossier", None)
    decision = getattr(report_context, "decision", None)
    if dossier is None or decision is None:
        msg = "report context must include dossier and decision"
        raise ValueError(msg)

    client = OpenRouterClient(model=os.environ.get("HFT_LLM_MODEL", ""))
    reasoner = LLMReportReasoner(client=client)
    return await reasoner.answer_followup(dossier, decision, question)
