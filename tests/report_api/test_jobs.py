from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx

from services.report_api.config import Settings
from services.report_api.domain import ConsumerReportRequest, Evidence
from services.report_api.jobs import PersistentJobStore
from services.report_api.main import create_app
from services.report_api.providers.base import LLMProviderError
from services.report_api.providers.mock import MockProvider
from services.report_api.research_harness import (
    ResearchBundle,
    fallback_research_plan,
)


def bundle_for(request, evidence: list[Evidence]) -> ResearchBundle:
    return ResearchBundle(
        evidence=evidence,
        warnings=["测试资料包由 ResearchHarness 返回。"],
        plan=fallback_research_plan(request),
        source_attempts={"test": "ok"},
    )


def test_job_store_survives_process_reconstruction(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉",
        report_date="2026-07-01",
    )
    first = PersistentJobStore(path)
    job = first.create(request)
    first.update(
        job.id,
        status="completed",
        phase="completed",
        progress=100,
        result={"ok": True},
    )

    reconstructed = PersistentJobStore(path)
    restored = reconstructed.get(job.id)

    assert restored.status == "completed"
    assert restored.result == {"ok": True}


def test_job_store_marks_interrupted_work_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "interrupted.db"
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉",
        report_date="2026-07-01",
    )
    first = PersistentJobStore(path)
    job = first.create(request)

    reconstructed = PersistentJobStore(path)
    restored = reconstructed.get(job.id)

    assert restored.status == "failed"
    assert restored.phase == "interrupted"


def test_prediction_outcome_rejects_duplicate_write(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.db"
    request = ConsumerReportRequest(
        report_type="match_prediction",
        subject="A vs B",
        report_date="2026-07-01",
        match_stage="knockout",
    )
    store = PersistentJobStore(path)
    job = store.create(request)
    store.update(
        job.id,
        status="completed",
        phase="completed",
        progress=100,
        result={
            "report": {
                "report": {
                    "prediction": {
                        "home_win": 0.4,
                        "draw": 0.3,
                        "away_win": 0.3,
                    }
                }
            }
        },
    )

    first = store.record_prediction_outcome(job.id, "home")

    assert first["brier_score"] > 0
    try:
        store.record_prediction_outcome(job.id, "away")
    except ValueError as exc:
        assert "already recorded" in str(exc)
    else:
        raise AssertionError("duplicate outcome write should fail")


def test_async_research_job_reports_real_completion(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_collect(_self, request, **_kwargs):
        now = datetime.now(UTC)
        evidence = [
            Evidence(
                id="source-1",
                title="Football update",
                url="https://example.com/update",
                published_at=now,
                source_name="Approved publisher",
                summary="A current football update.",
            ),
            Evidence(
                id="source-2",
                title="Transfer update",
                url="https://example.com/transfer",
                published_at=now,
                source_name="Approved publisher",
                summary="A current transfer update.",
            ),
        ]
        return bundle_for(request, evidence)

    monkeypatch.setattr(
        "services.report_api.research_harness.ResearchHarness.collect", fake_collect
    )
    settings = Settings(database_path=tmp_path / "api-jobs.db")
    app = create_app(settings, MockProvider())

    async def scenario() -> tuple[dict, dict]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/v1/research/jobs",
                json={
                    "report_type": "daily_football_digest",
                    "subject": "今日球脉",
                    "report_date": "2026-07-01",
                },
            )
            assert created.status_code == 202
            job_id = created.json()["id"]
            for _ in range(20):
                current = await client.get(f"/v1/research/jobs/{job_id}")
                payload = current.json()
                if payload["status"] in {"completed", "failed"}:
                    return payload
                await asyncio.sleep(0.01)
        raise AssertionError("job did not finish")

    payload = asyncio.run(scenario())

    assert payload["status"] == "completed"
    assert payload["progress"] == 100
    assert payload["result"]["run"]["skill_id"] == "daily-football-digest"


def test_async_job_reports_provider_disconnect_separately(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_collect(_self, request, **_kwargs):
        now = datetime.now(UTC)
        evidence = [
            Evidence(
                id=f"source-{index}",
                title=f"Football update {index}",
                url=f"https://example.com/update-{index}",
                published_at=now,
                source_name="Approved publisher",
                summary="A current football update.",
            )
            for index in range(2)
        ]
        return bundle_for(request, evidence)

    class DisconnectedProvider:
        async def generate_json(self, _request):
            raise LLMProviderError("DeepSeek request failed: RemoteProtocolError")

    monkeypatch.setattr(
        "services.report_api.research_harness.ResearchHarness.collect", fake_collect
    )
    app = create_app(
        Settings(database_path=tmp_path / "failed-jobs.db"),
        DisconnectedProvider(),
    )

    async def scenario() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/v1/research/jobs",
                json={
                    "report_type": "daily_football_digest",
                    "subject": "今日球脉",
                    "report_date": "2026-07-01",
                },
            )
            job_id = created.json()["id"]
            for _ in range(20):
                payload = (await client.get(f"/v1/research/jobs/{job_id}")).json()
                if payload["status"] in {"completed", "failed"}:
                    return payload
                await asyncio.sleep(0.01)
        raise AssertionError("job did not finish")

    payload = asyncio.run(scenario())

    assert payload["status"] == "failed"
    assert payload["error"] == "AI 服务连接中断，系统已自动重试；请再次生成"


def test_async_job_reports_deepseek_authentication_error(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_collect(_self, request, **_kwargs):
        now = datetime.now(UTC)
        evidence = [
            Evidence(
                id=f"source-{index}",
                title=f"Football update {index}",
                url=f"https://example.com/update-{index}",
                published_at=now,
                source_name="Approved publisher",
                summary="A current football update.",
            )
            for index in range(2)
        ]
        return bundle_for(request, evidence)

    class UnauthorizedProvider:
        async def generate_json(self, _request):
            raise LLMProviderError(
                "DeepSeek request was rejected (HTTP 401)",
                kind="authentication",
                status_code=401,
            )

    monkeypatch.setattr(
        "services.report_api.research_harness.ResearchHarness.collect", fake_collect
    )
    app = create_app(
        Settings(database_path=tmp_path / "auth-failed-jobs.db"),
        UnauthorizedProvider(),
    )

    async def scenario() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/v1/research/jobs",
                json={
                    "report_type": "daily_football_digest",
                    "subject": "今日球脉",
                    "report_date": "2026-07-02",
                },
            )
            job_id = created.json()["id"]
            for _ in range(20):
                payload = (await client.get(f"/v1/research/jobs/{job_id}")).json()
                if payload["status"] in {"completed", "failed"}:
                    status = (await client.get("/v1/product/status")).json()
                    return payload, status
                await asyncio.sleep(0.01)
        raise AssertionError("job did not finish")

    payload, status = asyncio.run(scenario())

    assert payload["status"] == "failed"
    assert payload["error"] == "AI 模型密钥或权限配置异常，请检查 DeepSeek API Key"
    assert status["generation_ready"] is False
    assert status["model_status"] == "needs_attention"
    assert status["model_issue"] == "AI 模型密钥或权限配置异常，请检查 DeepSeek API Key"
