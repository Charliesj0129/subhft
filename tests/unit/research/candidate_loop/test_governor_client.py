"""DeepSeek client: fail-closed on missing key, JSONL extraction, redaction, retries."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from research.candidate_loop.governor.client import DeepSeekClient, DeepSeekError
from research.candidate_loop.governor.signals import load_governor_config

CFG = load_governor_config(
    Path(__file__).resolve().parents[4]
    / "config" / "research" / "candidate_loop" / "governor_v1.yaml"
)


def _completion(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _two_candidate_content() -> str:
    return (
        '{"family":"trade_flow","name":"tf_a","formula":"x"}\n'
        '{"family":"trade_flow","name":"tf_b","formula":"y"}'
    )


def test_missing_api_key_fails_closed(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(DeepSeekError):
        DeepSeekClient(CFG)


def test_generate_candidates_extracts_jsonl_lines():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(_two_candidate_content()))

    client = DeepSeekClient(
        CFG, api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    lines = client.generate_candidates(base_prompt="P", brief_body="B", n=2)
    assert len(lines) == 2
    assert json.loads(lines[0])["name"] == "tf_a"


def test_generate_candidates_strips_markdown_fences():
    content = "```json\n" + _two_candidate_content() + "\n```"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(content))

    client = DeepSeekClient(
        CFG, api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    assert len(client.generate_candidates(base_prompt="P", brief_body="B", n=2)) == 2


def test_non_json_line_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("not json at all"))

    client = DeepSeekClient(
        CFG, api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(DeepSeekError):
        client.generate_candidates(base_prompt="P", brief_body="B", n=1)


def test_redact_removes_api_key_from_text():
    client = DeepSeekClient(
        CFG, api_key="sk-supersecret", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    assert "sk-supersecret" not in client.redact("boom sk-supersecret here")


def test_retries_then_succeeds_within_bound():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": "transient"})
        return httpx.Response(200, json=_completion(_two_candidate_content()))

    client = DeepSeekClient(
        CFG, api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    lines = client.generate_candidates(base_prompt="P", brief_body="B", n=2)
    assert len(lines) == 2
    assert calls["n"] == 2  # one retry consumed
