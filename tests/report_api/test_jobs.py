from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx

from services.report_api.config import Settings
from services.report_api.domain import ConsumerReportRequest, Evidence
from services.report_api.jobs import PersistentJobStore
from services.report_api.main import create_app
from services.report_api.providers.mock import MockProvider


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


def test_async_research_job_reports_real_completion(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_collect(_request, **_kwargs):
        now = datetime.now(UTC)
        return [
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

    monkeypatch.setattr(
        "services.report_api.main.collect_research_evidence", fake_collect
    )
    settings = Settings(database_path=tmp_path / "api-jobs.db")
    app = create_app(settings, MockProvider())

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
