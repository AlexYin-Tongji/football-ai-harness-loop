import asyncio
from collections.abc import Iterable

from services.report_api.domain import ReportRequest
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
        "title": "A vs B prediction",
        "executive_summary": "Evidence-backed test prediction.",
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
