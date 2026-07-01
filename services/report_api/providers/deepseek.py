from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from services.report_api.providers.base import (
    LLMProviderError,
    LLMRequest,
    LLMResult,
)


class DeepSeekProvider:
    """Minimal OpenAI-compatible DeepSeek V4 JSON client."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        max_attempts: int = 3,
        max_concurrent_requests: int = 2,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport
        self._max_attempts = max(1, min(max_attempts, 3))
        self._request_slots = asyncio.Semaphore(
            max(1, min(max_concurrent_requests, 4))
        )

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        async with self._request_slots:
            return await self._generate_json(request)

    async def _generate_json(self, request: LLMRequest) -> LLMResult:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "response_format": {"type": "json_object"},
            "max_tokens": request.max_output_tokens,
            "thinking": {"type": "enabled" if request.thinking_enabled else "disabled"},
        }
        if request.thinking_enabled:
            payload["reasoning_effort"] = "high"

        response: httpx.Response | None = None
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            # A fresh connection per attempt avoids reusing a socket that the
            # upstream closed while returning a long reasoning response.
            limits = httpx.Limits(max_connections=1, max_keepalive_connections=0)
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                limits=limits,
            ) as client:
                try:
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                            "Connection": "close",
                        },
                        json=payload,
                    )
                    response.raise_for_status()
                    break
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    retryable = exc.response.status_code in {408, 409, 429} or (
                        exc.response.status_code >= 500
                    )
                    if not retryable or attempt == self._max_attempts:
                        status = exc.response.status_code
                        request_id = exc.response.headers.get("x-request-id")
                        raise LLMProviderError(
                            "DeepSeek request was rejected "
                            f"(HTTP {status}, request_id={request_id or 'unknown'})"
                        ) from exc
                except httpx.RequestError as exc:
                    last_error = exc
                    if attempt == self._max_attempts:
                        raise LLMProviderError(
                            f"DeepSeek request failed: {exc.__class__.__name__}"
                        ) from exc
            await asyncio.sleep(0.4 * attempt)

        if response is None:
            raise LLMProviderError("DeepSeek request failed") from last_error

        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            if not content:
                raise ValueError("empty model content")
            output = json.loads(content)
            usage = data.get("usage") or {}
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise LLMProviderError(
                "DeepSeek returned an invalid JSON response"
            ) from exc

        if not isinstance(output, dict):
            raise LLMProviderError("DeepSeek JSON root must be an object")

        return LLMResult(
            output=output,
            provider="deepseek",
            model=data.get("model") or request.model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            request_id=response.headers.get("x-request-id") or data.get("id"),
        )
