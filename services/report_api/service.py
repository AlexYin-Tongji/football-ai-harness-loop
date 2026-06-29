from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from services.report_api.domain import (
    PredictionOpinion,
    ReportRequest,
    ReportResponse,
    ReportType,
    TokenUsage,
)
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
        flash_model: str | None = None,
        max_attempts: int = 2,
    ) -> None:
        self._provider = provider
        self._model = model
        self._council_enabled = flash_model is not None
        self._flash_model = flash_model or model
        self._max_output_tokens = max_output_tokens
        self._max_attempts = max_attempts

    async def generate(
        self,
        request: ReportRequest,
        max_attempts: int | None = None,
        skill_instructions: str | None = None,
    ) -> ReportResponse:
        messages = build_messages(request, skill_instructions)
        council_results: list[LLMResult] = []
        if request.report_type == ReportType.MATCH_PREDICTION and self._council_enabled:
            opinions, council_results = await self._run_prediction_council(request)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "以下是两个独立分析席位的结构化意见。你是终审席：必须审阅分歧，"
                        "不得简单平均；仅能引用输入 evidence_id，并输出最终报告。\n"
                        + "\n".join(item.model_dump_json() for item in opinions)
                    ),
                }
            )
        last_result: LLMResult | None = None
        last_errors: list[str] = []
        attempt_limit = max_attempts or self._max_attempts
        attempt_limit = min(
            attempt_limit,
            self._max_attempts,
            max(1, 5 - len(council_results)),
        )

        for attempt in range(1, attempt_limit + 1):
            llm_request = LLMRequest(
                purpose=request.report_type.value,
                model=(
                    self._model
                    if request.report_type == ReportType.MATCH_PREDICTION
                    else self._flash_model
                ),
                messages=messages,
                thinking_enabled=request.report_type == ReportType.MATCH_PREDICTION,
                max_output_tokens=min(
                    self._max_output_tokens,
                    (
                        5000
                        if request.report_type == ReportType.MATCH_PREDICTION
                        else {"concise": 1800, "standard": 3500, "deep": 6000}[
                            request.length.value
                        ]
                    ),
                ),
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
                attempts=attempt + len(council_results),
                usage=TokenUsage(
                    input_tokens=last_result.input_tokens
                    + sum(item.input_tokens for item in council_results),
                    output_tokens=last_result.output_tokens
                    + sum(item.output_tokens for item in council_results),
                ),
                report=report,
            )

        raise ReportGenerationError(
            "report validation failed after bounded retries: " + "; ".join(last_errors)
        )

    async def _run_prediction_council(
        self, request: ReportRequest
    ) -> tuple[list[PredictionOpinion], list[LLMResult]]:
        evidence = "\n".join(
            f"[{item.id}] {item.title}: {item.summary}" for item in request.evidence
        )
        roles = {
            "form_analyst": "从实力、状态、阵容、休息和战术匹配分析支持性证据",
            "skeptic": "主动寻找样本偏差、伤停不确定性、对位风险和反方证据",
        }

        opinion_schema = json.dumps(
            PredictionOpinion.model_json_schema(), ensure_ascii=False
        )

        async def run_role(
            role: str, instruction: str
        ) -> tuple[PredictionOpinion, list[LLMResult]]:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are an independent pre-match football analyst. Output one "
                        "JSON object only. It must match this JSON Schema exactly: "
                        f"{opinion_schema}. key_claims must contain at least one "
                        "claim, and every evidence_ids value must come from the "
                        "supplied evidence. The three probabilities must sum to 1. "
                        "Do not add keys or prose."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Role: {role}. Task: {instruction}\n"
                        f"Match: {request.subject}\nEvidence:\n{evidence}"
                    ),
                },
            ]

            async def generate(current_messages: list[dict[str, str]]) -> LLMResult:
                return await self._provider.generate_json(
                    LLMRequest(
                        purpose=f"prediction_opinion:{role}",
                        model=self._flash_model,
                        messages=current_messages,
                        thinking_enabled=True,
                        max_output_tokens=min(self._max_output_tokens, 1800),
                        metadata={
                            "report_type": request.report_type.value,
                            "subject": request.subject,
                            "match_stage": request.match_stage.value
                            if request.match_stage
                            else None,
                            "evidence_ids": [item.id for item in request.evidence],
                            "opinion_role": role,
                        },
                    )
                )

            def validate(output: dict[str, object]) -> PredictionOpinion:
                opinion = PredictionOpinion.model_validate({**output, "role": role})
                total = opinion.home_win + opinion.draw + opinion.away_win
                if abs(total - 1.0) > 0.001:
                    raise ValueError("probabilities are not normalized")
                allowed = {item.id for item in request.evidence}
                referenced = {
                    evidence_id
                    for claim in opinion.key_claims
                    for evidence_id in claim.evidence_ids
                }
                if referenced - allowed:
                    raise ValueError("opinion cites unknown evidence")
                return opinion

            results = [await generate(messages)]
            try:
                opinion = validate(results[0].output)
            except (ValidationError, ValueError) as exc:
                repair_messages = [
                    *messages,
                    {
                        "role": "assistant",
                        "content": json.dumps(results[0].output, ensure_ascii=False),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Repair the JSON to match the schema exactly. "
                            "Do not add prose. "
                            f"Validation error: {exc}"
                        ),
                    },
                ]
                results.append(await generate(repair_messages))
                try:
                    opinion = validate(results[-1].output)
                except (ValidationError, ValueError) as retry_exc:
                    raise ReportGenerationError(
                        f"prediction opinion {role} failed its bounded repair"
                    ) from retry_exc
            return opinion, results

        completed = await asyncio.gather(
            *(run_role(role, instruction) for role, instruction in roles.items())
        )
        opinions = [item[0] for item in completed]
        results = [result for item in completed for result in item[1]]
        return opinions, results
