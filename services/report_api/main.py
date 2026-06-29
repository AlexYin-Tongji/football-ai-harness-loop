from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from secrets import compare_digest

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from services.report_api.admin import AdminCatalog, data_catalog
from services.report_api.config import Settings
from services.report_api.domain import (
    ConsumerReportRequest,
    ReportRequest,
    ReportResponse,
)
from services.report_api.evidence import (
    EvidenceCollectionError,
    collect_guardian_evidence,
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
from services.report_api.providers.base import LLMProvider, LLMProviderError
from services.report_api.providers.deepseek import DeepSeekProvider
from services.report_api.providers.mock import MockProvider
from services.report_api.service import ReportGenerationError, ReportService


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "deepseek":
        assert settings.deepseek_api_key is not None
        return DeepSeekProvider(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
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
    )
    skill_registry = default_skill_registry()
    run_memory = InMemoryRunMemory()
    harness = ReportHarness(service, skill_registry, run_memory)
    repository_root = Path(__file__).resolve().parents[2]
    web_root = repository_root / "apps" / "web"

    app = FastAPI(
        title="Football AI Report API",
        version="0.1.0",
        description="Evidence-backed football reports; no social publishing.",
    )
    app.mount("/assets", StaticFiles(directory=web_root / "assets"), name="assets")

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
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    async def consumer_home() -> FileResponse:
        return FileResponse(web_root / "index.html")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "provider": settings.llm_provider}

    @app.get("/v1/product/status")
    async def product_status() -> dict[str, bool | str]:
        return {
            "generation_ready": settings.llm_provider == "deepseek",
            "mode": "live" if settings.llm_provider == "deepseek" else "demo",
            "source": "Guardian Football RSS",
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
            evidence = await collect_guardian_evidence(
                request,
                max_items={"concise": 6, "standard": 10, "deep": 12}[
                    request.length.value
                ],
            )
            report_request = ReportRequest(
                **request.model_dump(),
                data_cutoff=datetime.now(UTC),
                evidence=evidence,
            )
            return await harness.run(report_request, tool_rounds_used=1)
        except EvidenceCollectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ReportGenerationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMProviderError as exc:
            raise HTTPException(
                status_code=502,
                detail="AI 服务暂时无法完成报告，请稍后重试",
            ) from exc

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
