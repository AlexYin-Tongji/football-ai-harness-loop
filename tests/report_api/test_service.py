import asyncio
from collections.abc import Iterable

from services.report_api.domain import (
    DeskDraft,
    GeneratedReport,
    MatchModelContext,
    MediaAsset,
    RecentMatchSample,
    ReportRequest,
)
from services.report_api.evidence_state import is_completed_match_evidence
from services.report_api.providers.base import LLMProviderError, LLMRequest, LLMResult
from services.report_api.service import (
    VISIBLE_SCORE_RE,
    ReportService,
    _assign_section_categories,
    _repair_report_scorelines,
)


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


class CoverageRetryDailyProvider(CapturingDailyProvider):
    def __init__(self) -> None:
        super().__init__()
        self.final_calls = 0

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        if request.purpose == "daily_football_digest":
            self.final_calls += 1
            sections = [
                {
                    "heading": "葡萄牙淘汰赛惊险晋级",
                    "body": "葡萄牙与克罗地亚的淘汰赛因 VAR 判罚成为焦点。",
                    "evidence_ids": ["ev-match"],
                    "category": "match",
                }
            ]
            if self.final_calls >= 2:
                sections.append(
                    {
                        "heading": "热刺继续推进转会投入",
                        "body": (
                            "热刺的转会投入仍在继续，相关报道提到 Spurs 的支出计划。"
                        ),
                        "evidence_ids": ["ev-transfer"],
                        "category": "transfer",
                    }
                )
            return LLMResult(
                output={
                    "title": "今日球脉",
                    "executive_summary": "总编辑按栏目完成合稿。",
                    "sections": sections,
                    "warnings": [],
                    "prediction": None,
                },
                provider="stub",
                model=request.model,
            )
        return await super().generate_json(request)


class CoverageNeverDailyProvider(CapturingDailyProvider):
    def __init__(self) -> None:
        super().__init__()
        self.final_calls = 0

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        if request.purpose.startswith("daily_desk_write:"):
            desk = request.metadata["desk"]
            evidence_id = request.metadata["evidence_ids"][0]
            body = (
                "\u8461\u8404\u7259\u4e0e\u514b\u7f57\u5730\u4e9a"
                "\u7684\u6bd4\u8d5b\u56e0 VAR \u5224\u7f5a"
                "\u6210\u4e3a\u7126\u70b9\u3002"
                if desk == "match_report"
                else "\u70ed\u523a\u7684\u8f6c\u4f1a\u652f\u51fa"
                "\u62a5\u9053\u4ecd\u5728\u7ee7\u7eed\u3002"
            )
            return LLMResult(
                output={
                    "desk": desk,
                    "heading": "deterministic desk",
                    "summary": "\u5206\u684c\u8349\u7a3f\u6458\u8981\u3002",
                    "sections": [
                        {
                            "heading": "deterministic section",
                            "body": body,
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "warnings": [],
                },
                provider="stub",
                model=request.model,
            )
        if request.purpose == "daily_football_digest":
            self.final_calls += 1
            return LLMResult(
                output={
                    "title": "\u4eca\u65e5\u7403\u8109",
                    "executive_summary": (
                        "\u603b\u7f16\u8f91\u6f0f\u6389\u4e86\u4e00\u4e2a"
                        "\u680f\u76ee\u3002"
                    ),
                    "sections": [
                        {
                            "heading": "\u8461\u8404\u7259 VAR \u7126\u70b9",
                            "body": (
                                "\u8461\u8404\u7259\u4e0e\u514b\u7f57\u5730"
                                "\u4e9a\u7684\u6bd4\u8d5b\u56e0 VAR "
                                "\u5224\u7f5a\u6210\u4e3a\u7126\u70b9\u3002"
                            ),
                            "evidence_ids": ["ev-match"],
                            "category": "match",
                        }
                    ],
                    "warnings": [],
                    "prediction": None,
                },
                provider="stub",
                model=request.model,
            )
        return await super().generate_json(request)


class NumericClaimFailureDailyProvider(CapturingDailyProvider):
    def __init__(self) -> None:
        super().__init__()
        self.final_calls = 0

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        if request.purpose.startswith("daily_research:"):
            return LLMResult(
                output={
                    "desk": request.metadata["desk"],
                    "key_items": [
                        {
                            "claim": "Argentina beat Cape Verde 3-2.",
                            "evidence_ids": ["ev-cape"],
                        }
                    ],
                    "rumor_items": [],
                    "conflicts": [],
                    "unknowns": [],
                },
                provider="stub",
                model=request.model,
            )
        if request.purpose.startswith("daily_desk_write:"):
            return LLMResult(
                output={
                    "desk": request.metadata["desk"],
                    "heading": "佛得角 vs 阿根廷",
                    "summary": "阿根廷3-2击败佛得角，进入下一轮。",
                    "sections": [
                        {
                            "heading": "佛得角 vs 阿根廷",
                            "body": "阿根廷3-2击败佛得角，进入下一轮。",
                            "evidence_ids": ["ev-cape"],
                            "category": "match",
                        }
                    ],
                    "warnings": [],
                },
                provider="stub",
                model=request.model,
            )
        if request.purpose == "daily_football_digest":
            self.final_calls += 1
            return LLMResult(
                output={
                    "title": "今日球脉",
                    "executive_summary": "阿根廷与佛得角的比赛成为今日主线。",
                    "sections": [
                        {
                            "heading": "佛得角 vs 阿根廷：阿根廷晋级",
                            "body": "阿根廷3-2击败佛得角，晋级16强。",
                            "evidence_ids": ["ev-cape"],
                            "category": "match",
                        }
                    ],
                    "warnings": [],
                    "prediction": None,
                },
                provider="stub",
                model=request.model,
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


def jota_request_payload() -> ReportRequest:
    return ReportRequest.model_validate(
        {
            "report_type": "world_cup_daily",
            "subject": "若塔 今日消息",
            "report_date": "2026-07-03",
            "data_cutoff": "2026-07-03T08:00:00Z",
            "evidence": [
                {
                    "id": "critical-diogo-jota",
                    "title": "Diogo Jota: 1996-2025",
                    "url": "https://www.liverpoolfc.com/news/diogo-jota-1996-2025",
                    "published_at": "2025-07-03T00:00:00Z",
                    "source_name": "Liverpool FC",
                    "source_id": "liverpool",
                    "trust_tier": "S0",
                    "evidence_kind": "official",
                    "verification_status": "official",
                    "summary": (
                        "Liverpool FC confirmed that Diogo Jota passed away at "
                        "age 28 following a road traffic accident in Spain."
                    ),
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


def daily_digest_request_with_editorial_plan() -> ReportRequest:
    return ReportRequest.model_validate(
        {
            "report_type": "daily_football_digest",
            "subject": "今日球脉｜世界杯与夏季转会窗",
            "report_date": "2026-07-03",
            "data_cutoff": "2026-07-03T08:00:00Z",
            "focus": ["世界杯", "热刺"],
            "evidence": [
                {
                    "id": "ev-match",
                    "title": "Portugal beat Croatia after VAR drama",
                    "url": "https://www.theguardian.com/football/portugal-croatia",
                    "published_at": "2026-07-03T07:00:00Z",
                    "source_name": "The Guardian Football",
                    "summary": "Portugal beat Croatia in a World Cup match.",
                },
                {
                    "id": "ev-transfer",
                    "title": "Why Spurs' statement spending appears set to continue",
                    "url": "https://www.bbc.co.uk/sport/football/articles/spurs",
                    "published_at": "2026-07-03T07:00:00Z",
                    "source_name": "BBC Sport Football",
                    "summary": "Tottenham transfer spending is expected to continue.",
                },
            ],
            "editorial_plan": [
                {
                    "column_id": "match_report",
                    "title": "赛场主线",
                    "category": "match",
                    "specialist_group": "match_report",
                    "priority": 1,
                    "evidence_ids": ["ev-match"],
                },
                {
                    "column_id": "transfer_intel",
                    "title": "转会市场",
                    "category": "transfer",
                    "specialist_group": "transfer_intel",
                    "priority": 2,
                    "evidence_ids": ["ev-transfer"],
                },
            ],
        }
    )


def daily_digest_request_with_cape_verde_match() -> ReportRequest:
    return ReportRequest.model_validate(
        {
            "report_type": "daily_football_digest",
            "subject": "今日球脉｜世界杯",
            "report_date": "2026-07-04",
            "data_cutoff": "2026-07-04T08:00:00Z",
            "focus": ["世界杯"],
            "evidence": [
                {
                    "id": "ev-cape",
                    "title": "Cape Verde push Argentina in 3-2 defeat",
                    "url": "https://www.theguardian.com/football/cape-verde-argentina",
                    "published_at": "2026-07-04T07:00:00Z",
                    "source_name": "The Guardian Football",
                    "summary": (
                        "Argentina will move on to a quarter-final against Egypt "
                        "after a thrilling 3-2 victory against Cape Verde."
                    ),
                }
            ],
            "editorial_plan": [
                {
                    "column_id": "match_report",
                    "title": "赛场战报",
                    "category": "match",
                    "specialist_group": "match_report",
                    "priority": 1,
                    "evidence_ids": ["ev-cape"],
                }
            ],
        }
    )


def daily_digest_request_with_mixed_match_column() -> ReportRequest:
    return ReportRequest.model_validate(
        {
            "report_type": "daily_football_digest",
            "subject": "今日球脉｜世界杯",
            "report_date": "2026-07-05",
            "data_cutoff": "2026-07-05T08:00:00Z",
            "focus": ["世界杯"],
            "evidence": [
                {
                    "id": "england-mexico-preview",
                    "title": (
                        "England get hostile welcome on arrival at Mexico City "
                        "hotel for World Cup showdown"
                    ),
                    "url": "https://www.theguardian.com/football/england-mexico",
                    "published_at": "2026-07-05T07:00:00Z",
                    "source_name": "The Guardian Football",
                    "summary": (
                        "England arrived ahead of the Mexico match, with kick-off "
                        "scheduled for later in the day."
                    ),
                    "story_cluster_id": "england-mexico-preview",
                },
                {
                    "id": "france-paraguay-report",
                    "title": (
                        "France survive Paraguay's 'disgraceful' and "
                        "'embarrassing' dark arts"
                    ),
                    "url": "https://www.bbc.co.uk/sport/football/france-paraguay",
                    "published_at": "2026-07-05T07:00:00Z",
                    "source_name": "BBC Sport Football",
                    "summary": (
                        "France survived Paraguay and reached the quarter-final "
                        "after a 1-0 World Cup win."
                    ),
                    "story_cluster_id": "france-paraguay-report",
                },
            ],
            "editorial_plan": [
                {
                    "column_id": "mixed-match-report",
                    "title": "赛场战报",
                    "category": "match",
                    "specialist_group": "match_report",
                    "priority": 1,
                    "evidence_ids": [
                        "england-mexico-preview",
                        "france-paraguay-report",
                    ],
                }
            ],
        }
    )


def daily_digest_request_with_context_and_offfield_sources() -> ReportRequest:
    return ReportRequest.model_validate(
        {
            "report_type": "daily_football_digest",
            "subject": "今日球脉｜世界杯与夏季转会窗",
            "report_date": "2026-07-05",
            "data_cutoff": "2026-07-05T08:00:00Z",
            "focus": ["世界杯", "转会进展"],
            "evidence": [
                {
                    "id": "england-mexico-preview",
                    "title": "Mexico could bring the best out of Tuchel's England",
                    "url": "https://www.bbc.co.uk/sport/football/videos/england",
                    "published_at": "2026-07-05T07:00:00Z",
                    "source_name": "BBC Sport Football",
                    "summary": (
                        "BBC pundits discuss how Mexico may challenge England "
                        "ahead of their fixture."
                    ),
                },
                {
                    "id": "aramco-worldcup",
                    "title": (
                        "Aramco makes its presence hurt in the shadow of the World Cup"
                    ),
                    "url": "https://www.theguardian.com/football/aramco",
                    "published_at": "2026-07-05T07:00:00Z",
                    "source_name": "The Guardian Football",
                    "summary": (
                        "The World Cup sponsor Aramco faces community criticism "
                        "over pollution near Houston."
                    ),
                },
                {
                    "id": "mexico-watches",
                    "title": "Mexico return luxury watches gifted by YouTuber",
                    "url": "https://www.bbc.co.uk/sport/football/watches",
                    "published_at": "2026-07-05T07:00:00Z",
                    "source_name": "BBC Sport Football",
                    "summary": (
                        "Mexico returned luxury watches because FIFA rules do "
                        "not allow players to accept third-party gifts."
                    ),
                },
                {
                    "id": "rodrygo-brazil",
                    "title": "Football is at the centre of the universe in Brazil",
                    "url": "https://www.theguardian.com/football/rodrygo",
                    "published_at": "2026-07-05T07:00:00Z",
                    "source_name": "The Guardian Football",
                    "summary": (
                        "Rodrygo discussed Brazilian football culture and how "
                        "deeply fans care about the national team."
                    ),
                },
            ],
            "editorial_plan": [
                {
                    "column_id": "match-preview",
                    "title": "赛场战报",
                    "category": "match",
                    "specialist_group": "match_report",
                    "priority": 1,
                    "evidence_ids": ["england-mexico-preview"],
                },
                {
                    "column_id": "bad-transfer",
                    "title": "转会市场",
                    "category": "transfer",
                    "specialist_group": "transfer_intel",
                    "priority": 2,
                    "evidence_ids": ["aramco-worldcup"],
                },
                {
                    "column_id": "off-field",
                    "title": "场外焦点",
                    "category": "context",
                    "specialist_group": "off_field",
                    "priority": 3,
                    "evidence_ids": ["mexico-watches"],
                },
                {
                    "column_id": "context",
                    "title": "背景脉络",
                    "category": "context",
                    "specialist_group": "context",
                    "priority": 4,
                    "evidence_ids": ["rodrygo-brazil"],
                },
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


def test_service_repairs_injury_absence_for_deceased_player() -> None:
    invalid = {
        "title": "若塔今日消息",
        "executive_summary": "若塔因伤缺席本届剩余比赛，这是错误说法。",
        "sections": [
            {
                "heading": "球队消息",
                "body": "若塔因伤缺席本届剩余比赛。",
                "evidence_ids": ["critical-diogo-jota"],
            }
        ],
        "warnings": [],
        "prediction": None,
    }
    valid = {
        "title": "若塔纪念消息",
        "executive_summary": "利物浦官方信息显示，若塔已于2025年7月3日去世。",
        "sections": [
            {
                "heading": "官方确认",
                "body": (
                    "根据利物浦官方消息，若塔在西班牙交通事故中去世，"
                    "报道应以悼念和事实回溯为主。"
                ),
                "evidence_ids": ["critical-diogo-jota"],
            }
        ],
        "warnings": [],
        "prediction": None,
    }
    provider = SequenceProvider([invalid, valid])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(jota_request_payload()))

    assert provider.calls == 1
    assert "去世" in result.report.executive_summary
    assert "因伤" not in result.report.executive_summary
    assert "去世" in result.report.sections[0].body


def test_service_uses_deterministic_critical_fallback_after_bounded_failure() -> None:
    invalid = {
        "title": "若塔今日消息",
        "executive_summary": "若塔因伤缺席本届剩余比赛，这是错误说法。",
        "sections": [
            {
                "heading": "球队消息",
                "body": "若塔因伤缺席本届剩余比赛。",
                "evidence_ids": ["critical-diogo-jota"],
            }
        ],
        "warnings": [],
        "prediction": None,
    }
    provider = SequenceProvider([invalid])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=1,
    )

    result = asyncio.run(service.generate(jota_request_payload()))

    assert provider.calls == 1
    assert "去世" in result.report.executive_summary
    assert "因伤" not in result.report.executive_summary
    assert result.report.sections[0].evidence_ids == ["critical-diogo-jota"]


def test_service_drops_uncertain_warning_for_official_deceased_player() -> None:
    output = {
        "title": "若塔纪念消息",
        "executive_summary": "利物浦官方信息显示，若塔已于2025年7月3日去世。",
        "sections": [
            {
                "heading": "官方确认",
                "body": "若塔在西班牙交通事故中去世，报道应以官方事实处理。",
                "evidence_ids": ["critical-diogo-jota"],
            }
        ],
        "warnings": ["迪奥戈·若塔逝世为独立官方来源，需等待后续证实与更多细节。"],
        "prediction": None,
    }
    provider = SequenceProvider([output, output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=1,
    )

    result = asyncio.run(service.generate(jota_request_payload()))

    assert result.report.warnings == []


def test_service_repairs_misleading_recent_death_timing() -> None:
    output = {
        "title": "若塔纪念消息",
        "executive_summary": "葡萄牙赛前向若塔致敬。",
        "sections": [
            {
                "heading": "葡萄牙致敬若塔",
                "body": "葡萄牙赛后向赛前因车祸去世的前锋迪奥戈·若塔致以哀思。",
                "evidence_ids": ["critical-diogo-jota"],
            }
        ],
        "warnings": [],
        "prediction": None,
    }
    provider = SequenceProvider([output, output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=1,
    )

    result = asyncio.run(service.generate(jota_request_payload()))

    assert "赛前因车祸去世" not in result.report.sections[0].body
    assert "2025年7月3日" in result.report.sections[0].body


def test_service_retries_unsupported_direct_quote() -> None:
    invalid = report_output()
    invalid["sections"][0]["body"] = "主教练表示：“这是一句证据里没有的话。”"
    valid = report_output()
    valid["sections"][0]["body"] = "报道只确认了测试上下文，没有可直接引用的原话。"
    provider = SequenceProvider([invalid, valid])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request_payload()))

    assert provider.calls == 2
    assert "证据里没有的话" not in result.report.sections[0].body


def test_service_filters_noisy_reader_warnings() -> None:
    output = report_output()
    output["warnings"] = [
        "关键画面需人工补充：Example goal。",
        "资料覆盖偏薄，发布前建议人工补充。",
        "已启用稳定合稿，内容精简。",
        "高思考总编辑请求曾遇到模型连接异常。",
        "这条传闻尚未确认。",
    ]
    provider = SequenceProvider([output, output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request_payload()))

    assert "这条传闻尚未确认。" in result.report.warnings
    assert not any("关键画面需人工补充" in item for item in result.report.warnings)
    assert not any("资料覆盖偏薄" in item for item in result.report.warnings)
    assert not any("稳定合稿" in item for item in result.report.warnings)


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


def test_service_injects_evidence_backed_goal_timeline() -> None:
    request = request_payload()
    request.evidence[0].title = "Ramos sends Portugal into last 16"
    request.evidence[
        0
    ].summary = "89th-minute goal by Goncalo Ramos gave Portugal a 2-1 win."
    provider = SequenceProvider([report_output()])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request))

    assert result.report.enrichment.match_timeline
    event = result.report.enrichment.match_timeline[0]
    assert event.minute == "89"
    assert event.player == "Goncalo Ramos"
    assert event.score_after == "2-1"


def test_service_injects_multiple_match_events_and_player_card() -> None:
    request = ReportRequest.model_validate(
        {
            "report_type": "world_cup_daily",
            "subject": "Fulham Burnley report",
            "report_date": "2026-07-03",
            "data_cutoff": "2026-07-03T08:00:00Z",
            "evidence": [
                {
                    "id": "ev-match",
                    "title": "Fulham beat Burnley 3-1",
                    "url": "https://example.com/fulham-burnley",
                    "published_at": "2026-07-03T07:00:00Z",
                    "source_name": "Approved publisher",
                    "summary": (
                        "Flemming scored in the 60th minute. Josh King made it "
                        "1-1 in the 67th minute. Harry Wilson made it 2-1 in "
                        "the 72nd minute. Raul Jimenez made it 3-1 in the "
                        "90+5th minute. Laurent was sent off in the 90+5th minute."
                    ),
                }
            ],
        }
    )
    output = {
        "title": "\u5bcc\u52d2\u59c6\u6218\u62a5",
        "executive_summary": (
            "\u5bcc\u52d2\u59c6\u7684\u6bd4\u8d5b\u6210\u4e3a"
            "\u4eca\u65e5\u4e3b\u7ebf\u3002"
        ),
        "sections": [
            {
                "heading": "\u5bcc\u52d2\u59c6\u51fb\u8d25\u4f2f\u6069\u5229",
                "body": (
                    "\u5bcc\u52d2\u59c6\u5728\u4e00\u573a\u5173\u952e"
                    "\u6218\u4e2d\u51fb\u8d25\u4f2f\u6069\u5229\u3002"
                ),
                "evidence_ids": ["ev-match"],
            }
        ],
        "warnings": [],
        "prediction": None,
    }
    provider = SequenceProvider([output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=1,
    )

    result = asyncio.run(service.generate(request))

    events = result.report.enrichment.match_timeline
    minutes = {event.minute for event in events}
    players = {event.player for event in events}
    assert {"60", "67", "72", "90+5"}.issubset(minutes)
    assert "Harry Wilson" in players
    assert any(
        event.event_type == "card" and event.player == "Laurent" for event in events
    )
    assert any(
        spotlight.name == "Harry Wilson"
        for spotlight in result.report.enrichment.player_spotlights
    )


def test_service_repairs_scoreline_against_cited_evidence() -> None:
    request = ReportRequest.model_validate(
        {
            "report_type": "world_cup_daily",
            "subject": "Portugal Croatia",
            "report_date": "2026-07-03",
            "data_cutoff": "2026-07-03T08:00:00Z",
            "evidence": [
                {
                    "id": "ev-match",
                    "title": "Portugal through to last 16",
                    "url": "https://www.bbc.co.uk/sport/football/example",
                    "published_at": "2026-07-03T07:00:00Z",
                    "source_name": "BBC Sport",
                    "summary": "Portugal beat Croatia 2-1 after a late Ramos goal.",
                }
            ],
        }
    )
    output = {
        "title": "葡萄牙晋级",
        "executive_summary": "葡萄牙3-2击败克罗地亚晋级。",
        "sections": [
            {
                "heading": "葡萄牙晋级",
                "body": "葡萄牙最终3-2击败克罗地亚。",
                "evidence_ids": ["ev-match"],
            }
        ],
        "warnings": [],
        "prediction": None,
    }
    provider = SequenceProvider([output, output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=1,
    )

    result = asyncio.run(service.generate(request))

    assert "2-1" in result.report.executive_summary
    assert "3-2" not in result.report.executive_summary
    assert "2-1" in result.report.sections[0].body
    assert "3-2" not in result.report.sections[0].body


def test_service_repairs_scoreline_by_team_sentence() -> None:
    request = ReportRequest.model_validate(
        {
            "report_type": "world_cup_daily",
            "subject": "World Cup knockouts",
            "report_date": "2026-07-03",
            "data_cutoff": "2026-07-03T08:00:00Z",
            "evidence": [
                {
                    "id": "ev-portugal",
                    "title": "Portugal through to last 16",
                    "url": "https://www.bbc.co.uk/sport/football/portugal",
                    "published_at": "2026-07-03T07:00:00Z",
                    "source_name": "BBC Sport",
                    "summary": "Portugal beat Croatia 2-1 after a late Ramos goal.",
                },
                {
                    "id": "ev-swiss",
                    "title": "Switzerland beat Algeria",
                    "url": "https://www.theguardian.com/football/switzerland",
                    "published_at": "2026-07-03T07:00:00Z",
                    "source_name": "The Guardian Football",
                    "summary": "Switzerland beat Algeria 2-0 in the last 32.",
                },
            ],
        }
    )
    output = {
        "title": "世界杯淘汰赛",
        "executive_summary": "葡萄牙2-0击败克罗地亚，瑞士2-0击败阿尔及利亚。",
        "sections": [
            {
                "heading": "赛场速递",
                "body": "葡萄牙2-0击败克罗地亚。瑞士2-0击败阿尔及利亚。",
                "evidence_ids": ["ev-portugal", "ev-swiss"],
            }
        ],
        "warnings": [],
        "prediction": None,
    }
    provider = SequenceProvider([output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=1,
    )

    result = asyncio.run(service.generate(request))

    assert "葡萄牙2-1击败克罗地亚" in result.report.sections[0].body
    assert "瑞士2-0击败阿尔及利亚" in result.report.sections[0].body
    assert "葡萄牙2-1击败克罗地亚" in result.report.executive_summary


def test_service_repairs_scoreline_orientation_for_mentioned_team_order() -> None:
    request = ReportRequest.model_validate(
        {
            "report_type": "world_cup_daily",
            "subject": "France Paraguay",
            "report_date": "2026-07-05",
            "data_cutoff": "2026-07-05T08:00:00Z",
            "evidence": [
                {
                    "id": "ev-france",
                    "title": "Paraguay 0-1 France: World Cup last 16",
                    "url": "https://www.theguardian.com/football/france-paraguay",
                    "published_at": "2026-07-05T07:00:00Z",
                    "source_name": "The Guardian Football",
                    "summary": "France beat Paraguay 1-0 after a Mbappe penalty.",
                }
            ],
        }
    )
    output = {
        "title": "法国 0-1 巴拉圭晋级",
        "executive_summary": "法国以0-1战胜巴拉圭，这是淘汰赛关键结果。",
        "sections": [
            {
                "heading": "法国 0-1 巴拉圭",
                "body": "法国以0-1战胜巴拉圭，姆巴佩点球制胜。",
                "evidence_ids": ["ev-france"],
                "category": "match",
            }
        ],
        "warnings": [],
        "prediction": None,
    }
    provider = SequenceProvider([output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=1,
    )

    result = asyncio.run(service.generate(request))

    assert result.report.title == "法国 1-0 巴拉圭晋级"
    assert "法国以1-0战胜巴拉圭" in result.report.sections[0].body
    assert result.report.sections[0].heading == "法国 1-0 巴拉圭"
    assert "法国以1-0战胜巴拉圭" in result.report.executive_summary


def test_service_repairs_structured_scoreline_without_reading_date_as_score() -> None:
    request = ReportRequest.model_validate(
        {
            "report_type": "world_cup_daily",
            "subject": "Spain Austria",
            "report_date": "2026-07-03",
            "data_cutoff": "2026-07-03T16:00:00Z",
            "evidence": [
                {
                    "id": "football-data-spain",
                    "title": (
                        "结构化赛果｜Spain 3-0 Austria（北京时间 2026-07-03 03:00）"
                    ),
                    "url": "https://www.football-data.org/",
                    "published_at": "2026-07-03T16:00:00Z",
                    "source_name": "football-data.org",
                    "source_id": "football-data-org",
                    "trust_tier": "S1_structured_provider",
                    "evidence_kind": "structured",
                    "verification_status": "corroborated",
                    "summary": (
                        "football-data.org 结构化赛果：北京时间 "
                        "2026-07-03 03:00，LAST_32，Spain 3-0 Austria，"
                        "状态 FINISHED，结果方向按对阵顺序记录为 Spain vs Austria。"
                    ),
                }
            ],
        }
    )
    output = {
        "title": "西班牙 3-7 奥地利",
        "executive_summary": "西班牙3-7战胜奥地利。",
        "sections": [
            {
                "heading": "西班牙 3-7 奥地利",
                "body": "西班牙 3-7 奥地利，顺利晋级。",
                "evidence_ids": ["football-data-spain"],
                "category": "match",
            }
        ],
        "warnings": [],
        "prediction": None,
    }
    report = GeneratedReport.model_validate(output)

    _repair_report_scorelines(report, request)

    assert report.title == "西班牙 3-0 奥地利"
    assert "西班牙3-0战胜奥地利" in report.executive_summary
    assert report.sections[0].heading == "西班牙 3-0 奥地利"
    assert "3-7" not in report.sections[0].body


def test_visible_score_regex_ignores_beijing_day_window() -> None:
    text = "北京时间 2026-07-03 00:00-24:00；Spain 3-0 Austria"

    matches = [match.group(0) for match in VISIBLE_SCORE_RE.finditer(text)]

    assert matches == ["3-0"]


def test_assign_categories_requires_transfer_evidence_for_transfer_upgrade() -> None:
    request = ReportRequest.model_validate(
        {
            "report_type": "world_cup_daily",
            "subject": "World Cup daily",
            "report_date": "2026-07-03",
            "data_cutoff": "2026-07-03T16:00:00Z",
            "evidence": [
                {
                    "id": "match-player",
                    "title": "Manzambi stars in Switzerland win",
                    "url": "https://www.theguardian.com/football/manzambi",
                    "published_at": "2026-07-03T12:00:00Z",
                    "source_name": "The Guardian Football",
                    "summary": "Manzambi impressed during Switzerland's match.",
                    "source_id": "guardian-football",
                }
            ],
        }
    )
    report = GeneratedReport.model_validate(
        {
            "title": "今日球脉",
            "executive_summary": "摘要。",
            "sections": [
                {
                    "heading": "纽卡斯尔关注弗赖堡前锋曼赞比",
                    "body": "这条内容提到转会兴趣，但引用证据本身不是转会报道。",
                    "evidence_ids": ["match-player"],
                    "category": "transfer",
                }
            ],
            "warnings": [],
            "prediction": None,
        }
    )

    _assign_section_categories(report, request)

    assert report.sections[0].category == "context"


def test_service_suppresses_prefetched_media_assets() -> None:
    request = request_payload().model_copy(
        update={
            "prefetched_media_assets": [
                MediaAsset(
                    asset_type="image",
                    title="Example Player",
                    url="https://commons.wikimedia.org/wiki/File:Example_Player.jpg",
                    thumbnail_url="https://upload.wikimedia.org/example.jpg",
                    provider="Wikimedia Commons",
                    license="CC BY-SA 4.0",
                    attribution="Photographer",
                    rights_status="review_required",
                    relevance_status="metadata_match",
                    evidence_ids=["ev-1"],
                )
            ]
        }
    )
    provider = SequenceProvider([report_output()])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request))

    assert result.report.enrichment.media_assets == []


def test_service_suppresses_player_image_even_when_target_appears_in_copy() -> None:
    request = ReportRequest.model_validate(
        {
            "report_type": "transfer_daily",
            "subject": "Tottenham transfer",
            "report_date": "2026-07-03",
            "data_cutoff": "2026-07-03T08:00:00Z",
            "evidence": [
                {
                    "id": "ev-transfer",
                    "title": "Tottenham win race to sign Mateus Fernandes",
                    "url": "https://www.theguardian.com/football/spurs-fernandes",
                    "published_at": "2026-07-03T07:00:00Z",
                    "source_name": "The Guardian Football",
                    "summary": "Mateus Fernandes is the lead transfer story.",
                }
            ],
            "prefetched_media_assets": [
                {
                    "asset_type": "image",
                    "title": "Mateus Fernandes",
                    "url": "https://commons.wikimedia.org/wiki/File:Mateus.jpg",
                    "thumbnail_url": "https://upload.wikimedia.org/mateus.jpg",
                    "provider": "Wikimedia Commons",
                    "license": "CC BY-SA 4.0",
                    "attribution": "Photographer",
                    "rights_status": "review_required",
                    "relevance_status": "metadata_match",
                    "target": "Mateus Fernandes",
                    "placement": "spotlight",
                    "evidence_ids": ["unused-ev"],
                }
            ],
        }
    )
    output = {
        "title": "费尔南德斯加盟热刺",
        "executive_summary": (
            "马特乌斯·费尔南德斯（Mateus Fernandes）是热刺转会栏目的主线。"
        ),
        "sections": [
            {
                "heading": "费尔南德斯加盟热刺",
                "body": (
                    "Mateus Fernandes（马特乌斯·费尔南德斯）即将加盟热刺，"
                    "是本栏最需要配球员图的转会。"
                ),
                "evidence_ids": ["ev-transfer"],
            }
        ],
        "warnings": [],
        "prediction": None,
    }
    provider = SequenceProvider([output, output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request))

    assert result.report.enrichment.media_assets == []


def test_service_drops_implausible_target_image() -> None:
    request = request_payload().model_copy(
        update={
            "prefetched_media_assets": [
                MediaAsset(
                    asset_type="image",
                    title="JamalDuff.jpg",
                    url="https://commons.wikimedia.org/wiki/File:JamalDuff.jpg",
                    thumbnail_url="https://upload.wikimedia.org/jamal.jpg",
                    provider="Wikimedia Commons",
                    license="CC BY-SA 4.0",
                    attribution="Photographer",
                    rights_status="review_required",
                    relevance_status="metadata_match",
                    target="Jamal Johnson",
                    placement="spotlight",
                    evidence_ids=["ev-1"],
                )
            ]
        }
    )
    provider = SequenceProvider([report_output()])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request))

    assert result.report.enrichment.media_assets == []


def test_service_suppresses_timeline_media_without_timeline_event() -> None:
    request = request_payload().model_copy(
        update={
            "prefetched_media_assets": [
                MediaAsset(
                    asset_type="video",
                    title="Example match goal",
                    url="https://www.youtube.com/watch?v=goal",
                    embed_url="https://www.youtube.com/embed/goal",
                    thumbnail_url="https://i.ytimg.com/vi/goal/hqdefault.jpg",
                    provider="YouTube official channel",
                    license="YouTube embeddable link",
                    attribution="FIFA",
                    rights_status="approved",
                    relevance_status="metadata_match",
                    placement="timeline",
                    evidence_ids=["ev-1"],
                )
            ]
        }
    )
    provider = SequenceProvider([report_output()])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request))

    assert result.report.enrichment.media_assets == []


def test_service_drops_prefetched_media_not_cited_by_final_report() -> None:
    request = request_payload().model_copy(
        update={
            "prefetched_media_assets": [
                MediaAsset(
                    asset_type="video",
                    title="Unrelated match",
                    url="https://www.youtube.com/watch?v=unrelated",
                    embed_url="https://www.youtube.com/embed/unrelated",
                    thumbnail_url="https://i.ytimg.com/vi/unrelated/hqdefault.jpg",
                    provider="YouTube official channel",
                    license="YouTube embeddable link",
                    attribution="FIFA",
                    rights_status="approved",
                    relevance_status="metadata_match",
                    placement="report_cover",
                    evidence_ids=["unused-ev"],
                )
            ]
        }
    )
    provider = SequenceProvider([report_output()])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request))

    assert result.report.enrichment.media_assets == []


def test_service_drops_prefetched_cover_outside_primary_section() -> None:
    base = request_payload()
    request = ReportRequest.model_validate(
        {
            **base.model_dump(mode="json"),
            "evidence": [
                *[item.model_dump(mode="json") for item in base.evidence],
                {
                    "id": "ev-2",
                    "title": "Other match",
                    "url": "https://example.com/other",
                    "published_at": "2026-06-28T07:00:00Z",
                    "source_name": "Official source",
                    "summary": "Other match context.",
                },
            ],
            "prefetched_media_assets": [
                {
                    "asset_type": "video",
                    "title": "Other match highlights",
                    "url": "https://www.youtube.com/watch?v=other",
                    "embed_url": "https://www.youtube.com/embed/other",
                    "thumbnail_url": "https://i.ytimg.com/vi/other/hqdefault.jpg",
                    "provider": "YouTube official channel",
                    "license": "YouTube embeddable link",
                    "attribution": "FIFA",
                    "rights_status": "approved",
                    "relevance_status": "metadata_match",
                    "placement": "report_cover",
                    "evidence_ids": ["ev-2"],
                }
            ],
        }
    )
    output = report_output()
    output["sections"].append(
        {
            "heading": "Other section",
            "body": "Other cited item.",
            "evidence_ids": ["ev-2"],
        }
    )
    provider = SequenceProvider([output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request))

    assert result.report.enrichment.media_assets == []


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


def test_service_discards_malformed_optional_enrichment_without_retry() -> None:
    output = report_output()
    output["enrichment"] = {
        "player_spotlights": [
            {
                "name": 123,
                "narrative": "这张人物卡结构不完整，应该被移除。",
                "evidence_ids": ["ev-1"],
            }
        ],
        "match_timeline": [
            {
                "minute": {"text": "72"},
                "event_type": "goal",
                "description": "Example Player 打入一球。",
                "evidence_ids": ["ev-1"],
            },
            {
                "minute": "72",
                "event_type": "invented",
                "description": "Example Player 打入一球。",
                "evidence_ids": ["ev-1"],
            },
        ],
        "media_assets": [
            {
                "title": "模型不应注入媒体",
                "url": {"bad": "shape"},
            }
        ],
    }
    provider = SequenceProvider([output])
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        max_output_tokens=1000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(request_payload()))

    assert provider.calls == 1
    assert result.report.enrichment.player_spotlights == []
    assert result.report.enrichment.match_timeline == []
    assert result.report.enrichment.media_assets == []


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
    assert "numeric_claim_ledger" in final_text
    assert "daily_briefing_playbook" in final_text
    assert "关键信息整合商" in final_text
    assert "【核心】" in final_text
    assert "180-420" not in final_text
    assert len(final_text) < 22_000
    assert provider.final_request.thinking_enabled is False
    assert provider.final_request.max_output_tokens == 6000


def test_daily_digest_retries_until_leader_columns_are_covered() -> None:
    provider = CoverageRetryDailyProvider()
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        flash_model="deepseek-v4-flash",
        max_output_tokens=6000,
        max_attempts=3,
    )

    result = asyncio.run(service.generate(daily_digest_request_with_editorial_plan()))

    assert provider.final_calls == 2
    assert any(
        "热刺" in section.heading and "ev-transfer" in section.evidence_ids
        for section in result.report.sections
    )


def test_daily_digest_deterministic_finalizer_when_coverage_still_missing() -> None:
    provider = CoverageNeverDailyProvider()
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        flash_model="deepseek-v4-flash",
        max_output_tokens=6000,
        max_attempts=2,
    )

    result = asyncio.run(service.generate(daily_digest_request_with_editorial_plan()))

    assert provider.final_calls == 2
    assert result.provider == "harness"
    assert result.model == "deterministic-daily-finalizer"
    assert any(
        "ev-transfer" in section.evidence_ids for section in result.report.sections
    )


def test_daily_digest_claim_failure_recovers_with_conservative_report() -> None:
    provider = NumericClaimFailureDailyProvider()
    events: list[tuple[str, dict | None]] = []
    service = ReportService(
        provider=provider,
        model="deepseek-v4-pro",
        flash_model="deepseek-v4-flash",
        max_output_tokens=6000,
        max_attempts=2,
    )

    result = asyncio.run(
        service.generate(
            daily_digest_request_with_cape_verde_match(),
            progress_callback=lambda phase, _progress, payload=None: events.append(
                (phase, payload)
            ),
        )
    )

    visible = " ".join(section.body for section in result.report.sections)
    assert provider.final_calls == 2
    assert result.provider == "harness"
    assert result.model == "deterministic-daily-finalizer"
    assert "16" not in visible
    assert "3-2" in visible
    assert any(phase == "claim_repair" for phase, _payload in events)
    assert any(
        phase == "desk_drafts_ready"
        and isinstance(payload, dict)
        and payload.get("checkpoint", {}).get("name") == "desk_drafts_ready"
        for phase, payload in events
    )
    assert any(
        phase == "editor_synthesis"
        and isinstance(payload, dict)
        and payload.get("status") == "validation_failed"
        and payload.get("checkpoint", {}).get("name") == "editor_synthesis_attempt_1"
        for phase, payload in events
    )


def test_completed_headline_survives_mixed_match_summary_terms() -> None:
    request = daily_digest_request_with_mixed_match_column()

    assert is_completed_match_evidence(request.evidence[1])


def test_deterministic_daily_finalizer_reroutes_upcoming_match_fallback() -> None:
    request = daily_digest_request_with_mixed_match_column()
    bad_draft = DeskDraft.model_validate(
        {
            "desk": "mixed-match-report",
            "heading": "英格兰墨西哥城迎苦战",
            "summary": "英格兰和墨西哥的赛前安排成为今日焦点。",
            "sections": [
                {
                    "heading": "英格兰墨西哥城迎苦战",
                    "body": (
                        "今日世界杯赛场，英格兰客场挑战墨西哥，比赛已经展开较量。"
                    ),
                    "evidence_ids": ["england-mexico-preview"],
                    "category": "match",
                }
            ],
            "warnings": [],
        }
    )
    events: list[tuple[str, dict | None]] = []
    service = ReportService(
        provider=SequenceProvider([]),
        model="deepseek-v4-pro",
        max_output_tokens=6000,
        max_attempts=2,
    )

    result = asyncio.run(
        service._deterministic_daily_response(
            request,
            [bad_draft],
            [],
            attempts=2,
            progress_callback=lambda phase, _progress, payload=None: events.append(
                (phase, payload)
            ),
            recovery_reasons=["coverage failed"],
        )
    )

    visible = " ".join(section.body for section in result.report.sections)
    assert result.provider == "harness"
    assert result.model == "deterministic-daily-finalizer"
    assert "比赛已经展开较量" not in visible
    assert any(
        section.category == "match" and "france-paraguay-report" in section.evidence_ids
        for section in result.report.sections
    )
    assert not any(
        section.category == "match" and "england-mexico-preview" in section.evidence_ids
        for section in result.report.sections
    )
    assert any(
        phase == "deterministic_finalizer"
        and isinstance(payload, dict)
        and payload.get("status") == "desk_sections_validation_failed"
        for phase, payload in events
    )


def test_deterministic_daily_finalizer_keeps_offfield_columns_visible() -> None:
    request = daily_digest_request_with_context_and_offfield_sources()
    desk_drafts = [
        DeskDraft.model_validate(
            {
                "desk": "bad-transfer",
                "heading": "阿美赞助争议",
                "summary": "阿美赞助与社区污染争议成为场外焦点。",
                "sections": [
                    {
                        "heading": "阿美赞助争议",
                        "body": "阿美作为世界杯赞助商，在休斯顿附近面对社区污染批评。",
                        "evidence_ids": ["aramco-worldcup"],
                        "category": "context",
                    }
                ],
                "warnings": [],
            }
        ),
        DeskDraft.model_validate(
            {
                "desk": "off-field",
                "heading": "墨西哥退还手表",
                "summary": "墨西哥队因FIFA规则退还第三方礼物。",
                "sections": [
                    {
                        "heading": "墨西哥退还手表",
                        "body": "墨西哥队因FIFA规则退还YouTuber赠送的豪华手表。",
                        "evidence_ids": ["mexico-watches"],
                        "category": "context",
                    }
                ],
                "warnings": [],
            }
        ),
        DeskDraft.model_validate(
            {
                "desk": "context",
                "heading": "巴西足球文化",
                "summary": "罗德里戈谈到巴西社会对足球的热情。",
                "sections": [
                    {
                        "heading": "巴西足球文化",
                        "body": "罗德里戈谈到巴西社会对国家队和足球文化的深厚热情。",
                        "evidence_ids": ["rodrygo-brazil"],
                        "category": "context",
                    }
                ],
                "warnings": [],
            }
        ),
    ]
    events: list[tuple[str, dict | None]] = []
    service = ReportService(
        provider=SequenceProvider([]),
        model="deepseek-v4-pro",
        max_output_tokens=6000,
        max_attempts=2,
    )

    result = asyncio.run(
        service._deterministic_daily_response(
            request,
            desk_drafts,
            [],
            attempts=2,
            progress_callback=lambda phase, _progress, payload=None: events.append(
                (phase, payload)
            ),
            recovery_reasons=["match column has no completed evidence"],
        )
    )

    categories = [section.category for section in result.report.sections]
    assert "off_field" in categories
    assert "context" in categories
    assert "transfer" not in categories
    assert not any(
        section.category != "match" and section.heading.startswith("赛场战报")
        for section in result.report.sections
    )
    accepted_payloads = [
        payload
        for phase, payload in events
        if phase == "deterministic_finalizer"
        and isinstance(payload, dict)
        and payload.get("status") == "accepted"
    ]
    assert accepted_payloads
    assert accepted_payloads[-1]["category_counts"]["off_field"] >= 2


def test_deterministic_daily_finalizer_hides_internal_evidence_markup() -> None:
    request = ReportRequest.model_validate(
        {
            "report_type": "daily_football_digest",
            "subject": "今日球脉｜世界杯",
            "report_date": "2026-07-04",
            "data_cutoff": "2026-07-04T08:00:00Z",
            "evidence": [
                {
                    "id": "football-data-aus-egypt",
                    "title": (
                        "结构化赛果｜Australia 3-5 Egypt（点球大战后；"
                        "常规时间 1-1；点球 2-4）（北京时间 2026-07-04 02:00）"
                    ),
                    "url": "https://www.football-data.org/",
                    "published_at": "2026-07-04T08:00:00Z",
                    "source_name": "football-data.org",
                    "source_id": "football-data-org",
                    "trust_tier": "S1_structured_provider",
                    "evidence_kind": "structured",
                    "verification_status": "corroborated",
                    "summary": (
                        "精简提炼：同簇标题：结构化赛果｜Australia 3-5 Egypt。"
                        "football facts 结构化赛事实据：北京时间 2026-07-04 全日窗口；"
                        "北京时间 2026-07-04 02:00，阶段 32强淘汰赛（LAST_32），"
                        "Australia 3-5 Egypt（点球大战后；常规时间 1-1；点球 2-4），"
                        "状态 FINISHED，结果方向按对阵顺序记录为 Australia vs Egypt；"
                        "结论：Egypt。结构化事件：当前无进球者/分钟/红黄牌/换人事件；"
                        "不得编写未入证据的时间线。"
                    ),
                }
            ],
            "editorial_plan": [
                {
                    "column_id": "match_report",
                    "title": "赛场战报",
                    "category": "match",
                    "specialist_group": "match_report",
                    "priority": 1,
                    "evidence_ids": ["football-data-aus-egypt"],
                }
            ],
        }
    )
    service = ReportService(
        provider=SequenceProvider([]),
        model="deepseek-v4-pro",
        max_output_tokens=6000,
        max_attempts=2,
    )

    result = asyncio.run(
        service._deterministic_daily_response(
            request,
            [],
            [],
            attempts=2,
            recovery_reasons=["forced fallback"],
        )
    )

    visible = " ".join(
        [section.heading + " " + section.body for section in result.report.sections]
    )
    assert "同簇标题" not in visible
    assert "摘要信息" not in visible
    assert "结构化赛果｜" not in visible
    assert "Australia 3-5 Egypt" in visible
    assert "Australia 4-2 Egypt" not in visible
    assert "【核心】" in visible
    assert "【边界】" in visible


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
    assert result.attempts >= 4
    assert [item.purpose for item in provider.final_attempts] == [
        "daily_football_digest",
        "daily_football_digest:stable_final",
    ]
    stable_request = provider.final_attempts[-1]
    assert stable_request.thinking_enabled is False
    assert stable_request.max_output_tokens == 6000
    assert not any("稳定合稿模式" in item for item in result.report.warnings)


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
    assert result.attempts >= 5
    assert provider.final_attempts == [
        "daily_football_digest",
        "daily_football_digest:stable_final",
    ]
    assert "保守合稿版" in result.report.title
    assert len(result.report.sections) >= 1
    assert not any("保守版本" in item for item in result.report.warnings)
