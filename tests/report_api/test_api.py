import asyncio

import httpx

from services.report_api.config import Settings
from services.report_api.main import create_app


async def post_json(path: str, payload: dict[str, object]) -> httpx.Response:
    app = create_app(Settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.post(path, json=payload)


async def get(path: str) -> httpx.Response:
    app = create_app(Settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.get(path)


def test_generate_world_cup_report_with_mock_provider() -> None:
    response = asyncio.run(
        post_json(
            "/v1/reports/generate",
            {
                "report_type": "world_cup_daily",
                "subject": "世界杯今日综述",
                "report_date": "2026-06-28",
                "data_cutoff": "2026-06-28T08:00:00Z",
                "length": "standard",
                "focus": ["比赛结果", "今日看点"],
                "evidence": [
                    {
                        "id": "ev-1",
                        "title": "Official match update",
                        "url": "https://example.com/match",
                        "published_at": "2026-06-28T07:00:00Z",
                        "source_name": "Official source",
                        "summary": "A structured source summary for the test.",
                    }
                ],
            },
        )
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "mock"
    assert payload["report"]["prediction"] is None
    assert payload["report"]["sections"][0]["evidence_ids"] == ["ev-1"]


def test_match_prediction_requires_stage() -> None:
    response = asyncio.run(
        post_json(
            "/v1/reports/generate",
            {
                "report_type": "match_prediction",
                "subject": "A vs B",
                "report_date": "2026-06-28",
                "data_cutoff": "2026-06-28T08:00:00Z",
                "evidence": [
                    {
                        "id": "ev-1",
                        "title": "Match data",
                        "url": "https://example.com/match",
                        "published_at": "2026-06-28T07:00:00Z",
                        "source_name": "Official source",
                        "summary": "Match context.",
                    }
                ],
            },
        )
    )

    assert response.status_code == 422


def test_report_rejects_naive_cutoff_timestamp() -> None:
    response = asyncio.run(
        post_json(
            "/v1/reports/generate",
            {
                "report_type": "world_cup_daily",
                "subject": "Daily report",
                "report_date": "2026-06-28",
                "data_cutoff": "2026-06-28T08:00:00",
                "evidence": [
                    {
                        "id": "ev-1",
                        "title": "Match data",
                        "url": "https://example.com/match",
                        "published_at": "2026-06-28T07:00:00Z",
                        "source_name": "Official source",
                        "summary": "Match context.",
                    }
                ],
            },
        )
    )

    assert response.status_code == 422


def test_workbench_and_capabilities_are_available() -> None:
    page = asyncio.run(get("/"))
    capabilities = asyncio.run(get("/v1/system/capabilities"))

    assert page.status_code == 200
    assert "球脉 FootPulse AI" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert capabilities.status_code == 200
    payload = capabilities.json()
    assert len(payload["skills"]) == 3
    assert payload["provider"] == "mock"
    assert capabilities.headers["cache-control"] == "no-store"


def test_harness_run_endpoint_completes_and_is_queryable() -> None:
    app = create_app(Settings())

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            run = await client.post(
                "/v1/runs",
                json={
                    "report_type": "world_cup_daily",
                    "subject": "World Cup daily",
                    "report_date": "2026-06-29",
                    "data_cutoff": "2026-06-29T08:00:00Z",
                    "evidence": [
                        {
                            "id": "ev-1",
                            "title": "Match data",
                            "url": "https://example.com/match",
                            "published_at": "2026-06-29T07:00:00Z",
                            "source_name": "Official source",
                            "summary": "Match context.",
                        }
                    ],
                },
            )
            history = await client.get("/v1/runs")
            return run, history

    run, history = asyncio.run(scenario())

    assert run.status_code == 200
    assert run.json()["run"]["skill_id"] == "world-cup-daily"
    assert len(run.json()["run"]["steps"]) == 5
    assert history.status_code == 200
    assert len(history.json()) == 1
