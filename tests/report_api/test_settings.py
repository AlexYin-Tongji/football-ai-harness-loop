from __future__ import annotations

from services.report_api.config import Settings, load_local_dotenv


def test_dotenv_loads_without_overriding_process_env(tmp_path, monkeypatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "LLM_PROVIDER=deepseek",
                "DEEPSEEK_API_KEY=dotenv-key",
                "SPORTMONKS_API_TOKEN=sportmonks-token",
                "YOUTUBE_API_KEY=youtube-token",
                "YOUTUBE_OFFICIAL_CHANNEL_IDS=fifa-channel, club-channel",
                "GOOGLE_CLOUD_VISION_API_KEY=vision-token",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "process-key")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SPORTMONKS_API_TOKEN", raising=False)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("YOUTUBE_OFFICIAL_CHANNEL_IDS", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_VISION_API_KEY", raising=False)

    try:
        loaded = load_local_dotenv(dotenv)
        settings = Settings.from_env(dotenv)

        assert "SPORTMONKS_API_TOKEN" in loaded
        assert settings.llm_provider == "deepseek"
        assert settings.deepseek_api_key == "process-key"
        assert settings.sportmonks_configured is True
        assert settings.youtube_api_key == "youtube-token"
        assert settings.youtube_official_channel_ids == (
            "fifa-channel",
            "club-channel",
        )
        assert settings.google_vision_configured is True
    finally:
        for key in (
            "LLM_PROVIDER",
            "SPORTMONKS_API_TOKEN",
            "YOUTUBE_API_KEY",
            "YOUTUBE_OFFICIAL_CHANNEL_IDS",
            "GOOGLE_CLOUD_VISION_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)


def test_local_dotenv_can_be_disabled(tmp_path, monkeypatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("LLM_PROVIDER=deepseek\n", encoding="utf-8")
    monkeypatch.setenv("FOOTPULSE_LOAD_DOTENV", "false")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    loaded = load_local_dotenv(dotenv)

    assert loaded == ()
    assert Settings.from_env(dotenv).llm_provider == "mock"
