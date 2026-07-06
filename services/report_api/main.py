from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from secrets import compare_digest

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from services.report_api.admin import (
    AdminCatalog,
    PredictionOutcomeRequest,
    data_catalog,
)
from services.report_api.config import Settings
from services.report_api.connector_health import (
    ConnectorHealthResponse,
    collect_connector_health,
)
from services.report_api.domain import (
    ConsumerReportRequest,
    ReportRequest,
    ReportResponse,
    ReportType,
)
from services.report_api.evidence import EvidenceCollectionError
from services.report_api.harness.mcp import load_mcp_capabilities
from services.report_api.harness.memory import InMemoryRunMemory
from services.report_api.harness.models import (
    HarnessRunResponse,
    HarnessTrace,
    SkillCapability,
    SystemCapabilities,
)
from services.report_api.harness.orchestrator import ReportHarness
from services.report_api.harness.skills import default_skill_registry
from services.report_api.jobs import JobEvent, JobView, PersistentJobStore
from services.report_api.phase_registry import PhaseView, phase_progress, phase_views
from services.report_api.providers.base import LLMProvider, LLMProviderError
from services.report_api.providers.deepseek import DeepSeekProvider
from services.report_api.providers.mock import MockProvider
from services.report_api.research_harness import LayerLoopSummary, ResearchHarness
from services.report_api.service import ReportGenerationError, ReportService
from services.report_api.structured_match_data import (
    collect_structured_match_context,
)
from services.report_api.time_scope import scope_for_request

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "deepseek":
        assert settings.deepseek_api_key is not None
        return DeepSeekProvider(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
            max_attempts=3,
            max_concurrent_requests=settings.deepseek_max_concurrency,
        )
    return MockProvider()


def public_provider_error_message(exc: LLMProviderError) -> str:
    if exc.kind == "authentication":
        return "AI 模型密钥或权限配置异常，请检查 DeepSeek API Key"
    if exc.kind == "billing":
        return "AI 模型账户余额不足，请检查 DeepSeek 账户余额"
    if exc.kind == "rate_limit":
        return "AI 服务请求过于频繁，系统已自动限流；请稍后重试"
    if exc.kind == "context_overflow":
        return "AI 上下文过大，系统已安全停止；请改用标准版或稍后重试"
    if exc.kind == "bad_request":
        return "AI 模型请求参数不被服务接受，请检查模型配置"
    if exc.kind == "invalid_response":
        return "AI 返回格式异常，系统已安全停止；请稍后重试"
    if exc.kind == "timeout":
        return "AI 服务响应超时，系统已自动重试；请再次生成"
    return "AI 服务连接中断，系统已自动重试；请再次生成"


def research_item_budget(request: ConsumerReportRequest) -> int:
    if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
        return {"concise": 12, "standard": 24, "deep": 36}[request.length.value]
    return {"concise": 6, "standard": 10, "deep": 12}[request.length.value]


def create_app(
    settings: Settings | None = None, provider: LLMProvider | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    provider = provider or build_provider(settings)
    service = ReportService(
        provider=provider,
        model=settings.deepseek_pro_model,
        flash_model=settings.deepseek_flash_model,
        max_output_tokens=settings.llm_max_output_tokens,
        max_attempts=settings.report_max_attempts,
        youtube_api_key=settings.youtube_api_key,
        youtube_channel_ids=settings.youtube_official_channel_ids,
        media_enabled=settings.licensed_media_enabled,
    )
    skill_registry = default_skill_registry()
    run_memory = InMemoryRunMemory()
    harness = ReportHarness(service, skill_registry, run_memory)
    research_harness = ResearchHarness(
        provider,
        model=settings.deepseek_flash_model,
        max_output_tokens=settings.llm_max_output_tokens,
        youtube_api_key=settings.youtube_api_key,
        youtube_channel_ids=settings.youtube_official_channel_ids,
        media_enabled=settings.licensed_media_enabled,
        structured_match_enabled=settings.football_data_configured,
    )
    repository_root = Path(__file__).resolve().parents[2]
    web_root = repository_root / "apps" / "web"
    media_root = repository_root / "artifacts" / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    job_store = PersistentJobStore(repository_root / settings.database_path)
    provider_health: dict[str, str | int | None] = {
        "kind": None,
        "status_code": None,
        "message": None,
    }

    def record_provider_error(exc: LLMProviderError) -> None:
        provider_health["kind"] = exc.kind
        provider_health["status_code"] = exc.status_code
        provider_health["message"] = public_provider_error_message(exc)

    def current_model_status() -> str:
        kind = provider_health["kind"]
        if kind in {"authentication", "billing", "bad_request", "context_overflow"}:
            return "needs_attention"
        if kind in {"rate_limit", "invalid_response", "timeout", "transient"}:
            return "degraded"
        return "available" if settings.llm_provider == "deepseek" else "demo"

    def require_internal_api() -> None:
        if not settings.internal_api_enabled:
            raise HTTPException(status_code=404, detail="not found")

    async def parse_internal_body(
        raw_request: Request, model_type: type[ReportRequest | ConsumerReportRequest]
    ) -> ReportRequest | ConsumerReportRequest:
        try:
            payload = await raw_request.json()
            return model_type.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=exc.errors(include_context=False)
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid JSON body") from exc

    app = FastAPI(
        title="Football AI Report API",
        version="0.1.0",
        description="Evidence-backed football reports; no social publishing.",
    )
    app.mount("/assets", StaticFiles(directory=web_root / "assets"), name="assets")
    app.mount("/media", StaticFiles(directory=media_root), name="media")
    app.state.background_tasks = set()
    job_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)

    @app.middleware("http")
    async def privacy_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: https://upload.wikimedia.org "
            "https://i.ytimg.com; connect-src 'self'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    async def consumer_home() -> FileResponse:
        return FileResponse(
            web_root / "index.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/admin", include_in_schema=False)
    async def admin_home() -> FileResponse:
        return FileResponse(
            web_root / "admin.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "provider": settings.llm_provider}

    @app.get("/v1/product/status")
    async def product_status() -> dict[str, bool | str | dict[str, bool]]:
        model_status = current_model_status()
        return {
            "generation_ready": settings.llm_provider == "deepseek"
            and model_status != "needs_attention",
            "mode": "live" if settings.llm_provider == "deepseek" else "demo",
            "model_status": model_status,
            "model_issue": provider_health["message"] or "",
            "source": (
                "今日球脉阶段表（Seed 收集 + 精简 + Leader 分栏 + 小组循环 "
                "+ 覆盖合稿 + 声明校验）"
            ),
            "external_services": {
                "sportmonks": settings.sportmonks_configured,
                "football_data": settings.football_data_configured,
                "news_api": settings.news_api_configured,
                "youtube_key": bool(settings.youtube_api_key),
                "youtube_channel_allowlist": bool(
                    settings.youtube_official_channel_ids
                ),
                "licensed_media": settings.licensed_media_enabled,
                "google_vision": settings.google_vision_configured,
            },
        }

    @app.get("/v1/admin/catalog", response_model=AdminCatalog)
    async def admin_catalog(
        x_admin_token: str | None = Header(default=None),
    ) -> AdminCatalog:
        if not settings.admin_enabled:
            raise HTTPException(status_code=404, detail="not found")
        if not x_admin_token or not compare_digest(
            x_admin_token, settings.admin_token or ""
        ):
            raise HTTPException(status_code=403, detail="forbidden")
        return data_catalog(datetime.now(UTC))

    @app.get("/v1/admin/connector-health", response_model=ConnectorHealthResponse)
    async def admin_connector_health(
        x_admin_token: str | None = Header(default=None),
    ) -> ConnectorHealthResponse:
        if not settings.admin_enabled:
            raise HTTPException(status_code=404, detail="not found")
        if not x_admin_token or not compare_digest(
            x_admin_token, settings.admin_token or ""
        ):
            raise HTTPException(status_code=403, detail="forbidden")
        return await collect_connector_health()

    @app.post("/v1/reports/generate", response_model=ReportResponse)
    async def generate_report(raw_request: Request) -> ReportResponse:
        require_internal_api()
        request = await parse_internal_body(raw_request, ReportRequest)
        assert isinstance(request, ReportRequest)
        try:
            return await service.generate(request)
        except ReportGenerationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMProviderError as exc:
            record_provider_error(exc)
            raise HTTPException(
                status_code=502, detail=public_provider_error_message(exc)
            ) from exc

    @app.post("/v1/runs", response_model=HarnessRunResponse)
    async def run_report(raw_request: Request) -> HarnessRunResponse:
        require_internal_api()
        request = await parse_internal_body(raw_request, ReportRequest)
        assert isinstance(request, ReportRequest)
        try:
            return await harness.run(request)
        except ReportGenerationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMProviderError as exc:
            record_provider_error(exc)
            raise HTTPException(
                status_code=502, detail=public_provider_error_message(exc)
            ) from exc

    @app.post("/v1/research/reports", response_model=HarnessRunResponse)
    async def research_report(
        raw_request: Request,
    ) -> HarnessRunResponse:
        require_internal_api()
        request = await parse_internal_body(raw_request, ConsumerReportRequest)
        assert isinstance(request, ConsumerReportRequest)
        try:
            scoped_request = request.model_copy(
                update={"time_scope": scope_for_request(request)}
            )
            bundle = await research_harness.collect(
                scoped_request,
                max_items=research_item_budget(scoped_request),
            )
            evidence = bundle.evidence
            match_context = None
            if scoped_request.report_type == ReportType.MATCH_PREDICTION:
                try:
                    structured, match_context = await collect_structured_match_context(
                        scoped_request
                    )
                    evidence = [*structured, *evidence]
                except Exception:
                    match_context = None
            time_scope = scope_for_request(scoped_request)
            report_request = ReportRequest(
                **scoped_request.model_dump(exclude={"time_scope"}),
                data_cutoff=time_scope.data_cutoff_utc,
                time_scope=time_scope,
                evidence=evidence,
                match_context=match_context,
                collection_warnings=bundle.warnings,
                previous_story_memory=job_store.recent_story_memory(
                    scoped_request.report_type.value
                ),
                editorial_plan=bundle.editorial_plan,
                prefetched_media_assets=bundle.media_assets,
            )
            return await harness.run(
                report_request,
                tool_rounds_used=bundle.tool_rounds_used + (1 if match_context else 0),
                research_layer_runs=bundle.layer_runs,
            )
        except EvidenceCollectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ReportGenerationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMProviderError as exc:
            record_provider_error(exc)
            raise HTTPException(
                status_code=502,
                detail=public_provider_error_message(exc),
            ) from exc

    async def execute_research_job_inner(
        job_id: str, request: ConsumerReportRequest
    ) -> None:
        try:
            def record_progress(
                phase: str, progress: int, payload: dict | None = None
            ) -> None:
                event_payload = dict(payload) if isinstance(payload, dict) else None
                checkpoint = None
                if event_payload:
                    checkpoint = event_payload.pop("checkpoint", None)
                if isinstance(checkpoint, dict):
                    checkpoint_name = checkpoint.get("name")
                    checkpoint_payload = checkpoint.get("payload")
                    if isinstance(checkpoint_name, str) and isinstance(
                        checkpoint_payload, dict
                    ):
                        job_store.save_checkpoint(
                            job_id, checkpoint_name, checkpoint_payload
                        )
                job_store.update(
                    job_id,
                    status="running",
                    phase=phase,
                    progress=progress,
                    detail=f"{phase} 阶段已更新。",
                    payload=event_payload,
                )

            async def finish_report_request(
                report_request: ReportRequest,
                *,
                tool_rounds_used: int,
                layer_runs: list[LayerLoopSummary],
                resumed_from_checkpoint: bool = False,
            ) -> None:
                next_phase = (
                    "prediction_council"
                    if request.report_type.value == "match_prediction"
                    else "research_desks"
                )
                job_store.update(
                    job_id,
                    status="running",
                    phase=next_phase,
                    progress=phase_progress(next_phase, 55),
                    detail=(
                        "从检查点恢复，进入专栏研究或多席研判阶段。"
                        if resumed_from_checkpoint
                        else "进入专栏研究或多席研判阶段。"
                    ),
                    payload={"resumed_from_checkpoint": resumed_from_checkpoint},
                )
                result = await harness.run(
                    report_request,
                    tool_rounds_used=tool_rounds_used,
                    research_layer_runs=layer_runs,
                    progress_callback=record_progress,
                )
                result_payload = result.model_dump(mode="json")
                job_store.save_story_memory(request.report_type.value, result_payload)
                job_store.update(
                    job_id,
                    status="completed",
                    phase="completed",
                    progress=phase_progress("completed", 100),
                    result=result_payload,
                    detail="报告已通过质量门并保存。",
                    payload={
                        "report_id": result.report.id,
                        "resumed_from_checkpoint": resumed_from_checkpoint,
                        "trace_steps": [
                            {
                                "name": step.name,
                                "label": step.label,
                                "detail": step.detail,
                            }
                            for step in result.run.steps
                        ],
                    },
                )

            checkpoint = job_store.latest_checkpoint(
                job_id, {"report_request_ready"}
            )
            if checkpoint is not None:
                checkpoint_payload = checkpoint.payload
                report_request = ReportRequest.model_validate(
                    checkpoint_payload["report_request"]
                )
                layer_runs = [
                    LayerLoopSummary.model_validate(item)
                    for item in checkpoint_payload.get("layer_runs", [])
                ]
                await finish_report_request(
                    report_request,
                    tool_rounds_used=int(
                        checkpoint_payload.get("tool_rounds_used") or 0
                    ),
                    layer_runs=layer_runs,
                    resumed_from_checkpoint=True,
                )
                return

            job_store.update(
                job_id,
                status="running",
                phase="collecting_sources",
                progress=phase_progress("collecting_sources", 10),
                detail="开始运行资料流水线。",
            )
            scoped_request = request.model_copy(
                update={"time_scope": scope_for_request(request)}
            )
            bundle = await research_harness.collect(
                scoped_request,
                max_items=research_item_budget(scoped_request),
                progress_callback=lambda phase, progress: job_store.update(
                    job_id,
                    status="running",
                    phase=phase,
                    progress=progress,
                    detail=f"{phase} 阶段已更新。",
                ),
            )
            evidence = bundle.evidence
            match_context = None
            if scoped_request.report_type == ReportType.MATCH_PREDICTION:
                try:
                    structured, match_context = await collect_structured_match_context(
                        scoped_request
                    )
                    evidence = [*structured, *evidence]
                except Exception:
                    match_context = None
            job_store.update(
                job_id,
                status="running",
                phase="evidence_ready",
                progress=phase_progress("evidence_ready", 44),
                detail=(
                    f"证据包 {len(evidence)} 条；媒体候选 "
                    f"{len(bundle.media_assets)} 个；栏目 "
                    f"{len(bundle.editorial_plan)} 个。"
                ),
                payload={
                    "evidence_count": len(evidence),
                    "media_assets": len(bundle.media_assets),
                    "editorial_columns": [
                        {
                            "id": column.column_id,
                            "title": column.title,
                            "group": column.specialist_group,
                            "evidence_ids": column.evidence_ids,
                        }
                        for column in bundle.editorial_plan
                    ],
                    "warnings": bundle.warnings[:8],
                },
            )
            time_scope = scope_for_request(scoped_request)
            report_request = ReportRequest(
                **scoped_request.model_dump(exclude={"time_scope"}),
                data_cutoff=time_scope.data_cutoff_utc,
                time_scope=time_scope,
                evidence=evidence,
                match_context=match_context,
                collection_warnings=bundle.warnings,
                previous_story_memory=job_store.recent_story_memory(
                    scoped_request.report_type.value
                ),
                editorial_plan=bundle.editorial_plan,
                prefetched_media_assets=bundle.media_assets,
            )
            tool_rounds_used = bundle.tool_rounds_used + (1 if match_context else 0)
            job_store.save_checkpoint(
                job_id,
                "report_request_ready",
                {
                    "report_request": report_request.model_dump(mode="json"),
                    "tool_rounds_used": tool_rounds_used,
                    "layer_runs": [
                        layer.model_dump(mode="json") for layer in bundle.layer_runs
                    ],
                },
            )
            await finish_report_request(
                report_request,
                tool_rounds_used=tool_rounds_used,
                layer_runs=bundle.layer_runs,
            )
        except EvidenceCollectionError as exc:
            job_store.update(
                job_id,
                status="failed",
                phase="failed",
                progress=phase_progress("failed", 100),
                error=str(exc) or "资料收集失败，请稍后重试",
                detail=str(exc) or "资料收集失败，请稍后重试",
            )
        except ReportGenerationError as exc:
            logger.warning("research job %s stopped by quality gate: %s", job_id, exc)
            job_store.update(
                job_id,
                status="failed",
                phase="failed",
                progress=phase_progress("failed", 100),
                error="AI 研究未能通过质量校验，请稍后重试",
                detail=str(exc),
            )
        except LLMProviderError as exc:
            record_provider_error(exc)
            logger.warning(
                "research job %s stopped by provider error kind=%s status=%s: %s",
                job_id,
                exc.kind,
                exc.status_code,
                exc,
            )
            job_store.update(
                job_id,
                status="failed",
                phase="failed",
                progress=phase_progress("failed", 100),
                error=public_provider_error_message(exc),
                detail=public_provider_error_message(exc),
                payload={
                    "provider_error_kind": exc.kind,
                    "status_code": exc.status_code,
                },
            )
        except Exception as exc:
            logger.exception("research job %s stopped by an internal error", job_id)
            job_store.update(
                job_id,
                status="failed",
                phase="failed",
                progress=phase_progress("failed", 100),
                error="任务遇到内部错误并已安全停止，请稍后重试",
                detail="任务遇到内部错误并已安全停止，请稍后重试",
                payload={
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:500],
                },
            )

    async def execute_research_job(job_id: str, request: ConsumerReportRequest) -> None:
        job_store.update(
            job_id,
            status="queued",
            phase="waiting_for_capacity",
            progress=phase_progress("waiting_for_capacity", 2),
            detail="等待后台研究名额。",
        )
        async with job_semaphore:
            await execute_research_job_inner(job_id, request)

    def schedule_research_job(job_id: str, request: ConsumerReportRequest) -> None:
        task = asyncio.create_task(execute_research_job(job_id, request))
        app.state.background_tasks.add(task)
        task.add_done_callback(app.state.background_tasks.discard)

    @app.on_event("startup")
    async def resume_interrupted_research_jobs() -> None:
        for job in job_store.list_resumable():
            try:
                request = job_store.request_for_job(job.id)
            except (KeyError, ValueError):
                continue
            schedule_research_job(job.id, request)

    @app.post("/v1/research/jobs", response_model=JobView, status_code=202)
    async def create_research_job(request: ConsumerReportRequest) -> JobView:
        job = job_store.create(request)
        schedule_research_job(job.id, request)
        return job

    @app.get("/v1/research/jobs/{job_id}", response_model=JobView)
    async def get_research_job(job_id: str) -> JobView:
        try:
            return job_store.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/v1/research/jobs/{job_id}/events", response_model=list[JobEvent])
    async def get_research_job_events(job_id: str) -> list[JobEvent]:
        try:
            job_store.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        return job_store.list_events(job_id)

    @app.get("/v1/admin/jobs", response_model=list[JobView])
    async def admin_jobs(
        x_admin_token: str | None = Header(default=None),
    ) -> list[JobView]:
        if not settings.admin_enabled:
            raise HTTPException(status_code=404, detail="not found")
        if not x_admin_token or not compare_digest(
            x_admin_token, settings.admin_token or ""
        ):
            raise HTTPException(status_code=403, detail="forbidden")
        return job_store.list_recent()

    @app.get("/v1/admin/overview")
    async def admin_overview(
        x_admin_token: str | None = Header(default=None),
    ) -> dict:
        if not settings.admin_enabled:
            raise HTTPException(status_code=404, detail="not found")
        if not x_admin_token or not compare_digest(
            x_admin_token, settings.admin_token or ""
        ):
            raise HTTPException(status_code=403, detail="forbidden")
        return job_store.overview()

    @app.post("/v1/admin/prediction-outcomes")
    async def record_prediction_outcome(
        request: PredictionOutcomeRequest,
        x_admin_token: str | None = Header(default=None),
        x_admin_role: str | None = Header(default=None),
    ) -> dict:
        if not settings.admin_enabled:
            raise HTTPException(status_code=404, detail="not found")
        if not x_admin_token or not compare_digest(
            x_admin_token, settings.admin_token or ""
        ):
            raise HTTPException(status_code=403, detail="forbidden")
        if x_admin_role != "result_writer":
            raise HTTPException(status_code=403, detail="result_writer role required")
        try:
            return job_store.record_prediction_outcome(request.job_id, request.outcome)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/runs", response_model=list[HarnessTrace])
    async def list_runs() -> list[HarnessTrace]:
        require_internal_api()
        return run_memory.list()

    @app.get("/v1/runs/{run_id}", response_model=HarnessTrace)
    async def get_run(run_id: str) -> HarnessTrace:
        require_internal_api()
        trace = run_memory.get(run_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="run not found")
        return trace

    @app.get("/v1/system/capabilities", response_model=SystemCapabilities)
    async def system_capabilities() -> SystemCapabilities:
        return SystemCapabilities(
            provider=settings.llm_provider,
            model=settings.deepseek_pro_model,
            skills=[
                SkillCapability(
                    id=skill.id,
                    version=skill.version,
                    report_type=skill.report_type,
                    max_model_rounds=skill.max_model_rounds,
                    max_tool_rounds=skill.max_tool_rounds,
                    phases=skill.phases,
                )
                for skill in skill_registry.list()
            ],
            mcp_servers=load_mcp_capabilities(),
            privacy=[
                "API Key 仅由后端环境读取",
                "模型请求不写入浏览器存储",
                "产品不连接社媒发布",
                "运行 Trace 不保存隐藏思维链",
            ],
        )

    @app.get("/v1/system/phases/{report_type}", response_model=list[PhaseView])
    async def system_phases(report_type: ReportType) -> list[PhaseView]:
        return phase_views(report_type)

    return app


app = create_app()
