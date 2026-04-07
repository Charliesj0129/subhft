"""Unit tests for the async OpenRouter client."""

from __future__ import annotations

import asyncio

import pytest

from hft_platform.reports.llm_client import OpenRouterClient


class _FakeResponse:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def json(self) -> object:
        return self._body


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, *, json: object, headers: dict[str, str], timeout: float) -> _FakeResponse:
        self.calls.append(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if not self._responses:
            msg = "no fake responses configured"
            raise AssertionError(msg)
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_complete_json_from_session_decodes_openrouter_payload() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"market_verdict":"bullish","confidence":78}',
                            }
                        }
                    ]
                },
            )
        ]
    )
    client = OpenRouterClient(model="openrouter/test-model", api_key="test-key")

    result = await client.complete_json_from_session(session, "prompt")

    assert result["market_verdict"] == "bullish"
    assert session.calls == [
        {
            "url": "https://openrouter.ai/api/v1/chat/completions",
            "json": {
                "model": "openrouter/test-model",
                "messages": [{"role": "user", "content": "prompt"}],
                "response_format": {"type": "json_object"},
            },
            "headers": {
                "Authorization": "Bearer test-key",
                "Content-Type": "application/json",
            },
            "timeout": 30.0,
        }
    ]


def test_headers_raise_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HFT_LLM_API_KEY", raising=False)
    client = OpenRouterClient(model="openrouter/test-model")

    with pytest.raises(RuntimeError, match="HFT_LLM_API_KEY"):
        client._headers()


@pytest.mark.asyncio
async def test_request_json_retries_once_after_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(
        [
            _FakeResponse(429, {"error": {"message": "rate limited"}}),
            _FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"market_verdict":"neutral","confidence":55}',
                            }
                        }
                    ]
                },
            ),
        ]
    )
    client = OpenRouterClient(model="openrouter/test-model", api_key="test-key", max_retries=1)
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    result = await client.complete_json_from_session(session, "prompt")

    assert result["market_verdict"] == "neutral"
    assert len(session.calls) == 2
    assert sleep_calls == [1]
