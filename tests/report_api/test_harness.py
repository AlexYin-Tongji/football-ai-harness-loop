import asyncio

from services.report_api.config import Settings
from services.report_api.domain import ReportRequest
from services.report_api.harness.memory import InMemoryRunMemory
from services.report_api.harness.orchestrator import ReportHarness
from services.report_api.harness.skills import default_skill_registry
from services.report_api.providers.mock import MockProvider
from services.report_api.research_harness import LayerLoopSummary
from services.report_api.service import ReportService


def sample_request(report_type: str = "world_cup_daily") -> ReportRequest:
    payload = {
        "report_type": report_type,
        "subject": "Harness test",
        "report_date": "2026-06-29",
        "data_cutoff": "2026-06-29T08:00:00Z",
        "evidence": [
            {
                "id": "ev-1",
                "title": "Test evidence",
                "url": "https://example.com/evidence",
                "published_at": "2026-06-29T07:00:00Z",
                "source_name": "Test source",
                "summary": "Bounded test evidence.",
            }
        ],
    }
    if report_type == "match_prediction":
        payload["match_stage"] = "knockout"
    return ReportRequest.model_validate(payload)


def test_skill_registry_has_a_bounded_skill_for_each_report() -> None:
    registry = default_skill_registry()

    skills = registry.list()

    assert len(skills) == 4
    assert all(1 <= skill.max_model_rounds <= 20 for skill in skills)
    assert all(skill.quality_gates for skill in skills)


def test_harness_records_sanitized_checkpoints() -> None:
    settings = Settings()
    service = ReportService(
        provider=MockProvider(),
        model=settings.deepseek_pro_model,
        max_output_tokens=settings.llm_max_output_tokens,
        max_attempts=settings.report_max_attempts,
    )
    memory = InMemoryRunMemory()
    harness = ReportHarness(service, default_skill_registry(), memory)

    result = asyncio.run(harness.run(sample_request()))

    assert result.run.status == "completed"
    assert result.run.model_rounds_used == 1
    assert [step.name for step in result.run.steps] == [
        "route",
        "context",
        "generate",
        "quality_gate",
        "checkpoint",
    ]
    stored = memory.get(result.run.run_id)
    assert stored is not None
    assert stored.status == "completed"
    assert "Authorization" not in stored.model_dump_json()


def test_harness_records_research_layer_checkpoints() -> None:
    settings = Settings()
    service = ReportService(
        provider=MockProvider(),
        model=settings.deepseek_pro_model,
        max_output_tokens=settings.llm_max_output_tokens,
        max_attempts=settings.report_max_attempts,
    )
    memory = InMemoryRunMemory()
    harness = ReportHarness(service, default_skill_registry(), memory)
    layer = LayerLoopSummary(
        name="url_collection",
        label="第一层：URL 资料收集",
        status="completed",
        input_count=2,
        output_count=4,
        checkpoints=["candidate_urls=4"],
    )

    result = asyncio.run(
        harness.run(sample_request(), research_layer_runs=[layer])
    )

    step_names = [step.name for step in result.run.steps]
    assert "research_url_collection" in step_names
    assert any("candidate_urls=4" in step.detail for step in result.run.steps)
