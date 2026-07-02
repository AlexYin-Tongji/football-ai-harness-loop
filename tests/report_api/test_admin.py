from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from services.report_api.config import Settings
from services.report_api.main import create_app
from services.report_api.providers.mock import MockProvider


async def get_catalog(settings: Settings, token: str | None = None) -> httpx.Response:
    headers = {"X-Admin-Token": token} if token else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, MockProvider())),
        base_url="http://test",
    ) as client:
        return await client.get("/v1/admin/catalog", headers=headers)


def test_admin_catalog_is_hidden_by_default() -> None:
    response = asyncio.run(get_catalog(Settings()))
    assert response.status_code == 404


def test_admin_catalog_requires_token_and_groups_data() -> None:
    settings = Settings(admin_enabled=True, admin_token="admin-test-token")
    denied = asyncio.run(get_catalog(settings, "wrong"))
    allowed = asyncio.run(get_catalog(settings, "admin-test-token"))

    assert denied.status_code == 403
    assert allowed.status_code == 200
    payload = allowed.json()
    assert set(payload["layers"]) == {
        "source",
        "football",
        "editorial",
        "agent",
        "governance",
    }
    assert all(
        not entity["contains_full_article"]
        for layer in payload["layers"].values()
        for entity in layer
    )


def test_admin_connector_health_requires_token_and_returns_safe_status(
    monkeypatch,
) -> None:
    for key in (
        "SPORTMONKS_API_TOKEN",
        "NEWS_API_KEY",
        "FOOTBALL_DATA_API_KEY",
        "YOUTUBE_API_KEY",
        "YOUTUBE_OFFICIAL_CHANNEL_IDS",
        "GOOGLE_CLOUD_VISION_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(admin_enabled=True, admin_token="admin-test-token")
    app = create_app(settings, MockProvider())

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.get(
                "/v1/admin/connector-health",
                headers={"X-Admin-Token": "wrong"},
            )
            allowed = await client.get(
                "/v1/admin/connector-health",
                headers={"X-Admin-Token": "admin-test-token"},
            )
            return denied, allowed

    denied, allowed = asyncio.run(scenario())

    assert denied.status_code == 403
    assert allowed.status_code == 200
    payload = allowed.json()
    assert payload["sportmonks"]["configured"] is False
    assert payload["sportmonks"]["big_five_leagues"][0]["status"] == "not_configured"
    assert payload["media"]["visual_relevance"]["configured"] is False


def test_prediction_result_write_requires_explicit_role(tmp_path: Path) -> None:
    settings = Settings(
        admin_enabled=True,
        admin_token="admin-test-token",
        database_path=tmp_path / "admin.db",
    )
    app = create_app(settings, MockProvider())

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.post(
                "/v1/admin/prediction-outcomes",
                headers={"X-Admin-Token": "admin-test-token"},
                json={"job_id": "missing", "outcome": "home"},
            )
            allowed_role = await client.post(
                "/v1/admin/prediction-outcomes",
                headers={
                    "X-Admin-Token": "admin-test-token",
                    "X-Admin-Role": "result_writer",
                },
                json={"job_id": "missing", "outcome": "home"},
            )
            return denied, allowed_role

    denied, allowed_role = asyncio.run(scenario())

    assert denied.status_code == 403
    assert allowed_role.status_code == 422
