from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded without importing framework-specific config."""

    llm_provider: str = "mock"
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_flash_model: str = "deepseek-v4-flash"
    deepseek_pro_model: str = "deepseek-v4-pro"
    llm_timeout_seconds: float = 90.0
    llm_max_output_tokens: int = 6000
    report_max_attempts: int = 2
    admin_enabled: bool = False
    admin_token: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        settings = cls(
            llm_provider=os.getenv("LLM_PROVIDER", "mock").strip().lower(),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            deepseek_base_url=os.getenv(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/"),
            deepseek_flash_model=os.getenv("DEEPSEEK_FLASH_MODEL", "deepseek-v4-flash"),
            deepseek_pro_model=os.getenv("DEEPSEEK_PRO_MODEL", "deepseek-v4-pro"),
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "90")),
            llm_max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "6000")),
            report_max_attempts=int(os.getenv("REPORT_MAX_ATTEMPTS", "2")),
            admin_enabled=os.getenv("ADMIN_ENABLED", "false").lower() == "true",
            admin_token=os.getenv("ADMIN_TOKEN") or None,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.llm_provider not in {"mock", "deepseek"}:
            raise ValueError("LLM_PROVIDER must be 'mock' or 'deepseek'")
        if self.llm_provider == "deepseek" and not self.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required when LLM_PROVIDER=deepseek")
        if self.llm_timeout_seconds <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS must be positive")
        if self.llm_max_output_tokens <= 0:
            raise ValueError("LLM_MAX_OUTPUT_TOKENS must be positive")
        if not 1 <= self.report_max_attempts <= 3:
            raise ValueError("REPORT_MAX_ATTEMPTS must be between 1 and 3")
        if self.admin_enabled and not self.admin_token:
            raise ValueError("ADMIN_TOKEN is required when ADMIN_ENABLED=true")
