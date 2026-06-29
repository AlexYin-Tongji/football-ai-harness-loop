from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

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
        opinions: list[PredictionOpinion] = []
        results: list[LLMResult] = []
        for role, instruction in roles.items():
            result = await self._provider.generate_json(
                LLMRequest(
                    purpose=f"prediction_opinion:{role}",
                    model=self._flash_model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是足球赛前分析委员。只输出 JSON：role, home_win, "
                                "draw, away_win, key_claims[{claim,evidence_ids}], "
                                "unknowns, "
                                "confidence。三项概率必须归一化为1，不得补造事实。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"角色：{role}。任务：{instruction}\n"
                                f"比赛：{request.subject}\n{evidence}"
                            ),
                        },
                    ],
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
            opinion = PredictionOpinion.model_validate({**result.output, "role": role})
            total = opinion.home_win + opinion.draw + opinion.away_win
            if abs(total - 1.0) > 0.001:
                raise ReportGenerationError(
                    f"prediction opinion {role} is not normalized"
                )
            allowed = {item.id for item in request.evidence}
            referenced = {
                evidence_id
                for claim in opinion.key_claims
                for evidence_id in claim.evidence_ids
            }
            if referenced - allowed:
                raise ReportGenerationError(
                    f"prediction opinion {role} cites unknown evidence"
                )
            opinions.append(opinion)
            results.append(result)
        return opinions, results
