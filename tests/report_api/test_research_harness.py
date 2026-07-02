from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from services.report_api.domain import ConsumerReportRequest, Evidence, MediaAsset
from services.report_api.providers.base import LLMRequest, LLMResult
from services.report_api.providers.mock import MockProvider
from services.report_api.research_harness import (
    ResearchHarness,
    fallback_research_plan,
)


def test_fallback_research_plan_handles_chinese_subject() -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉｜世界杯与夏季转会窗",
        report_date=date(2026, 7, 2),
    )

    plan = fallback_research_plan(request)

    assert plan.queries
    assert any("FIFA World Cup" in item.query for item in plan.queries)
    assert all(item.sources for item in plan.queries)


def test_research_harness_allows_discovery_only_bundle(monkeypatch) -> None:
    request = ConsumerReportRequest(
        report_type="transfer_daily",
        subject="今日转会传闻",
        report_date=date(2026, 7, 2),
    )
    now = datetime.now(UTC)
    discovery = Evidence(
        id="lead-1",
        title="Club linked with forward",
        url="https://www.bbc.com/sport/football/example",
        published_at=now,
        source_name="BBC Sport",
        summary="发现线索：Club linked with forward。",
        source_id="bbc-sport",
        evidence_kind="discovery",
        verification_status="unverified_lead",
        source_independence_key="bbc-sport",
    )

    async def fake_guardian(*_args, **_kwargs):
        return []

    async def fake_bbc(*_args, **_kwargs):
        return []

    async def fake_gdelt(*_args, **_kwargs):
        return [discovery]

    async def fake_newsapi(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "services.report_api.research_harness.collect_guardian_evidence",
        fake_guardian,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_bbc_evidence",
        fake_bbc,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_gdelt_evidence",
        fake_gdelt,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_newsapi_evidence",
        fake_newsapi,
    )
    harness = ResearchHarness(MockProvider(), model="deepseek-v4-flash")

    bundle = asyncio.run(harness.collect(request, max_items=8))

    assert len(bundle.evidence) == 1
    assert bundle.evidence[0].verification_status == "unverified_lead"
    assert any("发现层线索" in item for item in bundle.warnings)
    assert [item.name for item in bundle.layer_runs] == [
        "url_collection",
        "evidence_refinement",
        "enhancement",
        "writing_handoff",
    ]


class MediaNeedProvider(MockProvider):
    async def generate_json(self, request: LLMRequest) -> LLMResult:
        if request.purpose == "enhancement_plan":
            evidence_id = request.metadata["evidence_ids"][0]
            return LLMResult(
                output={
                    "needs": [
                        {
                            "kind": "licensed_image",
                            "target": "Example Player",
                            "reason": "人物卡需要许可图片。",
                            "evidence_ids": [evidence_id],
                            "priority": 2,
                        },
                        {
                            "kind": "gif",
                            "target": "Example Player highlight",
                            "reason": "用户可能想看动图。",
                            "evidence_ids": [evidence_id],
                            "priority": 5,
                        },
                    ],
                    "warnings": [],
                },
                provider="stub",
                model=request.model,
            )
        return await super().generate_json(request)


def test_enhancement_layer_prefetches_media_and_blocks_gif(monkeypatch) -> None:
    request = ConsumerReportRequest(
        report_type="transfer_daily",
        subject="Example Player transfer",
        report_date=date(2026, 7, 2),
    )
    now = datetime.now(UTC)
    source = Evidence(
        id="source-1",
        title="Example Player linked with Club A",
        url="https://www.theguardian.com/football/example-player",
        published_at=now,
        source_name="The Guardian Football",
        summary="Example Player is linked with Club A in a transfer report.",
        source_id="guardian-football-rss",
        trust_tier="S1",
        evidence_kind="verified",
        verification_status="publisher_report",
        source_independence_key="guardian-football",
    )

    async def fake_guardian(*_args, **_kwargs):
        return [source]

    async def fake_empty(*_args, **_kwargs):
        return []

    async def fake_image(*_args, **_kwargs):
        return MediaAsset(
            asset_type="image",
            title="Example Player",
            url="https://commons.wikimedia.org/wiki/File:Example_Player.jpg",
            thumbnail_url="https://upload.wikimedia.org/example.jpg",
            provider="Wikimedia Commons",
            license="CC BY-SA 4.0",
            attribution="Photographer",
            rights_status="review_required",
            relevance_status="metadata_match",
        )

    monkeypatch.setattr(
        "services.report_api.research_harness.collect_guardian_evidence",
        fake_guardian,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_bbc_evidence",
        fake_empty,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_gdelt_evidence",
        fake_empty,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_newsapi_evidence",
        fake_empty,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.search_commons_player_image",
        fake_image,
    )
    harness = ResearchHarness(MediaNeedProvider(), model="deepseek-v4-flash")

    bundle = asyncio.run(harness.collect(request, max_items=8))

    assert len(bundle.media_assets) == 1
    assert bundle.media_assets[0].provider == "Wikimedia Commons"
    assert any("GIF/比赛动图" in item for item in bundle.warnings)
