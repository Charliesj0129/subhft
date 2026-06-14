"""Thin DeepSeek client (OpenAI-compatible chat/completions).

Security: ``DEEPSEEK_API_KEY`` from the environment only; never logged or stored
beyond the in-memory client; redacted from any raised message; TLS verify on
(httpx default). Fails closed when the key is missing. Content validation of
candidates is deferred to the runner — the client only enforces the JSONL SHAPE
(one JSON object per line), matching ``generate.ingest_jsonl``.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from research.candidate_loop.governor.signals import GovernorConfig

API_KEY_ENV = "DEEPSEEK_API_KEY"


class DeepSeekError(RuntimeError):
    """A DeepSeek call or its response violated the client contract."""


def _extract_jsonl(content: str) -> list[str]:
    lines: list[str] = []
    for raw in content.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("```"):
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeepSeekError(f"model returned a non-JSON line: {raw[:80]!r}") from exc
        if not isinstance(obj, dict):
            raise DeepSeekError(f"model line is not a JSON object: {raw[:80]!r}")
        lines.append(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return lines


class DeepSeekClient:
    def __init__(
        self,
        cfg: GovernorConfig,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._cfg = cfg
        self._api_key = api_key or os.environ.get(API_KEY_ENV, "")
        if not self._api_key:
            raise DeepSeekError(
                f"{API_KEY_ENV} not set; refusing to call DeepSeek unauthenticated"
            )
        self._client = httpx.Client(
            base_url=cfg.base_url,
            timeout=cfg.timeout_seconds,
            transport=transport,  # None → real network with TLS verify on
        )

    def redact(self, text: str) -> str:
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, "***")
        return text

    def _chat(self, base_prompt: str, brief_body: str, n: int) -> str:
        payload: dict[str, Any] = {
            "model": self._cfg.model_name,
            "messages": [
                {"role": "system", "content": base_prompt},
                {
                    "role": "user",
                    "content": f"{brief_body}\n\nEmit exactly {n} JSONL candidate lines now.",
                },
            ],
            "max_tokens": self._cfg.max_tokens,
            "temperature": self._cfg.temperature,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last_exc: Exception | None = None
        for _ in range(self._cfg.max_retries + 1):
            try:
                resp = self._client.post("/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return str(data["choices"][0]["message"]["content"])
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                last_exc = exc
        raise DeepSeekError(self.redact(f"DeepSeek request failed: {last_exc}"))

    def generate_candidates(self, *, base_prompt: str, brief_body: str, n: int) -> list[str]:
        return _extract_jsonl(self._chat(base_prompt, brief_body, n))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DeepSeekClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["API_KEY_ENV", "DeepSeekClient", "DeepSeekError"]
