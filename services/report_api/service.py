from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from services.report_api.domain import (
    DeskBrief,
    DeskDraft,
    GeneratedReport,
    PredictionOpinion,
    ReportRequest,
    ReportResponse,
    ReportType,
    TokenUsage,
)
from services.report_api.media import collect_report_media
from services.report_api.prediction import (
    build_statistical_baseline,
    extract_external_predictions,
)
from services.report_api.prompts import (
    PROMPT_VERSION,
    append_revision_request,
    build_messages,
)
from services.report_api.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMRequest,
    LLMResult,
)
from services.report_api.validation import (
    ReportValidationError,
    normalize_generated_output,
    validate_generated_report,
)

FINAL_EVIDENCE_SUMMARY_CHARS = 420


class ReportGenerationError(RuntimeError):
    """Raised after the bounded generation loop cannot produce a valid report."""


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _compact_request_for_final_editor(request: ReportRequest) -> ReportRequest:
    """Keep citation targets but avoid repeating full evidence in the final pass."""
    compact_evidence = [
        item.model_copy(
            update={
                "title": _shorten(item.title, 220),
                "summary": _shorten(item.summary, FINAL_EVIDENCE_SUMMARY_CHARS),
            }
        )
        for item in request.evidence
    ]
    return request.model_copy(update={"evidence": compact_evidence})


def _final_output_budget(request: ReportRequest, configured_limit: int) -> int:
    if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
        return min(
            configured_limit,
            {"concise": 2400, "standard": 3600, "deep": 4500}[
                request.length.value
            ],
        )
    if request.report_type == ReportType.MATCH_PREDICTION:
        return min(configured_limit, 4500)
    return min(
        configured_limit,
        {"concise": 1800, "standard": 3500, "deep": 6000}[request.length.value],
    )


def _stable_final_output_budget(request: ReportRequest, configured_limit: int) -> int:
    if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
        return min(
            configured_limit,
            {"concise": 2000, "standard": 2800, "deep": 3300}[
                request.length.value
            ],
        )
    if request.report_type == ReportType.MATCH_PREDICTION:
        return min(configured_limit, 3000)
    return min(
        configured_limit,
        {"concise": 1600, "standard": 2600, "deep": 3200}[request.length.value],
    )


def _is_recoverable_final_provider_error(exc: LLMProviderError) -> bool:
    return exc.kind in {"timeout", "transient", "rate_limit", "invalid_response"}


def _append_report_warning(report: GeneratedReport, warning: str) -> None:
    if warning in report.warnings:
        return
    if len(report.warnings) < 12:
        report.warnings.append(warning)
    elif report.warnings:
        report.warnings[-1] = warning


class ReportService:
    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        max_output_tokens: int,
        flash_model: str | None = None,
        max_attempts: int = 2,
        youtube_api_key: str | None = None,
        youtube_channel_ids: tuple[str, ...] = (),
        media_enabled: bool = False,
    ) -> None:
        self._provider = provider
        self._model = model
        self._council_enabled = flash_model is not None
        self._flash_model = flash_model or model
        self._max_output_tokens = max_output_tokens
        self._max_attempts = max_attempts
        self._youtube_api_key = youtube_api_key
        self._youtube_channel_ids = youtube_channel_ids
        self._media_enabled = media_enabled

    async def generate(
        self,
        request: ReportRequest,
        max_attempts: int | None = None,
        skill_instructions: str | None = None,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> ReportResponse:
        messages = build_messages(request, skill_instructions)
        council_results: list[LLMResult] = []
        desk_drafts: list[DeskDraft] = []
        statistical_baseline = build_statistical_baseline(request.match_context)
        sourced_external_predictions = extract_external_predictions(request.evidence)
        if request.report_type == ReportType.MATCH_PREDICTION:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "确定性统计基线如下。可以比较和解释，但不得修改其数值；"
                        "输出中的 statistical_baseline 请保持 null，Harness 会在"
                        "校验后注入原始计算结果。\n"
                        + (
                            statistical_baseline.model_dump_json()
                            if statistical_baseline
                            else "当前缺少每队至少三场结构化赛果，统计基线不可用。"
                        )
                    ),
                }
            )
        if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
            desk_drafts, council_results = await self._run_daily_desks(request)
            if progress_callback:
                progress_callback("desk_drafts_ready", 75)
            messages = build_messages(
                _compact_request_for_final_editor(request), skill_instructions
            )
            if desk_drafts:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "以下是赛事桌与转会桌分别完成的草稿。你是总编辑：去重、"
                            "保留两个栏目边界并整合为一份《今日球脉》。传闻必须保留"
                            "明确的未核实标签，不能因进入草稿而升级可信度。"
                            "你已经收到压缩证据索引；事实细节优先来自草稿，"
                            "引用只能使用索引中存在的 evidence_id。\n"
                            + "\n".join(
                                item.model_dump_json() for item in desk_drafts
                            )
                        ),
                    }
                )
        if request.report_type == ReportType.MATCH_PREDICTION and self._council_enabled:
            opinions, council_results = await self._run_prediction_council(request)
            if progress_callback:
                progress_callback("prediction_council_ready", 72)
            if opinions:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"以下是 {len(opinions)} 个成功返回的独立分析席位意见。"
                            "你是终审席：必须审阅分歧，不得简单平均；仅能引用输入 "
                            "evidence_id，并输出最终报告。如果少于两个席位，必须在 "
                            "warnings 中说明预测委员会已降级。\n"
                            + "\n".join(
                                item.model_dump_json() for item in opinions
                            )
                        ),
                    }
                )
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "两个独立 Flash 分析席位暂时不可用。请仅依据证据包完成"
                            "保守预测，在 warnings 明确说明预测委员会已降级，并降低"
                            " confidence；不得补造外部观点。"
                        ),
                    }
                )
        last_result: LLMResult | None = None
        last_errors: list[str] = []
        attempt_limit = max_attempts or self._max_attempts
        total_round_limit = (
            7
            if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST
            else 5
            if request.report_type == ReportType.MATCH_PREDICTION
            else 2
        )
        attempt_limit = min(
            attempt_limit,
            self._max_attempts,
            max(1, total_round_limit - len(council_results)),
        )

        recovery_rounds = 0
        used_stable_final = False
        for attempt in range(1, attempt_limit + 1):
            if progress_callback:
                progress_callback("editor_synthesis", 82)
            llm_request = LLMRequest(
                purpose=request.report_type.value,
                model=(
                    self._model
                    if request.report_type
                    in {
                        ReportType.MATCH_PREDICTION,
                        ReportType.DAILY_FOOTBALL_DIGEST,
                    }
                    else self._flash_model
                ),
                messages=messages,
                thinking_enabled=request.report_type
                in {
                    ReportType.MATCH_PREDICTION,
                    ReportType.DAILY_FOOTBALL_DIGEST,
                },
                max_output_tokens=_final_output_budget(
                    request, self._max_output_tokens
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
            try:
                last_result = await self._provider.generate_json(llm_request)
            except LLMProviderError as exc:
                if not _is_recoverable_final_provider_error(exc):
                    raise
                recovery_rounds += 1
                used_stable_final = True
                stable_request = self._stable_final_request(
                    llm_request, request, exc
                )
                try:
                    last_result = await self._provider.generate_json(stable_request)
                except LLMProviderError as stable_exc:
                    if (
                        request.report_type == ReportType.DAILY_FOOTBALL_DIGEST
                        and desk_drafts
                        and _is_recoverable_final_provider_error(stable_exc)
                    ):
                        recovery_rounds += 1
                        return self._deterministic_daily_response(
                            request,
                            desk_drafts,
                            council_results,
                            attempts=attempt + len(council_results) + recovery_rounds,
                            progress_callback=progress_callback,
                        )
                    raise
            normalized_output = normalize_generated_output(last_result.output, request)
            try:
                report = validate_generated_report(normalized_output, request)
            except ReportValidationError as exc:
                last_errors = exc.errors
                if attempt < attempt_limit:
                    messages = append_revision_request(
                        messages, normalized_output, exc.errors
                    )
                    continue
                break

            if used_stable_final:
                _append_report_warning(
                    report,
                    "高思考总编辑请求曾遇到模型连接异常；系统已使用稳定合稿模式恢复，"
                    "请发布前重点复核取舍和措辞。",
                )

            if report.prediction is not None:
                report.prediction.statistical_baseline = statistical_baseline
                known_external = {
                    (item.source_name.casefold(), tuple(item.evidence_ids))
                    for item in report.prediction.external_predictions
                }
                additions = [
                    item
                    for item in sourced_external_predictions
                    if (item.source_name.casefold(), tuple(item.evidence_ids))
                    not in known_external
                ]
                report.prediction.external_predictions = [
                    *report.prediction.external_predictions,
                    *additions,
                ][:6]

            if self._media_enabled:
                if progress_callback:
                    progress_callback("licensed_media", 90)
                report.enrichment.media_assets = await collect_report_media(
                    report,
                    youtube_api_key=self._youtube_api_key,
                    youtube_channel_ids=list(self._youtube_channel_ids),
                )

            return ReportResponse(
                id=str(uuid4()),
                provider=last_result.provider,
                model=last_result.model,
                prompt_version=PROMPT_VERSION,
                data_cutoff=request.data_cutoff,
                generated_at=datetime.now(UTC),
                attempts=attempt + len(council_results) + recovery_rounds,
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

    def _stable_final_request(
        self,
        llm_request: LLMRequest,
        request: ReportRequest,
        exc: LLMProviderError,
    ) -> LLMRequest:
        return LLMRequest(
            purpose=f"{request.report_type.value}:stable_final",
            model=llm_request.model,
            messages=[
                *llm_request.messages,
                {
                    "role": "user",
                    "content": (
                        "上一轮高思考总编辑请求未能稳定返回。现在进入稳定合稿模式："
                        "只输出一份较短但完整的 JSON 报告；优先保留已证实事实、"
                        "传闻标签、来源引用和主要结论；可减少人物卡、时间线和修辞，"
                        "但不得新增证据外事实。warnings 中说明已启用稳定合稿。"
                        f"上游错误类型：{exc.kind}。"
                    ),
                },
            ],
            thinking_enabled=False,
            max_output_tokens=_stable_final_output_budget(
                request, self._max_output_tokens
            ),
            metadata={
                **llm_request.metadata,
                "recovery": "stable_final",
                "provider_error_kind": exc.kind,
            },
        )

    def _deterministic_daily_response(
        self,
        request: ReportRequest,
        desk_drafts: list[DeskDraft],
        council_results: list[LLMResult],
        *,
        attempts: int,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> ReportResponse:
        if progress_callback:
            progress_callback("deterministic_finalizer", 88)
        sections = [
            section
            for draft in desk_drafts
            for section in draft.sections
        ][:10]
        warnings = [
            "高思考总编辑与稳定合稿均遇到模型连接异常；系统已根据完成的分桌草稿"
            "生成保守版本，请发布前重点复核结构和措辞。"
        ]
        for draft in desk_drafts:
            warnings.extend(draft.warnings)
        report = validate_generated_report(
            GeneratedReport(
                title=f"{request.subject}｜保守合稿版",
                executive_summary="；".join(
                    _shorten(draft.summary, 450) for draft in desk_drafts
                )
                or "系统已根据完成的分桌草稿生成保守版今日足球消息汇总。",
                sections=sections,
                warnings=list(dict.fromkeys(warnings))[:12],
                prediction=None,
            ).model_dump(mode="json"),
            request,
        )
        return ReportResponse(
            id=str(uuid4()),
            provider="harness",
            model="deterministic-daily-finalizer",
            prompt_version=PROMPT_VERSION,
            data_cutoff=request.data_cutoff,
            generated_at=datetime.now(UTC),
            attempts=attempts,
            usage=TokenUsage(
                input_tokens=sum(item.input_tokens for item in council_results),
                output_tokens=sum(item.output_tokens for item in council_results),
            ),
            report=report,
        )

    async def _run_daily_desks(
        self, request: ReportRequest
    ) -> tuple[list[DeskDraft], list[LLMResult]]:
        evidence = "\n".join(
            (
                f"[{item.id}] [{item.verification_status}/{item.trust_tier}] "
                f"{item.title}: {item.summary}"
            )
            for item in request.evidence
        )
        desks = {
            "match_news": (
                "提取赛果、赛程、晋级影响、球队动态和今日观赛重点；若证据明确"
                "写出进球者、分钟和比分变化，必须保留为时间线候选。不得把新闻"
                "标题当作结构化赛果。"
            ),
            "transfer_market": (
                "提取官宣、报价、谈判、接触、否认及绯闻。低可信线索可以保留，"
                "但必须放入 rumor_items 并说明未核实。对重要球员同时保留位置、"
                "当前球队、关联球队和证据明确提供的出场/进球/助攻等数据。"
            ),
        }
        brief_schema = json.dumps(DeskBrief.model_json_schema(), ensure_ascii=False)
        draft_schema = json.dumps(DeskDraft.model_json_schema(), ensure_ascii=False)

        async def research(
            desk: str, instruction: str
        ) -> tuple[DeskBrief, LLMResult]:
            result = await self._provider.generate_json(
                LLMRequest(
                    purpose=f"daily_research:{desk}",
                    model=self._flash_model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是今日足球情报研究员。只输出符合 JSON Schema 的"
                                f"对象：{brief_schema}。只能引用输入 evidence_id。"
                                "unverified_lead 只能作为 rumor_items。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Desk: {desk}\n{instruction}\n证据：\n{evidence}"
                            ),
                        },
                    ],
                    thinking_enabled=True,
                    max_output_tokens=min(self._max_output_tokens, 2600),
                    metadata={
                        "report_type": request.report_type.value,
                        "subject": request.subject,
                        "evidence_ids": [item.id for item in request.evidence],
                        "desk": desk,
                    },
                )
            )
            brief = DeskBrief.model_validate({**result.output, "desk": desk})
            return brief, result

        researched = await asyncio.gather(
            *(research(desk, instruction) for desk, instruction in desks.items()),
            return_exceptions=True,
        )
        briefs = [item for item in researched if not isinstance(item, BaseException)]
        results = [item[1] for item in briefs]
        if not briefs:
            return [], results

        async def write_desk(brief: DeskBrief) -> tuple[DeskDraft, LLMResult]:
            result = await self._provider.generate_json(
                LLMRequest(
                    purpose=f"daily_desk_write:{brief.desk}",
                    model=self._model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是资深中文足球编辑。根据研究简报写一个栏目草稿，"
                                f"只输出符合 JSON Schema 的对象：{draft_schema}。"
                                "保留逐节 evidence_ids；传闻必须用‘传闻/未核实’措辞。"
                                "不要只复述结果：用一两句解释人物背景、球队关联或比赛"
                                "转折；事实与编辑判断必须分开。"
                            ),
                        },
                        {"role": "user", "content": brief.model_dump_json()},
                    ],
                    thinking_enabled=True,
                    max_output_tokens=min(self._max_output_tokens, 3600),
                    metadata={
                        "report_type": request.report_type.value,
                        "subject": request.subject,
                        "evidence_ids": [item.id for item in request.evidence],
                        "desk": brief.desk,
                    },
                )
            )
            draft = DeskDraft.model_validate({**result.output, "desk": brief.desk})
            return draft, result

        written = await asyncio.gather(
            *(write_desk(item[0]) for item in briefs), return_exceptions=True
        )
        successful = [item for item in written if not isinstance(item, BaseException)]
        results.extend(item[1] for item in successful)
        return [item[0] for item in successful], results

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
            *(run_role(role, instruction) for role, instruction in roles.items()),
            return_exceptions=True,
        )
        successful = [
            item for item in completed if not isinstance(item, BaseException)
        ]
        if not successful:
            # The Pro judge can still produce a bounded report from the evidence
            # packet. This avoids one transient Flash seat taking down the user
            # request while keeping the missing review visible in the prompt.
            return [], []
        opinions = [item[0] for item in successful]
        results = [result for item in successful for result in item[1]]
        return opinions, results
