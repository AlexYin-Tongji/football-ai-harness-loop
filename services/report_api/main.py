from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from secrets import compare_digest

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from services.report_api.admin import (
    AdminCatalog,
    PredictionOutcomeRequest,
    data_catalog,
)
from services.report_api.config import Settings
from services.report_api.domain import (
    ConsumerReportRequest,
    ReportRequest,
    ReportResponse,
    ReportType,
)
from services.report_api.evidence import (
    EvidenceCollectionError,
    collect_research_evidence,
)
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
from services.report_api.jobs import JobView, PersistentJobStore
from services.report_api.providers.base import LLMProvider, LLMProviderError
from services.report_api.providers.deepseek import DeepSeekProvider
from services.report_api.providers.mock import MockProvider
from services.report_api.service import ReportGenerationError, ReportService
from services.report_api.structured_match_data import (
    collect_structured_match_context,
)

logger = logging.getLogger(__name__)


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
    repository_root = Path(__file__).resolve().parents[2]
    web_root = repository_root / "apps" / "web"
    job_store = PersistentJobStore(repository_root / settings.database_path)

    app = FastAPI(
        title="Football AI Report API",
        version="0.1.0",
        description="Evidence-backed football reports; no social publishing.",
    )
    app.mount("/assets", StaticFiles(directory=web_root / "assets"), name="assets")
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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "provider": settings.llm_provider}

    @app.get("/v1/product/status")
    async def product_status() -> dict[str, bool | str]:
        return {
            "generation_ready": settings.llm_provider == "deepseek",
            "mode": "live" if settings.llm_provider == "deepseek" else "demo",
            "source": "批准来源池（Guardian/BBC RSS + GDELT）",
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

    @app.post("/v1/reports/generate", response_model=ReportResponse)
    async def generate_report(request: ReportRequest) -> ReportResponse:
        try:
            return await service.generate(request)
        except ReportGenerationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/runs", response_model=HarnessRunResponse)
    async def run_report(request: ReportRequest) -> HarnessRunResponse:
        try:
            return await harness.run(request)
        except ReportGenerationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/research/reports", response_model=HarnessRunResponse)
    async def research_report(
        request: ConsumerReportRequest,
    ) -> HarnessRunResponse:
        try:
            evidence = await collect_research_evidence(
                request,
                max_items={"concise": 6, "standard": 10, "deep": 12}[
                    request.length.value
                ],
            )
            match_context = None
            if request.report_type == ReportType.MATCH_PREDICTION:
                try:
                    structured, match_context = (
                        await collect_structured_match_context(request)
                    )
                    evidence = [*structured, *evidence]
                except Exception:
                    match_context = None
            report_request = ReportRequest(
                **request.model_dump(),
                data_cutoff=datetime.now(UTC),
                evidence=evidence,
                match_context=match_context,
            )
            return await harness.run(
                report_request, tool_rounds_used=3 if match_context else 2
            )
        except EvidenceCollectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ReportGenerationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMProviderError as exc:
            raise HTTPException(
                status_code=502,
                detail="AI 服务暂时无法完成报告，请稍后重试",
            ) from exc

    async def execute_research_job_inner(
        job_id: str, request: ConsumerReportRequest
    ) -> None:
        try:
            job_store.update(
                job_id,
                status="running",
                phase="collecting_sources",
                progress=10,
            )
            evidence = await collect_research_evidence(
                request,
                max_items={"concise": 8, "standard": 16, "deep": 24}[
                    request.length.value
                ],
            )
            match_context = None
            if request.report_type == ReportType.MATCH_PREDICTION:
                try:
                    structured, match_context = (
                        await collect_structured_match_context(request)
                    )
                    evidence = [*structured, *evidence]
                except Exception:
                    match_context = None
            job_store.update(
                job_id,
                status="running",
                phase="evidence_ready",
                progress=30,
            )
            report_request = ReportRequest(
                **request.model_dump(),
                data_cutoff=datetime.now(UTC),
                evidence=evidence,
                match_context=match_context,
            )
            job_store.update(
                job_id,
                status="running",
                phase=(
                    "prediction_council"
                    if request.report_type.value == "match_prediction"
                    else "research_desks"
                ),
                progress=45,
            )
            def record_progress(phase: str, progress: int) -> None:
                job_store.update(
                    job_id,
                    status="running",
                    phase=phase,
                    progress=progress,
                )

            result = await harness.run(
                report_request,
                tool_rounds_used=3 if match_context else 2,
                progress_callback=record_progress,
            )
            job_store.update(
                job_id,
                status="completed",
                phase="completed",
                progress=100,
                result=result.model_dump(mode="json"),
            )
        except EvidenceCollectionError:
            job_store.update(
                job_id,
                status="failed",
                phase="failed",
                progress=100,
                error="近期资料不足或来源暂时不可用，请调整主题后重试",
            )
        except ReportGenerationError as exc:
            logger.warning("research job %s stopped by quality gate: %s", job_id, exc)
            job_store.update(
                job_id,
                status="failed",
                phase="failed",
                progress=100,
                error="AI 研究未能通过质量校验，请稍后重试",
            )
        except LLMProviderError as exc:
            logger.warning("research job %s lost its model connection: %s", job_id, exc)
            job_store.update(
                job_id,
                status="failed",
                phase="failed",
                progress=100,
                error="AI 服务连接中断，系统已自动重试；请再次生成",
            )
        except Exception:
            logger.exception("research job %s stopped by an internal error", job_id)
            job_store.update(
                job_id,
                status="failed",
                phase="failed",
                progress=100,
                error="任务遇到内部错误并已安全停止，请稍后重试",
            )

    async def execute_research_job(
        job_id: str, request: ConsumerReportRequest
    ) -> None:
        job_store.update(
            job_id,
            status="queued",
            phase="waiting_for_capacity",
            progress=2,
        )
        async with job_semaphore:
            await execute_research_job_inner(job_id, request)

    @app.post("/v1/research/jobs", response_model=JobView, status_code=202)
    async def create_research_job(request: ConsumerReportRequest) -> JobView:
        job = job_store.create(request)
        task = asyncio.create_task(execute_research_job(job.id, request))
        app.state.background_tasks.add(task)
        task.add_done_callback(app.state.background_tasks.discard)
        return job

    @app.get("/v1/research/jobs/{job_id}", response_model=JobView)
    async def get_research_job(job_id: str) -> JobView:
        try:
            return job_store.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

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
            return job_store.record_prediction_outcome(
                request.job_id, request.outcome
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/runs", response_model=list[HarnessTrace])
    async def list_runs() -> list[HarnessTrace]:
        return run_memory.list()

    @app.get("/v1/runs/{run_id}", response_model=HarnessTrace)
    async def get_run(run_id: str) -> HarnessTrace:
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

    return app


app = create_app()
