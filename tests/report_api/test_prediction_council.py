from __future__ import annotations

import asyncio

from services.report_api.providers.base import LLMProviderError
from services.report_api.providers.mock import MockProvider
from services.report_api.service import ReportService
from tests.report_api.test_service import request_payload


def test_prediction_council_uses_three_analysts_and_a_judge() -> None:
    service = ReportService(
        provider=MockProvider(),
        model="deepseek-v4-pro",
        flash_model="deepseek-v4-flash",
        max_output_tokens=2000,
    )

    result = asyncio.run(service.generate(request_payload()))

    assert result.attempts == 4
    assert result.report.prediction is not None
    assert result.report.prediction.confidence == "low"


class OneSeatFailsProvider(MockProvider):
    async def generate_json(self, request):
        if request.purpose == "prediction_opinion:skeptic":
            raise LLMProviderError("transient seat failure")
        return await super().generate_json(request)


def test_prediction_council_degrades_when_one_seat_fails() -> None:
    service = ReportService(
        provider=OneSeatFailsProvider(),
        model="deepseek-v4-pro",
        flash_model="deepseek-v4-flash",
        max_output_tokens=2000,
    )

    result = asyncio.run(service.generate(request_payload()))

    assert result.attempts == 3
    assert result.report.prediction is not None
