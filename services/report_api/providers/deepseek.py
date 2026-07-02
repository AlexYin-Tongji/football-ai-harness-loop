from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from services.report_api.providers.base import (
    LLMProviderError,
    LLMRequest,
    LLMResult,
    ProviderErrorKind,
)

logger = logging.getLogger(__name__)


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

    def _classify_status(
        self, status_code: int, response_text: str = ""
    ) -> ProviderErrorKind:
        if status_code in {401, 403}:
            return "authentication"
        if status_code == 402:
            return "billing"
        if status_code == 429:
            return "rate_limit"
        normalized = response_text.casefold()
        if status_code in {400, 422} and any(
            marker in normalized
            for marker in ("context", "token", "maximum length", "max length")
        ):
            return "context_overflow"
        if status_code in {400, 422}:
            return "bad_request"
        return "transient"

    def _timeout_for(self, request: LLMRequest) -> float:
        if request.thinking_enabled and request.purpose in {
            "daily_football_digest",
            "match_prediction",
        }:
            return max(self._timeout, 240.0)
        return self._timeout

    @staticmethod
    def _message_chars(request: LLMRequest) -> int:
        return sum(len(message.get("content", "")) for message in request.messages)

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
        request_timeout = self._timeout_for(request)
        input_chars = self._message_chars(request)
        for attempt in range(1, self._max_attempts + 1):
            # A fresh connection per attempt avoids reusing a socket that the
            # upstream closed while returning a long reasoning response.
            limits = httpx.Limits(max_connections=1, max_keepalive_connections=0)
            async with httpx.AsyncClient(
                timeout=request_timeout,
                transport=self._transport,
                limits=limits,
            ) as client:
                try:
                    started = time.perf_counter()
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                            "Connection": "close",
                        },
                        json=payload,
                    )
                    duration_ms = int((time.perf_counter() - started) * 1000)
                    logger.info(
                        "deepseek call completed purpose=%s model=%s attempt=%s "
                        "status=%s input_chars=%s max_tokens=%s timeout_s=%s "
                        "duration_ms=%s",
                        request.purpose,
                        request.model,
                        attempt,
                        response.status_code,
                        input_chars,
                        request.max_output_tokens,
                        request_timeout,
                        duration_ms,
                    )
                    response.raise_for_status()
                    break
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    status = exc.response.status_code
                    request_id = exc.response.headers.get("x-request-id")
                    kind = self._classify_status(status, exc.response.text[:1000])
                    retryable = kind in {"rate_limit", "transient"} and (
                        status in {408, 409, 429} or status >= 500
                    )
                    if not retryable or attempt == self._max_attempts:
                        raise LLMProviderError(
                            "DeepSeek request was rejected "
                            f"(HTTP {status}, request_id={request_id or 'unknown'})",
                            kind=kind,
                            status_code=status,
                            request_id=request_id,
                        ) from exc
                except httpx.TimeoutException as exc:
                    last_error = exc
                    if attempt == self._max_attempts:
                        raise LLMProviderError(
                            f"DeepSeek request timed out: {exc.__class__.__name__}",
                            kind="timeout",
                        ) from exc
                except httpx.RequestError as exc:
                    last_error = exc
                    if attempt == self._max_attempts:
                        raise LLMProviderError(
                            f"DeepSeek request failed: {exc.__class__.__name__}",
                            kind="transient",
                        ) from exc
            await asyncio.sleep(0.4 * attempt)

        if response is None:
            raise LLMProviderError(
                "DeepSeek request failed", kind="transient"
            ) from last_error

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
                "DeepSeek returned an invalid JSON response",
                kind="invalid_response",
            ) from exc

        if not isinstance(output, dict):
            raise LLMProviderError(
                "DeepSeek JSON root must be an object", kind="invalid_response"
            )

        return LLMResult(
            output=output,
            provider="deepseek",
            model=data.get("model") or request.model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            request_id=response.headers.get("x-request-id") or data.get("id"),
        )
