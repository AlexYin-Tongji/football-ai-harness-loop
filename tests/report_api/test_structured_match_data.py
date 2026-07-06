from __future__ import annotations

import asyncio
from datetime import date

from services.report_api.domain import ConsumerReportRequest
from services.report_api.structured_match_data import (
    collect_daily_structured_match_evidence,
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


def test_daily_structured_match_evidence_uses_beijing_window(monkeypatch) -> None:
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-token")
    calls: list[tuple[str, str, str]] = []

    async def fake_matches(
        competition_code: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        calls.append((competition_code, date_from or "", date_to or ""))
        return {
            "matches": [
                {
                    "id": 1,
                    "home": "Spain",
                    "away": "Austria",
                    "stage": "LAST_32",
                    "status": "FINISHED",
                    "utc_date": "2026-07-02T19:00:00Z",
                    "score": {
                        "duration": "REGULAR",
                        "fullTime": {"home": 3, "away": 0},
                    },
                },
                {
                    "id": 2,
                    "home": "Portugal",
                    "away": "Croatia",
                    "stage": "LAST_32",
                    "status": "FINISHED",
                    "utc_date": "2026-07-02T23:00:00Z",
                    "score": {
                        "duration": "REGULAR",
                        "fullTime": {"home": 2, "away": 1},
                    },
                },
                {
                    "id": 3,
                    "home": "Switzerland",
                    "away": "Algeria",
                    "stage": "LAST_32",
                    "status": "FINISHED",
                    "utc_date": "2026-07-03T03:00:00Z",
                    "score": {
                        "duration": "REGULAR",
                        "fullTime": {"home": 2, "away": 0},
                    },
                },
                {
                    "id": 4,
                    "home": "Australia",
                    "away": "Egypt",
                    "stage": "LAST_32",
                    "status": "FINISHED",
                    "utc_date": "2026-07-03T18:00:00Z",
                    "score": {
                        "duration": "REGULAR",
                        "fullTime": {"home": 1, "away": 1},
                    },
                },
            ]
        }

    monkeypatch.setattr(
        "services.report_api.football_facts.list_competition_matches",
        fake_matches,
    )
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉｜世界杯",
        report_date=date(2026, 7, 3),
        focus=["世界杯"],
    )

    evidence = asyncio.run(collect_daily_structured_match_evidence(request))
    joined = "\n".join(f"{item.title}\n{item.summary}" for item in evidence)

    assert calls == [("WC", "2026-07-02", "2026-07-03")]
    assert len(evidence) == 3
    assert "Spain 3-0 Austria" in joined
    assert "Portugal 2-1 Croatia" in joined
    assert "Switzerland 2-0 Algeria" in joined
    assert "Australia" not in joined
    assert "北京时间 2026-07-03" in joined
    assert "北京时间 2026-07-03 全日窗口" in joined
    assert "阶段 32强淘汰赛（LAST_32）" in joined
    assert "00:00-24:00" not in joined
    assert {item.evidence_kind for item in evidence} == {"structured"}
    assert "不得编写未入证据的时间线" in joined


def test_daily_structured_match_warnings_explain_missing_events(monkeypatch) -> None:
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-token")
    monkeypatch.delenv("SPORTMONKS_API_TOKEN", raising=False)

    async def fake_matches(*_args, **_kwargs):
        return {
            "matches": [
                {
                    "id": 10,
                    "home": "Portugal",
                    "away": "Croatia",
                    "stage": "LAST_32",
                    "status": "FINISHED",
                    "utc_date": "2026-07-02T23:00:00Z",
                    "score": {
                        "duration": "REGULAR",
                        "fullTime": {"home": 2, "away": 1},
                    },
                }
            ]
        }

    monkeypatch.setattr(
        "services.report_api.football_facts.list_competition_matches",
        fake_matches,
    )
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉｜世界杯",
        report_date=date(2026, 7, 3),
    )

    from services.report_api.structured_match_data import (
        collect_daily_structured_match_evidence_with_warnings,
    )

    evidence, warnings = asyncio.run(
        collect_daily_structured_match_evidence_with_warnings(request)
    )

    assert len(evidence) == 1
    assert any("缺少结构化进球者" in warning for warning in warnings)
    assert any("Sportmonks 未配置" in warning for warning in warnings)
