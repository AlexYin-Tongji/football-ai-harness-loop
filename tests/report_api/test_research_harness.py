from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest

from services.report_api.article_reader import ArticleExcerpt
from services.report_api.domain import (
    ConsumerReportRequest,
    EditorialColumnPlan,
    Evidence,
)
from services.report_api.evidence import EvidenceCollectionError
from services.report_api.providers.base import LLMRequest, LLMResult
from services.report_api.providers.mock import MockProvider
from services.report_api.research_harness import (
    ColumnTeamLoopResult,
    EnhancementHarness,
    EnhancementPlan,
    EvidenceRefinementHarness,
    LayerLoopSummary,
    LeaderReviewHarness,
    ResearchHarness,
    UrlCollectionHarness,
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


def test_fallback_research_plan_expands_chinese_club_focus() -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉｜世界杯与夏季转会窗",
        report_date=date(2026, 7, 3),
        focus=["热刺"],
    )

    plan = fallback_research_plan(request)

    queries = " ".join(item.query for item in plan.queries)
    assert "Spurs" in queries
    assert "Tottenham" in queries
    assert "Spurs" in plan.queries[0].query or "Tottenham" in plan.queries[0].query


def test_url_layer_preserves_focus_queries_after_model_plan() -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉｜世界杯与夏季转会窗",
        report_date=date(2026, 7, 3),
        focus=["热刺"],
    )
    layer = UrlCollectionHarness(
        MockProvider(), model="deepseek-v4-flash", max_output_tokens=1000
    )

    plan = asyncio.run(layer.plan(request))

    queries = " ".join(item.query for item in plan.queries)
    assert "Spurs" in queries
    assert "Tottenham" in queries
    assert "Spurs" in plan.queries[0].query or "Tottenham" in plan.queries[0].query


class LateFocusPlanProvider(MockProvider):
    async def generate_json(self, request: LLMRequest) -> LLMResult:
        if request.purpose == "research_plan":
            return LLMResult(
                output={
                    "queries": [
                        {
                            "query": "Brazil Portugal World Cup match report",
                            "purpose": "match_news",
                            "sources": ["rss", "gdelt", "newsapi"],
                        },
                        {
                            "query": "Argentina France World Cup highlights",
                            "purpose": "match_news",
                            "sources": ["rss", "gdelt", "newsapi"],
                        },
                        {
                            "query": "Tottenham transfer interest bid offer",
                            "purpose": "transfer_market",
                            "sources": ["rss", "gdelt", "newsapi"],
                        },
                    ],
                    "min_items": 1,
                    "allow_discovery_only": True,
                },
                provider="stub",
                model=request.model,
            )
        return await super().generate_json(request)


def test_url_layer_promotes_late_focus_query_before_budget_queries() -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉｜世界杯与夏季转会窗",
        report_date=date(2026, 7, 3),
        focus=["热刺"],
    )
    layer = UrlCollectionHarness(
        LateFocusPlanProvider(), model="deepseek-v4-flash", max_output_tokens=1000
    )

    plan = asyncio.run(layer.plan(request))

    assert plan.queries[0].query == "Tottenham transfer interest bid offer"


def test_leader_adds_required_match_group_when_model_omits_it() -> None:
    fallback = [
        EditorialColumnPlan(
            column_id="match_report",
            title="赛场主线",
            category="match",
            specialist_group="match_report",
            priority=1,
            evidence_ids=["match-1"],
        ),
        EditorialColumnPlan(
            column_id="transfer_intel",
            title="转会市场",
            category="transfer",
            specialist_group="transfer_intel",
            priority=2,
            evidence_ids=["transfer-1"],
        ),
    ]
    model_columns = [fallback[1]]

    columns = LeaderReviewHarness._ensure_required_groups(
        model_columns, fallback, ["match", "transfer"]
    )

    assert [column.specialist_group for column in columns] == [
        "match_report",
        "transfer_intel",
    ]


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
        "services.report_api.research_harness.collect_guardian_search_evidence",
        fake_guardian,
    )
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
    layer_names = [item.name for item in bundle.layer_runs]
    assert "url_collection" in layer_names
    assert "evidence_refinement" in layer_names
    assert "leader_review" in layer_names
    assert "column_team_loop" in layer_names
    assert layer_names[-1] == "writing_handoff"
    assert all(item.checkpoints for item in bundle.layer_runs)


def test_daily_url_layer_reads_deeper_rss_for_breadth(monkeypatch) -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉｜世界杯与夏季转会窗",
        report_date=date(2026, 7, 3),
        focus=["热刺"],
    )
    seen_limits: list[int] = []

    async def fake_empty(*_args, **_kwargs):
        return []

    async def fake_bbc(_request, *, max_items: int):
        seen_limits.append(max_items)
        return []

    monkeypatch.setattr(
        "services.report_api.research_harness.collect_guardian_search_evidence",
        fake_empty,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_guardian_evidence",
        fake_empty,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_bbc_evidence",
        fake_bbc,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_gdelt_evidence",
        fake_empty,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_newsapi_evidence",
        fake_empty,
    )
    layer = UrlCollectionHarness(
        MockProvider(), model="deepseek-v4-flash", max_output_tokens=1000
    )

    asyncio.run(layer.collect(request, max_items=16))

    assert seen_limits
    assert max(seen_limits) >= 15


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


class CapturingRefinementProvider(MockProvider):
    def __init__(self) -> None:
        self.request: LLMRequest | None = None

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        if request.purpose == "evidence_refinement":
            self.request = request
            evidence_id = request.metadata["candidate_ids"][0]
            return LLMResult(
                output={
                    "items": [
                        {
                            "source_evidence_id": evidence_id,
                            "original_url": "https://www.theguardian.com/football/example",
                            "title": "Example transfer update",
                            "concise_summary": "该来源报道 Example Player 转会进展。",
                            "key_points": ["原链接随 evidence_id 保留"],
                        }
                    ],
                    "warnings": [],
                },
                provider="stub",
                model=request.model,
            )
        return await super().generate_json(request)


def test_refinement_prompt_requires_original_url_contract() -> None:
    request = ConsumerReportRequest(
        report_type="transfer_daily",
        subject="Example Player transfer",
        report_date=date(2026, 7, 3),
    )
    now = datetime.now(UTC)
    source = Evidence(
        id="source-1",
        title="Example Player transfer update",
        url="https://www.theguardian.com/football/example",
        published_at=now,
        source_name="The Guardian Football",
        summary="Example Player transfer update.",
        source_id="guardian-football-rss",
    )
    provider = CapturingRefinementProvider()
    harness = EvidenceRefinementHarness(
        provider, model="deepseek-v4-flash", max_output_tokens=1000
    )

    result = asyncio.run(harness.refine(request, [source], max_items=1))

    assert len(result.evidence) == 1
    assert result.evidence[0].title == "Example Player transfer update"
    assert "Example Player transfer update." in result.evidence[0].summary
    assert provider.request is not None
    prompt = provider.request.messages[0]["content"]
    assert "original_url" in prompt
    assert "资料精简 SKILL" in prompt


def test_refinement_reads_article_excerpt_without_storing_full_body() -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="Diogo Jota tribute",
        report_date=date(2026, 7, 3),
    )
    now = datetime.now(UTC)
    source = Evidence(
        id="source-1",
        title="Victorious Portugal pay emotional tribute to Jota",
        url="https://www.bbc.co.uk/sport/football/articles/example",
        published_at=now,
        source_name="BBC Sport Football",
        summary="One year after his death, Portugal are using Diogo Jota's memory.",
        source_id="bbc-football-rss",
    )
    provider = CapturingRefinementProvider()

    async def fake_article_reader(_url: str) -> ArticleExcerpt:
        return ArticleExcerpt(
            url=str(source.url),
            text=(
                "One year after Diogo Jota's death, Portugal said his memory "
                "helped inspire their World Cup performance."
            ),
            chars_read=116,
        )

    harness = EvidenceRefinementHarness(
        provider,
        model="deepseek-v4-flash",
        max_output_tokens=1000,
        article_reader=fake_article_reader,
    )

    result = asyncio.run(harness.refine(request, [source], max_items=1))

    assert provider.request is not None
    payload = provider.request.messages[1]["content"]
    assert "article_excerpt" in payload
    assert "helped inspire their World Cup performance" in payload
    assert result.loop.tool_rounds == 1
    assert all(
        "helped inspire their World Cup performance" not in item.summary
        for item in result.evidence
    )


def test_critical_entity_evidence_is_injected_and_preserved(monkeypatch) -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="若塔 今日消息",
        report_date=date(2026, 7, 3),
    )

    async def fake_empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "services.report_api.research_harness.collect_guardian_evidence",
        fake_empty,
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

    harness = ResearchHarness(MockProvider(), model="deepseek-v4-flash")
    bundle = asyncio.run(harness.collect(request, max_items=4))

    assert bundle.evidence[0].id == "critical-diogo-jota"
    assert "passed away" in bundle.evidence[0].summary
    assert any(
        item.id == "critical-diogo-jota" and item.source_name == "Liverpool FC"
        for item in bundle.evidence
    )


def test_critical_entity_evidence_is_injected_from_discovered_sources(
    monkeypatch,
) -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="World Cup knockout news",
        report_date=date(2026, 7, 3),
        focus=["Portugal Croatia"],
    )
    now = datetime.now(UTC)
    match_story = Evidence(
        id="guardian-portugal-croatia",
        title="Ramos sends Portugal into last 16 against Croatia",
        url="https://www.theguardian.com/football/portugal-croatia",
        published_at=now,
        source_name="The Guardian Football",
        summary="Portugal beat Croatia in a World Cup knockout match.",
        source_id="guardian-football",
    )
    jota_story = Evidence(
        id="bbc-jota-tribute",
        title="Victorious Portugal pay emotional tribute to Jota",
        url="https://www.bbc.co.uk/sport/football/articles/example",
        published_at=now,
        source_name="BBC Sport",
        summary="Portugal paid emotional tribute to Diogo Jota after the match.",
        source_id="bbc-sport",
    )

    async def fake_guardian(*_args, **_kwargs):
        return [match_story]

    async def fake_bbc(*_args, **_kwargs):
        return [jota_story]

    async def fake_gdelt(*_args, **_kwargs):
        return []

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
    bundle = asyncio.run(harness.collect(request, max_items=4))

    assert any(item.id == "guardian-portugal-croatia" for item in bundle.evidence)
    assert any(item.id == "bbc-jota-tribute" for item in bundle.evidence)
    assert not any(item.id.startswith("critical-") for item in bundle.evidence)
    assert any(item.name == "leader_review" for item in bundle.layer_runs)
    assert bundle.editorial_plan


def test_leader_blocks_broad_digest_when_only_critical_entity_remains(
    monkeypatch,
) -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="World Cup knockout news",
        report_date=date(2026, 7, 3),
        focus=["Portugal Croatia"],
    )
    now = datetime.now(UTC)
    jota_story = Evidence(
        id="bbc-jota-tribute",
        title="Victorious Portugal pay emotional tribute to Jota",
        url="https://www.bbc.co.uk/sport/football/articles/example",
        published_at=now,
        source_name="BBC Sport",
        summary="Portugal paid emotional tribute to Diogo Jota after the match.",
        source_id="bbc-sport",
    )

    async def fake_empty(*_args, **_kwargs):
        return []

    async def fake_bbc(*_args, **_kwargs):
        return [jota_story]

    monkeypatch.setattr(
        "services.report_api.research_harness.collect_guardian_search_evidence",
        fake_empty,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_guardian_evidence",
        fake_empty,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_bbc_evidence",
        fake_bbc,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_gdelt_evidence",
        fake_empty,
    )
    monkeypatch.setattr(
        "services.report_api.research_harness.collect_newsapi_evidence",
        fake_empty,
    )

    harness = ResearchHarness(MockProvider(), model="deepseek-v4-flash")

    with pytest.raises(EvidenceCollectionError, match="关键人物护栏"):
        asyncio.run(harness.collect(request, max_items=4))


def test_internal_layer_warnings_are_not_reader_warnings() -> None:
    warnings = ResearchHarness._reader_warnings(
        [
            "资料精简层模型不可用，已使用确定性短摘要兜底。",
            "未能补到结构化球员资料：Example Player",
            "关键画面需人工补充：Example goal。",
        ]
    )

    assert warnings == []


def test_enhancement_layer_filters_media_needs(monkeypatch) -> None:
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
    harness = ResearchHarness(MediaNeedProvider(), model="deepseek-v4-flash")

    bundle = asyncio.run(harness.collect(request, max_items=8))

    assert bundle.media_assets == []
    assert not any("许可图片" in item for item in bundle.warnings)
    assert not any("GIF/比赛动图" in item for item in bundle.warnings)


def test_deterministic_visual_needs_are_disabled() -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="World Cup knockout news",
        report_date=date(2026, 7, 3),
    )
    now = datetime.now(UTC)
    evidence = [
        Evidence(
            id="portugal-croatia",
            title=(
                "Ramos sends Portugal into last 16 as VAR drama caps wild finish "
                "against Croatia"
            ),
            url="https://www.theguardian.com/football/portugal-croatia",
            published_at=now,
            source_name="The Guardian Football",
            summary=(
                "Portugal beat Croatia after a late VAR call in a World Cup "
                "match report."
            ),
            source_id="guardian-football",
        ),
        Evidence(
            id="spain-austria",
            title="Oyarzabal scores twice as Spain cruise past Austria",
            url="https://www.bbc.co.uk/sport/football/videos/spain-austria",
            published_at=now,
            source_name="BBC Sport Football",
            summary=(
                "Spain beat Austria with two goals and official highlights available."
            ),
            source_id="bbc-football",
        ),
    ]

    plan = EnhancementHarness._with_deterministic_visual_needs(
        request, evidence, EnhancementPlan(needs=[])
    )

    assert plan.needs == []


def test_leader_fallback_routes_upcoming_fixture_to_off_field() -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="World Cup daily",
        report_date=date(2026, 7, 4),
    )
    evidence = [
        Evidence(
            id="england-mexico-preview",
            title=(
                "England get hostile welcome on arrival at Mexico City hotel "
                "for World Cup showdown"
            ),
            url="https://www.theguardian.com/football/example",
            published_at=datetime.now(UTC),
            source_name="The Guardian Football",
            summary=(
                "England arrived at their hotel ahead of the Mexico match. "
                "Kick-off is scheduled for Sunday, 5 July."
            ),
            source_id="guardian-football",
        )
    ]
    layer = LeaderReviewHarness(
        MockProvider(), model="deepseek-v4-flash", max_output_tokens=1000
    )

    columns = layer._fallback_editorial_plan(request, evidence)

    assert columns
    assert columns[0].specialist_group == "off_field"
    assert not any(column.specialist_group == "match_report" for column in columns)


def test_final_merge_preserves_structured_daily_matches() -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉｜世界杯",
        report_date=date(2026, 7, 3),
        focus=["世界杯"],
    )
    now = datetime(2026, 7, 3, 8, tzinfo=UTC)
    structured = [
        Evidence(
            id=f"football-data-match-{index}",
            title=title,
            url="https://www.football-data.org/",
            published_at=now,
            source_name="football-data.org",
            summary=title,
            source_id="football-data-org",
            trust_tier="S1_structured_provider",
            evidence_kind="structured",
            verification_status="corroborated",
        )
        for index, title in enumerate(
            [
                "结构化赛果｜Spain 3-0 Austria",
                "结构化赛果｜Portugal 2-1 Croatia",
                "结构化赛果｜Switzerland 2-0 Algeria",
            ],
            start=1,
        )
    ]
    noisy_news = [
        Evidence(
            id=f"news-{index}",
            title=f"World Cup transfer and context story {index}",
            url=f"https://www.bbc.co.uk/sport/football/{index}",
            published_at=now,
            source_name="BBC Sport Football",
            summary="Transfer context and general football background.",
            source_id="bbc-football-rss",
        )
        for index in range(6)
    ]
    column = EditorialColumnPlan(
        column_id="transfer",
        title="转会市场",
        category="transfer",
        specialist_group="transfer_intel",
        priority=1,
        evidence_ids=[item.id for item in noisy_news],
    )
    result = ColumnTeamLoopResult(
        column=column,
        evidence=noisy_news,
        media_assets=[],
        warnings=[],
        source_attempts={},
        loop=LayerLoopSummary(
            name="column_team_loop",
            label="小组循环：转会市场",
            status="completed",
        ),
    )

    merged = ResearchHarness._merge_column_team_evidence(
        request,
        structured,
        [result],
        max_items=4,
    )

    assert [item.id for item in merged[:3]] == [
        "football-data-match-1",
        "football-data-match-2",
        "football-data-match-3",
    ]
