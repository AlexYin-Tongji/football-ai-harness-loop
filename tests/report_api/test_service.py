import asyncio
from collections.abc import Iterable

from services.report_api.domain import (
    MatchModelContext,
    RecentMatchSample,
    ReportRequest,
)
from services.report_api.providers.base import LLMProviderError, LLMRequest, LLMResult
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


class CapturingDailyProvider:
    def __init__(self) -> None:
        self.final_request: LLMRequest | None = None

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        evidence_id = request.metadata["evidence_ids"][0]
        if request.purpose.startswith("daily_research:"):
            desk = request.metadata["desk"]
            return LLMResult(
                output={
                    "desk": desk,
                    "key_items": [
                        {"claim": "分桌研究要点", "evidence_ids": [evidence_id]}
                    ],
                    "rumor_items": [],
                    "conflicts": [],
                    "unknowns": [],
                },
                provider="stub",
                model=request.model,
            )
        if request.purpose.startswith("daily_desk_write:"):
            desk = request.metadata["desk"]
            return LLMResult(
                output={
                    "desk": desk,
                    "heading": "赛场脉搏" if desk == "match_news" else "转会雷达",
                    "summary": "分桌草稿摘要。",
                    "sections": [
                        {
                            "heading": "分桌段落",
                            "body": "分桌已经读过完整证据，这里保留可合稿的事实。",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "warnings": [],
                },
                provider="stub",
                model=request.model,
            )
        self.final_request = request
        return LLMResult(
            output={
                "title": "今日球脉",
                "executive_summary": "总编辑基于分桌草稿生成的摘要。",
                "sections": [
                    {
                        "heading": "赛场脉搏",
                        "body": "总编辑保留栏目边界并引用来源。",
                        "evidence_ids": [evidence_id],
                    }
                ],
                "warnings": [],
                "prediction": None,
            },
            provider="stub",
            model=request.model,
        )


class FinalTransientThenStableProvider(CapturingDailyProvider):
    def __init__(self) -> None:
        super().__init__()
        self.final_attempts: list[LLMRequest] = []

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        final_purposes = {
            "daily_football_digest",
            "daily_football_digest:stable_final",
        }
        if request.purpose in final_purposes:
            self.final_attempts.append(request)
            if request.purpose == "daily_football_digest":
                raise LLMProviderError(
                    "DeepSeek request failed: RemoteProtocolError",
                    kind="transient",
                )
        return await super().generate_json(request)


class FinalAndStableBothFailProvider(CapturingDailyProvider):
    def __init__(self) -> None:
        super().__init__()
        self.final_attempts: list[str] = []

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        final_purposes = {
            "daily_football_digest",
            "daily_football_digest:stable_final",
        }
        if request.purpose in final_purposes:
            self.final_attempts.append(request.purpose)
            raise LLMProviderError(
                "DeepSeek request failed: RemoteProtocolError",
                kind="transient",
            )
        return await super().generate_json(request)


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


def daily_digest_request_with_long_evidence() -> ReportRequest:
    return ReportRequest.model_validate(
        {
            "report_type": "daily_football_digest",
            "subject": "今日球脉",
            "report_date": "2026-07-01",
            "data_cutoff": "2026-07-01T08:00:00Z",
            "length": "deep",
            "focus": ["世界杯", "转会"],
            "evidence": [
                {
                    "id": f"ev-{index}",
                    "title": f"Football update {index}",
                    "url": f"https://example.com/update-{index}",
                    "published_at": "2026-07-01T07:00:00Z",
                    "source_name": "Approved source",
                    "summary": "Long evidence summary. " * 160,
                }
                for index in range(1, 25)
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
    request.evidence[
        0
    ].summary = "Opta rates the home side's chance of victory in normal time at 73.9%."
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


def test_service_normalizes_external_alias_and_missing_qualification() -> None:
    request = request_payload()
    request.evidence[
        0
    ].summary = "Opta rates the home side's chance of victory in normal time at 73.9%."
    output = report_output()
    output["prediction"]["qualification"] = None
    output["prediction"]["external_predictions"] = [
        {
            "source_name": "Opta (via The Guardian)",
            "summary": "Opta gives the home side a 73.9% chance.",
            "evidence_ids": ["ev-1"],
        }
    ]
    provider = SequenceProvider([output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request))

    assert result.attempts == 1
    assert result.report.prediction is not None
    assert result.report.prediction.external_predictions[0].source_name == "Opta"
    qualification = result.report.prediction.qualification
    assert qualification is not None
    assert abs(qualification.home + qualification.away - 1) < 0.001
    assert any("系统以90分钟平局双方均分" in item for item in result.report.warnings)


def test_service_accepts_evidence_backed_player_card_and_match_timeline() -> None:
    request = request_payload()
    request.evidence[
        0
    ].summary = "Example Player scored his 12th goal in the 72nd minute to make it 2-1."
    output = report_output()
    output["enrichment"] = {
        "player_spotlights": [
            {
                "name": "Example Player",
                "related_clubs": ["Club A", "Club B"],
                "position": "前锋",
                "narrative": "他是这次转会与比赛叙事的核心人物。",
                "metrics": [{"label": "进球", "value": "12"}],
                "evidence_ids": ["ev-1"],
            }
        ],
        "match_timeline": [
            {
                "minute": "72",
                "event_type": "goal",
                "player": "Example Player",
                "team": "Club A",
                "score_after": "2-1",
                "description": "Example Player 打入改变比赛走势的一球。",
                "evidence_ids": ["ev-1"],
            }
        ],
        "media_assets": [],
    }
    provider = SequenceProvider([output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request))

    assert result.report.enrichment.player_spotlights[0].metrics[0].value == "12"
    assert result.report.enrichment.match_timeline[0].minute == "72"


def test_service_prunes_unsupported_optional_enrichment() -> None:
    request = request_payload()
    request.evidence[0].summary = "Example Player completed a transfer."
    output = report_output()
    output["enrichment"] = {
        "player_spotlights": [
            {
                "name": "Example Player",
                "related_clubs": ["Club A"],
                "position": "前锋",
                "narrative": "他是这次转会叙事的核心人物。",
                "metrics": [{"label": "进球", "value": "99"}],
                "evidence_ids": ["ev-1"],
            }
        ],
        "match_timeline": [
            {
                "minute": "72",
                "event_type": "goal",
                "player": "Example Player",
                "team": "Club A",
                "score_after": "2-1",
                "description": "Example Player 打入一球。",
                "evidence_ids": ["ev-1"],
            }
        ],
        "media_assets": [],
    }
    provider = SequenceProvider([output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request))

    assert result.report.enrichment.player_spotlights[0].metrics == []
    assert result.report.enrichment.match_timeline == []
    assert any("人物卡数据" in item for item in result.report.warnings)
    assert any("比赛时间线" in item for item in result.report.warnings)


def test_daily_digest_final_editor_uses_compacted_evidence() -> None:
    provider = CapturingDailyProvider()
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        flash_model="deepseek-v4-flash",
        max_output_tokens=6000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(daily_digest_request_with_long_evidence()))

    assert result.report.title == "今日球脉"
    assert provider.final_request is not None
    final_text = "\n".join(
        message["content"] for message in provider.final_request.messages
    )
    assert "ev-24" in final_text
    assert "Harness 生成的确定性合稿提纲" in final_text
    assert len(final_text) < 45_000
    assert provider.final_request.max_output_tokens == 4500


def test_daily_digest_recovers_final_transient_with_stable_mode() -> None:
    provider = FinalTransientThenStableProvider()
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        flash_model="deepseek-v4-flash",
        max_output_tokens=6000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(daily_digest_request_with_long_evidence()))

    assert result.report.title == "今日球脉"
    assert result.attempts == 6
    assert [item.purpose for item in provider.final_attempts] == [
        "daily_football_digest",
        "daily_football_digest:stable_final",
    ]
    stable_request = provider.final_attempts[-1]
    assert stable_request.thinking_enabled is False
    assert stable_request.max_output_tokens == 3300
    assert any("稳定合稿模式" in item for item in result.report.warnings)


def test_daily_digest_uses_deterministic_finalizer_when_all_final_llm_fails() -> None:
    provider = FinalAndStableBothFailProvider()
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        flash_model="deepseek-v4-flash",
        max_output_tokens=6000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(daily_digest_request_with_long_evidence()))

    assert result.provider == "harness"
    assert result.model == "deterministic-daily-finalizer"
    assert result.attempts == 7
    assert provider.final_attempts == [
        "daily_football_digest",
        "daily_football_digest:stable_final",
    ]
    assert "保守合稿版" in result.report.title
    assert len(result.report.sections) >= 1
    assert any("保守版本" in item for item in result.report.warnings)
