from __future__ import annotations

from fastapi import FastAPI, HTTPException

from services.report_api.config import Settings
from services.report_api.domain import ReportRequest, ReportResponse
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
        max_output_tokens=settings.llm_max_output_tokens,
        max_attempts=settings.report_max_attempts,
    )

    app = FastAPI(
        title="Football AI Report API",
        version="0.1.0",
        description="Evidence-backed football reports; no social publishing.",
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "provider": settings.llm_provider}

    @app.post("/v1/reports/generate", response_model=ReportResponse)
    async def generate_report(request: ReportRequest) -> ReportResponse:
        try:
            return await service.generate(request)
        except ReportGenerationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app


app = create_app()
