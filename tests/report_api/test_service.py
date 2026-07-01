import asyncio
from collections.abc import Iterable

from services.report_api.domain import (
    MatchModelContext,
    RecentMatchSample,
    ReportRequest,
)
from services.report_api.providers.base import LLMRequest, LLMResult
from services.report_api.service import ReportService


class SequenceProvider:
    def __init__(self, outputs: Iterable[dict[str, object]]) -> None:
        self.outputs = iter(outputs)
        self.calls = 0

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        return LLMResult(
            output=next(self.outputs),
            provider="stub",
            model=request.model,
        )


def request_payload() -> ReportRequest:
    return ReportRequest.model_validate(
        {
            "report_type": "match_prediction",
            "subject": "A vs B",
            "report_date": "2026-06-28",
            "data_cutoff": "2026-06-28T08:00:00Z",
            "match_stage": "knockout",
            "evidence": [
                {
                    "id": "ev-1",
                    "title": "Match context",
                    "url": "https://example.com/match",
                    "published_at": "2026-06-28T07:00:00Z",
                    "source_name": "Official source",
                    "summary": "Match context for testing.",
                }
            ],
        }
    )


def report_output(home_win: float = 0.4) -> dict[str, object]:
    return {
        "title": "A 对 B 比赛预测",
        "executive_summary": "这是一份基于输入证据生成的中文比赛预测测试摘要。",
        "sections": [
            {
                "heading": "Context",
                "body": "Test context.",
                "evidence_ids": ["ev-1"],
            }
        ],
        "warnings": [],
        "prediction": {
            "home_win": home_win,
            "draw": 0.3,
            "away_win": 0.3,
            "qualification": {"home": 0.55, "away": 0.45},
            "scorelines": ["1-0"],
            "supporting_factors": [{"claim": "Support", "evidence_ids": ["ev-1"]}],
            "counter_factors": [{"claim": "Counter", "evidence_ids": ["ev-1"]}],
            "unknowns": [],
            "confidence": "low",
        },
    }


def test_service_retries_invalid_probability_once() -> None:
    provider = SequenceProvider([report_output(0.5), report_output(0.4)])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request_payload()))

    assert provider.calls == 2
    assert result.attempts == 2
    assert result.report.prediction is not None
    assert result.report.prediction.home_win == 0.4


def test_service_retries_unknown_evidence_reference() -> None:
    invalid = report_output()
    invalid["sections"][0]["evidence_ids"] = ["invented-id"]
    provider = SequenceProvider([invalid, report_output()])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request_payload()))

    assert provider.calls == 2
    assert result.report.sections[0].evidence_ids == ["ev-1"]


def test_service_injects_deterministic_baseline_after_model_validation() -> None:
    sample = RecentMatchSample(goals_for=2, goals_against=1)
    context = MatchModelContext(
        home_team="A",
        away_team="B",
        home_recent=[sample, sample, sample],
        away_recent=[
            RecentMatchSample(goals_for=0, goals_against=1),
            RecentMatchSample(goals_for=1, goals_against=1),
            RecentMatchSample(goals_for=0, goals_against=2),
        ],
        evidence_ids=["ev-1"],
    )
    request = request_payload().model_copy(update={"match_context": context})
    provider = SequenceProvider([report_output()])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request))

    assert result.report.prediction is not None
    baseline = result.report.prediction.statistical_baseline
    assert baseline is not None
    assert baseline.method == "poisson"
    assert baseline.evidence_ids == ["ev-1"]


def test_service_injects_only_sourced_external_prediction() -> None:
    request = request_payload()
    request.evidence[0].summary = (
        "Opta rates the home side's chance of victory in normal time at 73.9%."
    )
    provider = SequenceProvider([report_output()])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request))

    assert result.report.prediction is not None
    external = result.report.prediction.external_predictions
    assert len(external) == 1
    assert external[0].source_name == "Opta"
    assert "73.9%" in external[0].summary
