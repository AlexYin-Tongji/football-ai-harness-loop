from __future__ import annotations

import asyncio

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
