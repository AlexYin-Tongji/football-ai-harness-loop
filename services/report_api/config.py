from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DOTENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
DEFAULT_OFFICIAL_VIDEO_CHANNEL_IDS = (
    "UCpcTrCXblq78GZrTUTLWeBw",  # FIFA
    "UCCc3h5l7RvGzCAbZ1ApxOYw",  # Mediacorp Sports, official highlights partner
)


def _default_dotenv_path() -> Path:
    return Path(__file__).resolve().parents[2] / ".env"


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.removeprefix("export ").strip()
    if not DOTENV_KEY_RE.fullmatch(key):
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def load_local_dotenv(path: Path | None = None) -> tuple[str, ...]:
    """Load a local gitignored .env file.

    Process environment variables win by default. Local desktop runs can opt in
    to .env precedence with FOOTPULSE_DOTENV_OVERRIDE=true when a stale parent
    process secret would otherwise keep being inherited.
    """
    dotenv_path = path or _default_dotenv_path()
    if os.getenv("FOOTPULSE_LOAD_DOTENV", "true").lower() == "false":
        return ()
    if not dotenv_path.exists():
        return ()
    loaded: list[str] = []
    parsed_lines = [
        parsed
        for line in dotenv_path.read_text(encoding="utf-8-sig").splitlines()
        if (parsed := _parse_dotenv_line(line)) is not None
    ]
    override = os.getenv("FOOTPULSE_DOTENV_OVERRIDE", "").lower() == "true" or any(
        key == "FOOTPULSE_DOTENV_OVERRIDE" and value.lower() == "true"
        for key, value in parsed_lines
    )
    for key, value in parsed_lines:
        if override or key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return tuple(loaded)


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded without importing framework-specific config."""

    llm_provider: str = "mock"
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_flash_model: str = "deepseek-v4-flash"
    deepseek_pro_model: str = "deepseek-v4-pro"
    deepseek_max_concurrency: int = 2
    llm_timeout_seconds: float = 120.0
    llm_max_output_tokens: int = 6000
    report_max_attempts: int = 3
    admin_enabled: bool = False
    admin_token: str | None = None
    internal_api_enabled: bool = False
    database_path: Path = Path("data/footpulse.db")
    max_concurrent_jobs: int = 2
    sportmonks_configured: bool = False
    football_data_configured: bool = False
    news_api_configured: bool = False
    youtube_api_key: str | None = None
    youtube_official_channel_ids: tuple[str, ...] = ()
    licensed_media_enabled: bool = False
    google_vision_configured: bool = False

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> Settings:
        load_local_dotenv(dotenv_path)
        settings = cls(
            llm_provider=os.getenv("LLM_PROVIDER", "mock").strip().lower(),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            deepseek_base_url=os.getenv(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/"),
            deepseek_flash_model=os.getenv("DEEPSEEK_FLASH_MODEL", "deepseek-v4-flash"),
            deepseek_pro_model=os.getenv("DEEPSEEK_PRO_MODEL", "deepseek-v4-pro"),
            deepseek_max_concurrency=int(os.getenv("DEEPSEEK_MAX_CONCURRENCY", "2")),
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "120")),
            llm_max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "6000")),
            report_max_attempts=int(os.getenv("REPORT_MAX_ATTEMPTS", "3")),
            admin_enabled=os.getenv("ADMIN_ENABLED", "false").lower() == "true",
            admin_token=os.getenv("ADMIN_TOKEN") or None,
            internal_api_enabled=os.getenv("FOOTPULSE_INTERNAL_API_ENABLED", "false")
            .lower()
            == "true",
            database_path=Path(
                os.getenv("FOOTPULSE_DATABASE_PATH", "data/footpulse.db")
            ),
            max_concurrent_jobs=int(os.getenv("MAX_CONCURRENT_JOBS", "2")),
            sportmonks_configured=bool(os.getenv("SPORTMONKS_API_TOKEN")),
            football_data_configured=bool(os.getenv("FOOTBALL_DATA_API_KEY")),
            news_api_configured=bool(os.getenv("NEWS_API_KEY")),
            youtube_api_key=os.getenv("YOUTUBE_API_KEY") or None,
            youtube_official_channel_ids=tuple(
                dict.fromkeys(
                    [
                        *(
                            item.strip()
                            for item in os.getenv(
                                "YOUTUBE_OFFICIAL_CHANNEL_IDS", ""
                            ).split(",")
                            if item.strip()
                        ),
                        *DEFAULT_OFFICIAL_VIDEO_CHANNEL_IDS,
                    ]
                )
            ),
            licensed_media_enabled=os.getenv(
                "FOOTPULSE_MEDIA_PIPELINE_ENABLED", "false"
            ).lower()
            == "true",
            google_vision_configured=bool(
                os.getenv("GOOGLE_CLOUD_VISION_API_KEY")
                or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            ),
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
        if not 1 <= self.deepseek_max_concurrency <= 4:
            raise ValueError("DEEPSEEK_MAX_CONCURRENCY must be between 1 and 4")
        if not 1 <= self.report_max_attempts <= 3:
            raise ValueError("REPORT_MAX_ATTEMPTS must be between 1 and 3")
        if self.admin_enabled and not self.admin_token:
            raise ValueError("ADMIN_TOKEN is required when ADMIN_ENABLED=true")
        if not 1 <= self.max_concurrent_jobs <= 4:
            raise ValueError("MAX_CONCURRENT_JOBS must be between 1 and 4")
