from __future__ import annotations

import asyncio

import httpx

from services.report_api.connector_health import (
    local_connector_health_shell,
    probe_news_api,
    probe_sportmonks_big_five,
)


def test_sportmonks_big_five_probe_reports_partial_coverage(monkeypatch) -> None:
    monkeypatch.setenv("SPORTMONKS_API_TOKEN", "sportmonks-test-token")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "sportmonks-test-token"
        if "Premier%20League" in str(request.url):
            return httpx.Response(
                200, json={"data": [{"id": 8, "name": "Premier League"}]}
            )
        return httpx.Response(200, json={"data": []})

    result = asyncio.run(probe_sportmonks_big_five(httpx.MockTransport(handler)))

    assert result.status == "degraded"
    assert result.big_five_leagues[0].status == "covered"
    assert result.big_five_leagues[0].matched_league_ids == [8]
    assert any(item.status == "not_covered" for item in result.big_five_leagues)


def test_newsapi_probe_uses_header_not_query(monkeypatch) -> None:
    monkeypatch.setenv("NEWS_API_KEY", "newsapi-test-token")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] == "newsapi-test-token"
        assert "apiKey" not in request.url.params
        return httpx.Response(200, json={"status": "ok", "articles": []})

    result = asyncio.run(probe_news_api(httpx.MockTransport(handler)))

    assert result.status == "healthy"
    assert result.configured is True


def test_local_connector_health_shell_marks_missing_optional_services(
    monkeypatch,
) -> None:
    for key in (
        "FOOTBALL_DATA_API_KEY",
        "YOUTUBE_API_KEY",
        "YOUTUBE_OFFICIAL_CHANNEL_IDS",
        "GOOGLE_CLOUD_VISION_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        monkeypatch.delenv(key, raising=False)

    football_data, _commons, youtube, visual = local_connector_health_shell()

    assert football_data.status == "not_configured"
    assert youtube.status == "not_configured"
    assert visual.status == "not_configured"
