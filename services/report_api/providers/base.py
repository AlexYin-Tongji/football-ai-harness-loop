from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ProviderErrorKind = Literal[
    "authentication",
    "billing",
    "rate_limit",
    "bad_request",
    "context_overflow",
    "invalid_response",
    "timeout",
    "transient",
]


@dataclass(frozen=True)
class LLMRequest:
    purpose: str
    model: str
    messages: list[dict[str, str]]
    thinking_enabled: bool
    max_output_tokens: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResult:
    output: dict[str, Any]
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: str | None = None


class LLMProviderError(RuntimeError):
    """Raised when a provider cannot return a usable structured response."""

    def __init__(
        self,
        message: str,
        *,
        kind: ProviderErrorKind = "transient",
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.request_id = request_id


class LLMProvider(Protocol):
    async def generate_json(self, request: LLMRequest) -> LLMResult: ...
