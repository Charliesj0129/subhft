"""Async OpenRouter client for JSON-only report completions."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

try:
    import aiohttp
except ImportError:  # pragma: no cover - optional at import time for unit tests
    aiohttp = None

if TYPE_CHECKING:
    from aiohttp import ClientSession as AiohttpClientSession

__all__ = ["OpenRouterClient"]

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _transient_exception_types() -> tuple[type[BaseException], ...]:
    transient: list[type[BaseException]] = [asyncio.TimeoutError, ConnectionError, OSError]
    if aiohttp is not None:
        transient.append(aiohttp.ClientError)
    return tuple(dict.fromkeys(transient))


class OpenRouterClient:
    """Minimal async client for JSON completions via OpenRouter."""

    __slots__ = ("_api_key", "_base_url", "_max_retries", "_model", "_timeout_s")

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 1,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries

    def _headers(self) -> dict[str, str]:
        api_key = self._api_key or os.environ.get("HFT_LLM_API_KEY", "").strip()
        if not api_key:
            msg = "HFT_LLM_API_KEY is required for OpenRouterClient"
            raise RuntimeError(msg)
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    async def _request_json(
        self,
        session: AiohttpClientSession | Any,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        url = f"{self._base_url}/chat/completions"
        headers = self._headers()
        transient_errors = _transient_exception_types()

        for attempt in range(self._max_retries + 1):
            try:
                async with session.post(url, json=payload, headers=headers, timeout=self._timeout_s) as response:
                    if response.status in _RETRYABLE_STATUSES:
                        if attempt < self._max_retries:
                            await asyncio.sleep(2**attempt)
                            continue
                        msg = f"OpenRouter request failed with retryable status {response.status}"
                        raise RuntimeError(msg)

                    if not 200 <= response.status < 300:
                        msg = f"OpenRouter request failed with status {response.status}"
                        raise RuntimeError(msg)

                    try:
                        body = await response.json()
                    except Exception as exc:  # noqa: BLE001
                        msg = "OpenRouter response body was not valid JSON"
                        raise RuntimeError(msg) from exc
            except RuntimeError:
                raise
            except transient_errors as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                msg = "OpenRouter transport failed after retries"
                raise RuntimeError(msg) from exc
            except Exception as exc:  # noqa: BLE001
                msg = "OpenRouter request failed before receiving a response"
                raise RuntimeError(msg) from exc

            if not isinstance(body, dict):
                msg = "OpenRouter response body must be an object"
                raise RuntimeError(msg)
            return body

        msg = "OpenRouter request exhausted retries"
        raise RuntimeError(msg)

    async def complete_json_from_session(
        self,
        session: AiohttpClientSession | Any,
        prompt: str,
    ) -> dict[str, object]:
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        body = await self._request_json(session, payload)

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            msg = "OpenRouter response missing message content"
            raise RuntimeError(msg) from exc

        if not isinstance(content, str):
            msg = "OpenRouter response content must be a JSON string"
            raise RuntimeError(msg)

        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as exc:
            msg = "OpenRouter response content is not valid JSON"
            raise RuntimeError(msg) from exc

        if not isinstance(decoded, dict):
            msg = "OpenRouter response content must decode to a JSON object"
            raise RuntimeError(msg)
        return decoded

    async def complete_json(self, prompt: str) -> dict[str, object]:
        if aiohttp is None:
            msg = "aiohttp is required for OpenRouterClient.complete_json"
            raise RuntimeError(msg)
        async with aiohttp.ClientSession() as session:
            return await self.complete_json_from_session(session, prompt)
