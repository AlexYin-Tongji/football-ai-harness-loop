from __future__ import annotations

import asyncio
from datetime import date

from services.report_api.domain import ConsumerReportRequest
from services.report_api.structured_match_data import (
    collect_structured_match_context,
)


def test_structured_context_requires_configured_approved_provider(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
    request = ConsumerReportRequest(
        report_type="match_prediction",
        subject="Alpha vs Beta",
        report_date=date(2026, 7, 1),
        match_stage="group",
    )

    evidence, context = asyncio.run(collect_structured_match_context(request))

    assert evidence == []
    assert context is None


def test_structured_context_normalizes_recent_results(monkeypatch) -> None:
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-token")

    async def fake_matches(*_args, **_kwargs):
        return {
            "matches": [
                {
                    "home": "Alpha",
                    "away": "Gamma",
                    "status": "FINISHED",
                    "score": {"fullTime": {"home": 2, "away": 0}},
                },
                {
                    "home": "Delta",
                    "away": "Alpha",
                    "status": "FINISHED",
                    "score": {"fullTime": {"home": 1, "away": 1}},
                },
                {
                    "home": "Alpha",
                    "away": "Epsilon",
                    "status": "FINISHED",
                    "score": {"fullTime": {"home": 3, "away": 1}},
                },
                {
                    "home": "Beta",
                    "away": "Gamma",
                    "status": "FINISHED",
                    "score": {"fullTime": {"home": 1, "away": 0}},
                },
                {
                    "home": "Delta",
                    "away": "Beta",
                    "status": "FINISHED",
                    "score": {"fullTime": {"home": 2, "away": 0}},
                },
                {
                    "home": "Beta",
                    "away": "Epsilon",
                    "status": "FINISHED",
                    "score": {"fullTime": {"home": 2, "away": 2}},
                },
            ]
        }

    monkeypatch.setattr(
        "services.report_api.structured_match_data.list_competition_matches",
        fake_matches,
    )
    request = ConsumerReportRequest(
        report_type="match_prediction",
        subject="Alpha vs Beta｜World Cup",
        report_date=date(2026, 7, 1),
        match_stage="knockout",
    )

    evidence, context = asyncio.run(collect_structured_match_context(request))

    assert len(evidence) == 1
    assert evidence[0].evidence_kind == "structured"
    assert context is not None
    assert len(context.home_recent) == 3
    assert len(context.away_recent) == 3
    assert context.home_recent[0].goals_for == 2
