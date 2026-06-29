from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from services.report_api.domain import ReportRequest, ReportResponse, TokenUsage
from services.report_api.prompts import (
    PROMPT_VERSION,
    append_revision_request,
    build_messages,
)
from services.report_api.providers.base import LLMProvider, LLMRequest, LLMResult
from services.report_api.validation import (
    ReportValidationError,
    validate_generated_report,
)


class ReportGenerationError(RuntimeError):
    """Raised after the bounded generation loop cannot produce a valid report."""


class ReportService:
    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        max_output_tokens: int,
        max_attempts: int = 2,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._max_attempts = max_attempts

    async def generate(
        self,
        request: ReportRequest,
        max_attempts: int | None = None,
        skill_instructions: str | None = None,
    ) -> ReportResponse:
        messages = build_messages(request, skill_instructions)
        last_result: LLMResult | None = None
        last_errors: list[str] = []
        attempt_limit = max_attempts or self._max_attempts
        attempt_limit = min(attempt_limit, self._max_attempts)

        for attempt in range(1, attempt_limit + 1):
            llm_request = LLMRequest(
                purpose=request.report_type.value,
                model=self._model,
                messages=messages,
                thinking_enabled=True,
                max_output_tokens=self._max_output_tokens,
                metadata={
                    "report_type": request.report_type.value,
                    "subject": request.subject,
                    "match_stage": (
                        request.match_stage.value if request.match_stage else None
                    ),
                    "evidence_ids": [item.id for item in request.evidence],
                },
            )
            last_result = await self._provider.generate_json(llm_request)
            try:
                report = validate_generated_report(last_result.output, request)
            except ReportValidationError as exc:
                last_errors = exc.errors
                if attempt < attempt_limit:
                    messages = append_revision_request(
                        messages, last_result.output, exc.errors
                    )
                    continue
                break

            return ReportResponse(
                id=str(uuid4()),
                provider=last_result.provider,
                model=last_result.model,
                prompt_version=PROMPT_VERSION,
                data_cutoff=request.data_cutoff,
                generated_at=datetime.now(UTC),
                attempts=attempt,
                usage=TokenUsage(
                    input_tokens=last_result.input_tokens,
                    output_tokens=last_result.output_tokens,
                ),
                report=report,
            )

        raise ReportGenerationError(
            "report validation failed after bounded retries: " + "; ".join(last_errors)
        )
