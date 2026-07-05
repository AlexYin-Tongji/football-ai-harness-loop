from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field, ValidationError

from services.mcp_servers.common import get_json
from services.report_api.article_reader import ArticleExcerpt, read_article_excerpt
from services.report_api.critical_entities import (
    collect_critical_entity_evidence,
    expand_subject_with_critical_aliases,
    load_critical_entities,
    matching_critical_entities,
)
from services.report_api.domain import (
    ConsumerReportRequest,
    EditorialColumnPlan,
    Evidence,
    MediaAsset,
    ReportType,
)
from services.report_api.evidence import (
    EvidenceCollectionError,
    collect_bbc_evidence,
    collect_gdelt_evidence,
    collect_guardian_evidence,
    collect_guardian_search_evidence,
    collect_newsapi_evidence,
    finish_evidence_selection,
)
from services.report_api.evidence_state import (
    completed_match_items,
    evidence_is_match_like,
    is_completed_match_evidence,
    is_upcoming_match_evidence,
)
from services.report_api.media import (
    cache_media_thumbnail,
    search_commons_player_image,
    search_official_youtube_video,
)
from services.report_api.model_control import evidence_index, truncate_text
from services.report_api.providers.base import LLMProvider, LLMProviderError, LLMRequest
from services.report_api.structured_match_data import (
    collect_daily_structured_match_evidence,
)

INITIAL_LEADER_SEED_MAX_ITEMS = 8
COLUMN_TEAM_MAX_ITERATIONS = 4

ResearchSource = Literal["rss", "gdelt", "newsapi"]
LayerName = Literal[
    "url_collection",
    "evidence_refinement",
    "enhancement",
    "leader_review",
    "column_team_loop",
    "writing_handoff",
]
LayerStatus = Literal["completed", "degraded", "skipped"]
EnhancementKind = Literal[
    "player_profile",
    "club_context",
    "licensed_image",
    "official_video",
    "match_context",
    "gif",
]
MATCH_VISUAL_RE = re.compile(
    r"\b(goal|scored|scores|winner|equaliser|equalizer|penalty|var|"
    r"highlights?|match report|beat|beats|defeat|defeats)\b",
    re.I,
)
TEAM_NAME_ALIASES = {
    "Portugal": ("Portugal", "葡萄牙"),
    "Croatia": ("Croatia", "克罗地亚"),
    "Spain": ("Spain", "西班牙"),
    "Austria": ("Austria", "奥地利"),
    "Switzerland": ("Switzerland", "瑞士"),
    "Algeria": ("Algeria", "阿尔及利亚"),
    "England": ("England", "英格兰"),
    "Mexico": ("Mexico", "墨西哥"),
    "Australia": ("Australia", "澳大利亚"),
    "Egypt": ("Egypt", "埃及"),
}
PLAYER_IMAGE_TARGETS = {
    "Cristiano Ronaldo": ("Cristiano Ronaldo", "Ronaldo", "C罗", "罗纳尔多"),
    "Goncalo Ramos": ("Goncalo Ramos", "Ramos", "贡萨洛·拉莫斯", "拉莫斯"),
    "Mikel Oyarzabal": ("Mikel Oyarzabal", "Oyarzabal", "奥亚萨瓦尔"),
    "Lionel Messi": ("Lionel Messi", "Messi", "梅西"),
    "Mohamed Salah": ("Mohamed Salah", "Salah", "萨拉赫"),
    "Riyad Mahrez": ("Riyad Mahrez", "Mahrez", "马赫雷斯"),
    "Luka Modric": ("Luka Modric", "Modric", "莫德里奇", "魔笛"),
}
RESEARCH_FOCUS_ALIASES = {
    "热刺": ("Spurs", "Tottenham", "Tottenham Hotspur"),
    "托特纳姆": ("Spurs", "Tottenham", "Tottenham Hotspur"),
    "曼联": ("Manchester United", "Man Utd"),
    "阿森纳": ("Arsenal",),
    "纽卡": ("Newcastle United", "Newcastle"),
    "西汉姆": ("West Ham United", "West Ham"),
    "巴萨": ("Barcelona", "Barca"),
    "皇马": ("Real Madrid",),
}


class LayerLoopSummary(BaseModel):
    name: LayerName
    label: str
    status: LayerStatus
    model_rounds: int = Field(default=0, ge=0, le=20)
    tool_rounds: int = Field(default=0, ge=0, le=20)
    input_count: int = Field(default=0, ge=0)
    output_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=8)
    checkpoints: list[str] = Field(default_factory=list, max_length=8)


class ResearchQuery(BaseModel):
    query: str = Field(min_length=3, max_length=180)
    purpose: Literal[
        "match_news",
        "transfer_market",
        "prediction_context",
        "external_prediction",
    ]
    sources: list[ResearchSource] = Field(default_factory=list, max_length=3)


class ResearchPlan(BaseModel):
    queries: list[ResearchQuery] = Field(min_length=1, max_length=8)
    min_items: int = Field(default=1, ge=1, le=4)
    allow_discovery_only: bool = True


class UrlCollectionResult(BaseModel):
    plan: ResearchPlan
    candidates: list[Evidence]
    warnings: list[str] = Field(default_factory=list, max_length=12)
    source_attempts: dict[str, str] = Field(default_factory=dict)
    loop: LayerLoopSummary


class RefinedEvidenceItem(BaseModel):
    source_evidence_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=240)
    original_url: str | None = Field(default=None, max_length=1000)
    concise_summary: str = Field(min_length=1, max_length=1200)
    key_points: list[str] = Field(default_factory=list, max_length=5)


class EvidenceRefinementResult(BaseModel):
    items: list[RefinedEvidenceItem] = Field(default_factory=list, max_length=40)
    warnings: list[str] = Field(default_factory=list, max_length=8)


class RefinementLayerResult(BaseModel):
    evidence: list[Evidence]
    warnings: list[str] = Field(default_factory=list, max_length=12)
    loop: LayerLoopSummary


class EnhancementNeed(BaseModel):
    kind: EnhancementKind
    target: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=6)
    priority: int = Field(default=3, ge=1, le=5)


class EnhancementPlan(BaseModel):
    needs: list[EnhancementNeed] = Field(default_factory=list, max_length=8)
    warnings: list[str] = Field(default_factory=list, max_length=8)


class EnhancementLayerResult(BaseModel):
    additional_evidence: list[Evidence] = Field(default_factory=list, max_length=12)
    media_assets: list[MediaAsset] = Field(default_factory=list, max_length=8)
    warnings: list[str] = Field(default_factory=list, max_length=12)
    loop: LayerLoopSummary


class LeaderColumnPlanResult(BaseModel):
    columns: list[EditorialColumnPlan] = Field(default_factory=list, max_length=8)
    warnings: list[str] = Field(default_factory=list, max_length=8)


class LeaderReviewResult(BaseModel):
    block_generation: bool = False
    editorial_plan: list[EditorialColumnPlan] = Field(
        default_factory=list, max_length=8
    )
    loop: LayerLoopSummary


@dataclass(frozen=True)
class ResearchBundle:
    evidence: list[Evidence]
    warnings: list[str]
    plan: ResearchPlan
    source_attempts: dict[str, str]
    layer_runs: list[LayerLoopSummary] = field(default_factory=list)
    media_assets: list[MediaAsset] = field(default_factory=list)
    editorial_plan: list[EditorialColumnPlan] = field(default_factory=list)
    tool_rounds_used: int = 0


@dataclass(frozen=True)
class ColumnTeamLoopResult:
    column: EditorialColumnPlan
    evidence: list[Evidence]
    media_assets: list[MediaAsset]
    warnings: list[str]
    source_attempts: dict[str, str]
    loop: LayerLoopSummary


def _english_terms(value: str) -> list[str]:
    return [
        item
        for item in re.findall(r"[A-Za-z][A-Za-z0-9' -]{2,}", value)
        if item.casefold()
        not in {
            "report",
            "daily",
            "today",
            "football",
            "world cup",
            "prediction",
        }
    ]


def _load_agent_skill_text(skill_name: str, fallback: str) -> str:
    root = Path(__file__).resolve().parents[2]
    path = root / "agent_skills" / skill_name / "SKILL.md"
    if not path.exists():
        return fallback
    return path.read_text(encoding="utf-8")


def _model_failure_detail(exc: Exception) -> str:
    if isinstance(exc, LLMProviderError):
        return f"{exc.kind}"
    if isinstance(exc, ValidationError):
        return "schema_validation"
    return exc.__class__.__name__


def _known_team_names(text: str) -> list[str]:
    found: list[str] = []
    folded = text.casefold()
    for team, aliases in TEAM_NAME_ALIASES.items():
        for alias in aliases:
            if re.search(r"[\u4e00-\u9fff]", alias):
                if alias in text:
                    found.append(team)
                    break
                continue
            if re.search(rf"(?<![a-z]){re.escape(alias.casefold())}(?![a-z])", folded):
                found.append(team)
                break
    return list(dict.fromkeys(found))


def _known_player_image_targets(text: str) -> list[str]:
    found: list[str] = []
    folded = text.casefold()
    for player, aliases in PLAYER_IMAGE_TARGETS.items():
        for alias in aliases:
            if re.search(r"[\u4e00-\u9fff]", alias):
                if alias in text:
                    found.append(player)
                    break
                continue
            if re.search(
                rf"(?<![a-z]){re.escape(alias.casefold())}(?![a-z])", folded
            ):
                found.append(player)
                break
    return list(dict.fromkeys(found))


def _person_image_candidates(text: str, *, limit: int = 3) -> list[str]:
    blocked = {
        "World Cup",
        "FIFA World",
        "BBC Sport",
        "The Guardian",
        "Premier League",
        "Champions League",
        "Club World",
    }
    names: list[str] = []
    for name in re.findall(
        r"\b[A-Z][A-Za-zÀ-ÿ'’.-]{2,}(?:\s+[A-Z][A-Za-zÀ-ÿ'’.-]{2,}){1,2}\b",
        text,
    ):
        if name in blocked:
            continue
        if any(term in name.casefold() for term in {"world cup", "bbc sport"}):
            continue
        names.append(name)
    return list(dict.fromkeys(names))[:limit]


def _research_focus_aliases(text: str) -> list[str]:
    aliases: list[str] = []
    folded = text.casefold()
    for marker, values in RESEARCH_FOCUS_ALIASES.items():
        if marker.casefold() in folded:
            aliases.extend(values)
    return list(dict.fromkeys(aliases))


def _search_query_text(value: str) -> str:
    return truncate_text(re.sub(r"\s+", " ", value).strip(), 180)


def _with_focus_alias_queries(
    request: ConsumerReportRequest, plan: ResearchPlan
) -> ResearchPlan:
    aliases = _research_focus_aliases(" ".join([request.subject, *request.focus]))
    if not aliases:
        return plan

    alias_queries = [
        item
        for item in plan.queries
        if any(alias.casefold() in item.query.casefold() for alias in aliases)
    ]
    if alias_queries:
        other_queries = [item for item in plan.queries if item not in alias_queries]
        promoted_queries = [*alias_queries, *other_queries][:8]
        return plan.model_copy(update={"queries": promoted_queries})

    if request.report_type == ReportType.MATCH_PREDICTION:
        query = _search_query_text(
            f"{' '.join(aliases)} team news injuries lineup preview"
        )
        purpose = "prediction_context"
    elif request.report_type == ReportType.TRANSFER_DAILY:
        query = _search_query_text(
            f"{' '.join(aliases)} latest transfer news talks bid agreement today"
        )
        purpose = "transfer_market"
    else:
        query = _search_query_text(
            f"{' '.join(aliases)} latest football news transfer match today"
        )
        purpose = "transfer_market"

    focus_query = ResearchQuery(
        query=query,
        purpose=purpose,
        sources=["rss", "gdelt", "newsapi"],
    )
    return plan.model_copy(update={"queries": [focus_query, *plan.queries][:8]})


def fallback_research_plan(request: ConsumerReportRequest) -> ResearchPlan:
    focus = " ".join(request.focus)
    expanded_subject = expand_subject_with_critical_aliases(
        f"{request.subject} {focus}"
    )
    focus_aliases = _research_focus_aliases(f"{request.subject} {focus}")
    seed = " ".join([*_english_terms(expanded_subject), *focus_aliases]).strip()
    if not seed:
        seed = {
            ReportType.DAILY_FOOTBALL_DIGEST: "FIFA World Cup football transfer news",
            ReportType.WORLD_CUP_DAILY: "FIFA World Cup team news fixtures results",
            ReportType.TRANSFER_DAILY: "football transfer news talks bid agreement",
            ReportType.MATCH_PREDICTION: "World Cup match preview team news prediction",
        }[request.report_type]

    if request.report_type == ReportType.MATCH_PREDICTION:
        queries = [
            ResearchQuery(
                query=_search_query_text(f"{seed} team news injuries lineup preview"),
                purpose="prediction_context",
                sources=["rss", "gdelt", "newsapi"],
            ),
            ResearchQuery(
                query=_search_query_text(
                    f"{seed} Opta Stats Perform prediction probability"
                ),
                purpose="external_prediction",
                sources=["gdelt", "newsapi"],
            ),
            ResearchQuery(
                query=_search_query_text(
                    f"{seed} official match centre highlights team news"
                ),
                purpose="prediction_context",
                sources=["gdelt", "newsapi"],
            ),
            ResearchQuery(
                query=_search_query_text(
                    f"{request.subject} football tactical preview"
                ),
                purpose="prediction_context",
                sources=["gdelt", "newsapi"],
            ),
        ]
    elif request.report_type == ReportType.TRANSFER_DAILY:
        queries = [
            ResearchQuery(
                query=_search_query_text(
                    f"{seed} transfer talks bid agreement medical"
                ),
                purpose="transfer_market",
                sources=["rss", "gdelt", "newsapi"],
            ),
            ResearchQuery(
                query="football transfer news today bid talks agreement",
                purpose="transfer_market",
                sources=["gdelt", "newsapi"],
            ),
            ResearchQuery(
                query=_search_query_text(
                    f"{seed} BBC Guardian Sky Sports Marca Diario Sport "
                    "transfer update"
                ),
                purpose="transfer_market",
                sources=["gdelt", "newsapi"],
            ),
        ]
    else:
        queries = [
            ResearchQuery(
                query=_search_query_text(f"{seed} match news fixtures results injury"),
                purpose="match_news",
                sources=["rss", "gdelt", "newsapi"],
            ),
            ResearchQuery(
                query=_search_query_text(f"{seed} transfer news signing talks"),
                purpose="transfer_market",
                sources=["rss", "gdelt", "newsapi"],
            ),
            ResearchQuery(
                query=_search_query_text(
                    f"{seed} official club statement team news highlights"
                ),
                purpose="match_news",
                sources=["gdelt", "newsapi"],
            ),
            ResearchQuery(
                query="FIFA World Cup football news transfer team news today",
                purpose="match_news",
                sources=["gdelt", "newsapi"],
            ),
        ]
        if focus_aliases:
            queries.insert(
                1,
                ResearchQuery(
                    query=_search_query_text(
                        f"{' '.join(focus_aliases)} latest transfer news today"
                    ),
                    purpose="transfer_market",
                    sources=["rss", "gdelt", "newsapi"],
                ),
            )
    return ResearchPlan(queries=queries, min_items=1, allow_discovery_only=True)


def _dedupe_evidence(items: list[Evidence], *, max_items: int) -> list[Evidence]:
    deduped: list[Evidence] = []
    seen: set[str] = set()
    for item in items:
        key = (
            f"structured:{item.source_id}:{item.id}"
            if item.evidence_kind == "structured"
            else str(item.url).split("#", 1)[0]
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= max_items:
            break
    return deduped


class UrlCollectionHarness:
    """Layer 1: collect a broad but governed pool of candidate URLs."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str,
        max_output_tokens: int,
        max_tool_rounds: int = 8,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._max_tool_rounds = max_tool_rounds
        self._skill_text = _load_agent_skill_text(
            "source-discovery",
            "URL 收集层只允许使用 Source Registry 已登记的只读发现源。",
        )

    async def plan(self, request: ConsumerReportRequest) -> ResearchPlan:
        fallback = fallback_research_plan(request)
        schema = ResearchPlan.model_json_schema()
        try:
            result = await self._provider.generate_json(
                LLMRequest(
                    purpose="research_plan",
                    model=self._model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                            "你是足球资料 URL 收集规划器。只生成搜索计划，"
                            "不写事实。查询词优先英文，可根据球队、球员和联赛"
                            "补充原语言发布者查询；只能使用 rss、gdelt、"
                            "newsapi 三类已登记发现源。不要加入社媒、任意网页"
                            "抓取或未登记来源；候选必须保留原链接以便回溯。"
                            "如果请求包含 time_scope，查询计划必须服务于该北京时间"
                            "自然日窗口，不得按模型当前日期扩到其他比赛日。"
                            "输出 JSON，"
                                f"Schema: {json.dumps(schema, ensure_ascii=False)}"
                                "\n\n来源发现 SKILL：\n"
                                + self._skill_text
                            ),
                        },
                        {"role": "user", "content": request.model_dump_json()},
                    ],
                    thinking_enabled=False,
                    max_output_tokens=self._max_output_tokens,
                    metadata={
                        "report_type": request.report_type.value,
                        "subject": request.subject,
                        "fallback_queries": [item.query for item in fallback.queries],
                    },
                )
            )
            plan = ResearchPlan.model_validate(result.output)
        except (LLMProviderError, ValidationError, ValueError):
            return _with_focus_alias_queries(request, fallback)
        return _with_focus_alias_queries(request, plan if plan.queries else fallback)

    async def collect(
        self, request: ConsumerReportRequest, *, max_items: int
    ) -> UrlCollectionResult:
        plan = await self.plan(request)
        warnings: list[str] = []
        critical_subject_mode = bool(
            matching_critical_entities(" ".join([request.subject, *request.focus]))
        )
        combined: list[Evidence] = (
            collect_critical_entity_evidence(request) if critical_subject_mode else []
        )
        attempts: dict[str, str] = {}
        tool_rounds = 0
        empty_rounds = 0
        candidate_limit = max(max_items * 4, 24)
        if combined:
            attempts["critical_entities"] = f"ok:{len(combined)}"

        for query in plan.queries:
            if tool_rounds >= self._max_tool_rounds:
                warnings.append("URL 收集层达到工具轮次预算，已停止继续扩搜。")
                break
            before = len(combined)
            used = await self._collect_query(
                query,
                request,
                combined=combined,
                attempts=attempts,
                max_items=candidate_limit,
                remaining_tool_rounds=self._max_tool_rounds - tool_rounds,
            )
            tool_rounds += used
            if len(combined) == before:
                empty_rounds += 1
            else:
                empty_rounds = 0
            if empty_rounds >= 2:
                warnings.append("连续两轮没有新增候选 URL，URL 收集层提前停止。")
                break

        discovered_critical = (
            collect_critical_entity_evidence(
                request,
                extra_text=" ".join(
                    f"{item.title} {item.summary} {item.source_name}"
                    for item in combined
                ),
            )
            if critical_subject_mode
            else []
        )
        if discovered_critical:
            known_ids = {item.id for item in combined}
            additions = [
                item for item in discovered_critical if item.id not in known_ids
            ]
            if additions:
                combined = [*additions, *combined]
                attempts["critical_entities_from_evidence"] = f"ok:{len(additions)}"

        candidates = _dedupe_evidence(combined, max_items=candidate_limit)
        ok_attempts = sum(1 for status in attempts.values() if status.startswith("ok:"))
        return UrlCollectionResult(
            plan=plan,
            candidates=candidates,
            warnings=warnings,
            source_attempts=attempts,
            loop=LayerLoopSummary(
                name="url_collection",
                label="第一层：URL 资料收集",
                status="completed" if candidates else "degraded",
                model_rounds=1,
                tool_rounds=tool_rounds,
                input_count=len(plan.queries),
                output_count=len(candidates),
                warnings=warnings[:8],
                checkpoints=[
                    f"planned_queries={len(plan.queries)}",
                    f"tool_rounds={tool_rounds}/{self._max_tool_rounds}",
                    f"successful_sources={ok_attempts}",
                    f"candidate_urls={len(candidates)}",
                ],
            ),
        )

    async def _collect_query(
        self,
        query: ResearchQuery,
        request: ConsumerReportRequest,
        *,
        combined: list[Evidence],
        attempts: dict[str, str],
        max_items: int,
        remaining_tool_rounds: int,
    ) -> int:
        if remaining_tool_rounds <= 0:
            return 0
        query_request = request.model_copy(update={"subject": query.query})
        source_limit = max(4, min(12, max_items // 2))
        if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
            source_limit = max(source_limit, min(15, max_items))
        sources = query.sources or ["rss", "gdelt", "newsapi"]
        tasks: list[tuple[str, Awaitable[list[Evidence]]]] = []

        def add_task(
            source_id: str, factory: Callable[[], Awaitable[list[Evidence]]]
        ) -> None:
            if len(tasks) >= remaining_tool_rounds:
                return
            tasks.append((source_id, factory()))

        if "rss" in sources:
            add_task(
                "guardian_search",
                lambda: collect_guardian_search_evidence(
                    query_request, max_items=source_limit
                ),
            )
            add_task(
                "guardian_rss",
                lambda: collect_guardian_evidence(
                    query_request, max_items=source_limit
                ),
            )
            add_task(
                "bbc_rss",
                lambda: collect_bbc_evidence(query_request, max_items=source_limit),
            )
        if "gdelt" in sources:
            add_task(
                "gdelt",
                lambda: collect_gdelt_evidence(query_request, max_items=source_limit),
            )
        if "newsapi" in sources:
            add_task(
                "newsapi",
                lambda: collect_newsapi_evidence(query_request, max_items=source_limit),
            )
        results = await asyncio.gather(
            *(task for _, task in tasks), return_exceptions=True
        )
        for (source_id, _), result in zip(tasks, results, strict=False):
            key = f"{source_id}:{query.purpose}"
            if isinstance(result, BaseException):
                attempts.setdefault(key, result.__class__.__name__)
                continue
            attempts[key] = f"ok:{len(result)}"
            combined.extend(result)
        return len(tasks)


class EvidenceRefinementHarness:
    """Layer 2: turn candidate URLs into compact, citable evidence notes."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str,
        max_output_tokens: int,
        article_reader: Callable[[str], Awaitable[ArticleExcerpt | None]] | None = None,
        max_article_reads: int = 10,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._article_reader = article_reader
        self._max_article_reads = max_article_reads
        self._skill_text = _load_agent_skill_text(
            "evidence-refinement",
            "资料精简层只压缩候选资料，不新增事实，并保留原链接。",
        )

    async def refine(
        self,
        request: ConsumerReportRequest,
        candidates: list[Evidence],
        *,
        max_items: int,
    ) -> RefinementLayerResult:
        if not candidates:
            warning = "资料精简层没有收到候选 URL。"
            return RefinementLayerResult(
            evidence=[],
            warnings=[warning],
            loop=LayerLoopSummary(
                name="evidence_refinement",
                label="第二层：资料精简提炼",
                    status="degraded",
                input_count=0,
                output_count=0,
                warnings=[warning],
                checkpoints=["no_candidates"],
            ),
        )

        selected = finish_evidence_selection(request, candidates, max_items=max_items)
        article_excerpts = await self._read_article_excerpts(selected)
        model_rounds = 0
        try:
            refined = await self._run_model_refinement(
                request, selected, article_excerpts
            )
            model_rounds = 1
            evidence = self._apply_refinement(selected, refined)
            warnings = refined.warnings
            status: LayerStatus = "completed"
        except (LLMProviderError, ValidationError, ValueError) as exc:
            evidence = self._fallback_refinement(selected)
            warnings = [
                "资料精简层模型输出未通过结构化解析"
                f"（{_model_failure_detail(exc)}），已使用确定性短摘要兜底。"
            ]
            status = "degraded"

        return RefinementLayerResult(
            evidence=evidence,
            warnings=warnings,
            loop=LayerLoopSummary(
                name="evidence_refinement",
                label="第二层：资料精简提炼",
                status=status,
                model_rounds=model_rounds,
                tool_rounds=len(article_excerpts),
                input_count=len(selected),
                output_count=len(evidence),
                warnings=warnings[:8],
                checkpoints=[
                    f"selected_candidates={len(selected)}",
                    f"article_excerpts={len(article_excerpts)}",
                    f"model_rounds={model_rounds}",
                    f"handoff_evidence={len(evidence)}",
                ],
            ),
        )

    async def _read_article_excerpts(
        self, selected: list[Evidence]
    ) -> dict[str, ArticleExcerpt]:
        if not self._article_reader:
            return {}
        targets = selected[: self._max_article_reads]
        results = await asyncio.gather(
            *(self._article_reader(str(item.url)) for item in targets),
            return_exceptions=True,
        )
        excerpts: dict[str, ArticleExcerpt] = {}
        for item, result in zip(targets, results, strict=False):
            if isinstance(result, ArticleExcerpt):
                excerpts[item.id] = result
        return excerpts

    async def _run_model_refinement(
        self,
        request: ConsumerReportRequest,
        selected: list[Evidence],
        article_excerpts: dict[str, ArticleExcerpt],
    ) -> EvidenceRefinementResult:
        schema = EvidenceRefinementResult.model_json_schema()
        candidates = [
            {
                "id": item.id,
                "source": item.source_name,
                "status": item.verification_status,
                "kind": item.evidence_kind,
                "trust_tier": item.trust_tier,
                "published_at": item.published_at.isoformat(),
                "title": truncate_text(item.title, 180),
                "summary": truncate_text(item.summary, 1200),
                "article_excerpt": truncate_text(
                    article_excerpts[item.id].text, 2400
                )
                if item.id in article_excerpts
                else "",
                "url": str(item.url),
            }
            for item in selected
        ]
        result = await self._provider.generate_json(
            LLMRequest(
                purpose="evidence_refinement",
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是足球资料精简编辑。只压缩候选资料，不新增事实。"
                            "每条输出必须引用 source_evidence_id，并在 original_url"
                            "填写原候选 URL；保留不确定性、传闻标签、比分/金额/"
                            "时间等关键数字，删除重复和空话。"
                            "如果候选含 article_excerpt，优先按该正文片段提炼；"
                            "不得写入正文片段、RSS 摘要或标题之外的细节。"
                            "如果请求包含 time_scope，保留该窗口内事件的北京时间"
                            "日期信号，不要把窗口外内容改写成当天事实。"
                            "精简摘要要能让撰写层知道该回溯哪条原链接。"
                            "输出 JSON，"
                            f"Schema: {json.dumps(schema, ensure_ascii=False)}"
                            "\n\n资料精简 SKILL：\n"
                            + self._skill_text
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "report_type": request.report_type.value,
                                "subject": request.subject,
                                "report_date": request.report_date.isoformat(),
                                "time_scope": (
                                    request.time_scope.model_dump(mode="json")
                                    if request.time_scope
                                    else None
                                ),
                                "candidates": candidates,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                thinking_enabled=False,
                max_output_tokens=min(self._max_output_tokens, 4800),
                metadata={
                    "report_type": request.report_type.value,
                    "subject": request.subject,
                    "candidate_ids": [item.id for item in selected],
                },
            )
        )
        refined = EvidenceRefinementResult.model_validate(result.output)
        allowed = {item.id for item in selected}
        if not refined.items or any(
            item.source_evidence_id not in allowed for item in refined.items
        ):
            raise ValueError("refinement omitted all candidates or cited unknown ids")
        return refined

    @staticmethod
    def _apply_refinement(
        selected: list[Evidence], refined: EvidenceRefinementResult
    ) -> list[Evidence]:
        by_id = {item.id: item for item in selected}
        output: list[Evidence] = []
        seen: set[str] = set()
        for item in refined.items:
            original = by_id.get(item.source_evidence_id)
            if original is None or original.id in seen:
                continue
            points = "；".join(
                truncate_text(point, 180) for point in item.key_points[:5]
            )
            summary = item.concise_summary
            if points:
                summary = f"{summary} 关键点：{points}。"
            if original.id.startswith("critical-"):
                title = original.title
                summary = f"{original.summary} 精简补充：{summary}"
            else:
                title = truncate_text(original.title, 240)
                source_signal = truncate_text(original.summary, 500)
                if source_signal and source_signal not in summary:
                    summary = f"{summary} 来源原摘：{source_signal}"
            output.append(
                original.model_copy(
                    update={
                        "title": title,
                        "summary": truncate_text(f"精简提炼：{summary}", 1800),
                    }
                )
            )
            seen.add(original.id)
        for original in selected:
            if original.id not in seen:
                output.append(
                    original.model_copy(
                        update={
                            "summary": truncate_text(
                                f"精简提炼：{original.summary}", 1400
                            )
                        }
                    )
                )
        return output

    @staticmethod
    def _fallback_refinement(selected: list[Evidence]) -> list[Evidence]:
        return [
            item.model_copy(
                update={
                    "summary": truncate_text(
                        f"精简提炼：{item.summary}",
                        1800 if item.id.startswith("critical-") else 1400,
                    )
                }
            )
            for item in selected
        ]


class EnhancementHarness:
    """Layer 3: decide and collect bounded enrichments requested by layer 2."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str,
        max_output_tokens: int,
        media_enabled: bool,
        youtube_api_key: str | None,
        youtube_channel_ids: tuple[str, ...],
        max_tool_rounds: int = 10,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._media_enabled = media_enabled
        self._youtube_api_key = youtube_api_key
        self._youtube_channel_ids = youtube_channel_ids
        self._max_tool_rounds = max_tool_rounds
        self._skill_text = self._load_skill_text()

    async def enhance(
        self, request: ConsumerReportRequest, evidence: list[Evidence]
    ) -> EnhancementLayerResult:
        if not evidence:
            return EnhancementLayerResult(
                warnings=["增强层没有收到可用精简资料。"],
                loop=LayerLoopSummary(
                    name="enhancement",
                    label="第三层：增强补采",
                    status="skipped",
                    input_count=0,
                    output_count=0,
                    warnings=["增强层没有收到可用精简资料。"],
                ),
            )
        model_rounds = 0
        try:
            plan = await self._plan(request, evidence)
            model_rounds = 1
        except (LLMProviderError, ValidationError, ValueError) as exc:
            plan = self._fallback_plan(request, evidence)
            plan = plan.model_copy(
                update={
                    "warnings": [
                        *plan.warnings,
                        "增强层模型输出未通过结构化解析"
                        f"（{_model_failure_detail(exc)}），已使用确定性增强计划。",
                    ][:8]
                }
            )
        plan = self._with_deterministic_visual_needs(request, evidence, plan)
        additional: list[Evidence] = []
        assets: list[MediaAsset] = []
        warnings = list(plan.warnings)
        tool_rounds = 0

        for need in sorted(plan.needs, key=lambda item: item.priority)[:8]:
            if tool_rounds >= self._max_tool_rounds:
                warnings.append("增强层达到工具轮次预算，已停止补采。")
                break
            if need.kind == "gif":
                warnings.append(self._manual_visual_hint(need))
                continue
            tool_rounds += 1
            if need.kind == "licensed_image":
                asset = await self._collect_commons_image(need)
                if asset:
                    assets.append(asset)
                else:
                    warnings.append(f"未找到可自动附加的许可图片：{need.target}")
            elif need.kind == "official_video":
                asset = await self._collect_official_video(need)
                if asset:
                    assets.append(asset)
                else:
                    warnings.append(f"未找到可自动附加的官方视频：{need.target}")
            elif need.kind == "player_profile":
                profile = await self._collect_player_profile(need)
                if profile:
                    additional.append(profile)
                else:
                    warnings.append(f"未能补到结构化球员资料：{need.target}")
            elif need.kind == "match_context":
                warnings.append(
                    "比赛结构化上下文由 football-data/Sportmonks 写作入口单独处理。"
                )
            elif need.kind == "club_context":
                warnings.append(
                    "俱乐部增强需要已授权球队资料接口；当前未自动补采俱乐部页。"
                )

        assets = self._dedupe_assets(assets)
        status: LayerStatus = (
            "completed" if additional or assets or not plan.needs else "degraded"
        )
        executed_needs = [need.kind for need in plan.needs[:8]]
        return EnhancementLayerResult(
            additional_evidence=_dedupe_evidence(additional, max_items=12),
            media_assets=assets,
            warnings=list(dict.fromkeys(warnings))[:12],
            loop=LayerLoopSummary(
                name="enhancement",
                label="第三层：增强补采",
                status=status,
                model_rounds=model_rounds,
                tool_rounds=tool_rounds,
                input_count=len(evidence),
                output_count=len(additional) + len(assets),
                warnings=list(dict.fromkeys(warnings))[:8],
                checkpoints=[
                    f"planned_needs={len(plan.needs)}",
                    f"need_kinds={','.join(executed_needs) or 'none'}",
                    f"media_assets={len(assets)}",
                    f"additional_evidence={len(additional)}",
                    f"tool_rounds={tool_rounds}/{self._max_tool_rounds}",
                ],
            ),
        )

    async def _plan(
        self, request: ConsumerReportRequest, evidence: list[Evidence]
    ) -> EnhancementPlan:
        schema = EnhancementPlan.model_json_schema()
        result = await self._provider.generate_json(
            LLMRequest(
                purpose="enhancement_plan",
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是足球报道增强层调度员。只判断还需要哪些补充资料，"
                            "不要写报道。严格遵守增强 SKILL 和 Source Registry 边界。"
                            "GIF/比赛动图如果没有批准来源，只能提出人工补充，不得自动抓取。"
                            "输出 JSON，Schema: "
                            f"{json.dumps(schema, ensure_ascii=False)}"
                            "\n\n增强 SKILL：\n"
                            + self._skill_text
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "report_type": request.report_type.value,
                                "subject": request.subject,
                                "evidence_index": evidence_index(evidence),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                thinking_enabled=False,
                max_output_tokens=min(self._max_output_tokens, 2600),
                metadata={
                    "report_type": request.report_type.value,
                    "subject": request.subject,
                    "evidence_ids": [item.id for item in evidence],
                },
            )
        )
        plan = EnhancementPlan.model_validate(result.output)
        allowed = {item.id for item in evidence}
        for need in plan.needs:
            if set(need.evidence_ids) - allowed:
                raise ValueError("enhancement need cites unknown evidence")
        return plan

    @staticmethod
    def _with_deterministic_visual_needs(
        request: ConsumerReportRequest,
        evidence: list[Evidence],
        plan: EnhancementPlan,
    ) -> EnhancementPlan:
        existing = {
            (need.kind, need.target.casefold(), tuple(need.evidence_ids))
            for need in plan.needs
        }
        additions: list[EnhancementNeed] = []
        seen_match_keys: set[str] = set()
        cover_added = False
        for item in evidence[:10]:
            if item.id.startswith("critical-"):
                continue
            text = f"{item.title} {item.summary}"
            if not is_completed_match_evidence(item):
                continue
            if not MATCH_VISUAL_RE.search(text):
                continue
            teams = _known_team_names(text)
            match_key = " vs ".join(teams[:2]) if len(teams) >= 2 else item.id
            if match_key in seen_match_keys:
                continue
            seen_match_keys.add(match_key)
            placement_reason = (
                "赛事首图：优先使用官方高光视频缩略图作为日报头图。"
                if not cover_added
                else "赛事栏目图：为该场比赛寻找官方高光缩略图。"
            )
            cover_added = True
            title_target = truncate_text(
                (
                    f"{match_key} FIFA World Cup 2026 official highlights"
                    if match_key != item.id
                    else f"{item.title} official highlights"
                ),
                160,
            )
            key = ("official_video", title_target.casefold(), (item.id,))
            if key not in existing:
                additions.append(
                    EnhancementNeed(
                        kind="official_video",
                        target=title_target,
                        reason=placement_reason,
                        evidence_ids=[item.id],
                        priority=1,
                    )
                )
                existing.add(key)
            if re.search(r"\b(goal|scored|winner|equaliser|penalty|var)\b", text, re.I):
                event_target = truncate_text(
                    f"{item.title} goal VAR official highlight", 160
                )
                key = ("official_video", event_target.casefold(), (item.id,))
                if key not in existing:
                    additions.append(
                        EnhancementNeed(
                            kind="official_video",
                            target=event_target,
                            reason="关键事件画面：插到进球、VAR 或时间线事件下方。",
                            evidence_ids=[item.id],
                            priority=2,
                        )
                    )
                    existing.add(key)
            for player in _known_player_image_targets(text):
                key = ("licensed_image", player.casefold(), (item.id,))
                if key not in existing:
                    additions.append(
                        EnhancementNeed(
                            kind="licensed_image",
                            target=player,
                            reason=(
                                "球员图候选：优先寻找可许可的人物/庆祝图片，"
                                "用于战报小节配图。"
                            ),
                            evidence_ids=[item.id],
                            priority=3,
                        )
                    )
                    existing.add(key)
            for player in _person_image_candidates(text):
                key = ("licensed_image", player.casefold(), (item.id,))
                if key not in existing:
                    additions.append(
                        EnhancementNeed(
                            kind="licensed_image",
                            target=player,
                            reason=(
                                "人物图候选：证据标题或摘要出现该球员/教练，"
                                "优先查 Wikimedia Commons 许可图片。"
                            ),
                            evidence_ids=[item.id],
                            priority=4,
                        )
                    )
                    existing.add(key)
        ordered_needs = sorted(
            [*additions, *plan.needs], key=lambda need: need.priority
        )
        return plan.model_copy(update={"needs": ordered_needs[:10]})

    @staticmethod
    def _fallback_plan(
        request: ConsumerReportRequest, evidence: list[Evidence]
    ) -> EnhancementPlan:
        needs: list[EnhancementNeed] = []
        if request.report_type in {
            ReportType.DAILY_FOOTBALL_DIGEST,
            ReportType.TRANSFER_DAILY,
        }:
            names: list[tuple[str, str]] = []
            for item in evidence:
                text = f"{item.title} {item.summary}"
                for name in re.findall(
                    r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,2}\b", text
                ):
                    if name in {
                        "World Cup",
                        "Premier League",
                        "The Guardian",
                        "BBC Sport",
                    }:
                        continue
                    names.append((name, item.id))
            for name, evidence_id in list(dict(names).items())[:2]:
                needs.append(
                    EnhancementNeed(
                        kind="player_profile",
                        target=name,
                        reason="转会或人物叙事可能需要结构化球员资料。",
                        evidence_ids=[evidence_id],
                        priority=2,
                    )
                )
                needs.append(
                    EnhancementNeed(
                        kind="licensed_image",
                        target=name,
                        reason="人物卡可能需要许可图片。",
                        evidence_ids=[evidence_id],
                        priority=4,
                    )
                )
        if request.report_type == ReportType.MATCH_PREDICTION:
            needs.append(
                EnhancementNeed(
                    kind="match_context",
                    target=request.subject,
                    reason="预测报告需要结构化赛果、阵容或伤停上下文。",
                    evidence_ids=[evidence[0].id],
                    priority=1,
                )
            )
            needs.append(
                EnhancementNeed(
                    kind="official_video",
                    target=f"{request.subject} official highlights team news",
                    reason="比赛预测和复盘可用官方视频补充关键画面或赛前信息。",
                    evidence_ids=[evidence[0].id],
                    priority=4,
                )
            )
        if any(MATCH_VISUAL_RE.search(item.summary) for item in evidence):
            needs.append(
                EnhancementNeed(
                    kind="official_video",
                    target=f"{request.subject} official match highlights goal",
                    reason="证据提到关键进球或高光，优先查官方频道可嵌入视频。",
                    evidence_ids=[evidence[0].id],
                    priority=3,
                )
            )
            needs.append(
                EnhancementNeed(
                    kind="gif",
                    target=f"{request.subject} key goal or highlight",
                    reason="证据提到关键进球或高光，适合生成一条人工补图任务。",
                    evidence_ids=[evidence[0].id],
                    priority=5,
                )
            )
        return EnhancementPlan(needs=needs)

    async def _collect_commons_image(self, need: EnhancementNeed) -> MediaAsset | None:
        if not self._media_enabled:
            return None
        try:
            asset = await search_commons_player_image(need.target)
        except Exception:
            return None
        if not asset:
            return None
        asset = await cache_media_thumbnail(asset)
        return asset.model_copy(
            update={
                "placement": "spotlight",
                "target": need.target,
                "evidence_ids": need.evidence_ids,
            }
        )

    async def _collect_official_video(self, need: EnhancementNeed) -> MediaAsset | None:
        if not (
            self._media_enabled and self._youtube_api_key and self._youtube_channel_ids
        ):
            return None
        try:
            asset = await search_official_youtube_video(
                need.target,
                self._youtube_api_key,
                list(self._youtube_channel_ids),
            )
        except Exception:
            return None
        if not asset:
            return None
        if "赛事首图" in need.reason:
            placement = "report_cover"
        elif "赛事栏目图" in need.reason:
            placement = "section"
        elif "关键事件画面" in need.reason or re.search(
            r"goal|highlight|winner|equaliser|var|penalty|进球|高光",
            need.target,
            re.I,
        ):
            placement = "timeline"
        else:
            placement = "section"
        asset = await cache_media_thumbnail(asset)
        return asset.model_copy(
            update={
                "placement": placement,
                "target": need.target,
                "evidence_ids": need.evidence_ids,
            }
        )

    async def _collect_player_profile(self, need: EnhancementNeed) -> Evidence | None:
        token = os.getenv("SPORTMONKS_API_TOKEN")
        if not token:
            return None
        try:
            search_payload = await get_json(
                "https://api.sportmonks.com/v3/football/players/search/"
                + quote(need.target),
                headers={"Accept": "application/json", "Authorization": token},
            )
            candidates = search_payload.get("data") or []
            if not candidates:
                return None
            player_id = candidates[0].get("id")
            if not isinstance(player_id, int):
                return None
            profile_payload = await get_json(
                f"https://api.sportmonks.com/v3/football/players/{player_id}",
                params={"include": "position;nationality"},
                headers={"Accept": "application/json", "Authorization": token},
            )
        except Exception:
            return None
        player = profile_payload.get("data") or candidates[0]
        name = str(
            player.get("display_name")
            or player.get("name")
            or candidates[0].get("display_name")
            or need.target
        )
        position = player.get("position") or {}
        nationality = player.get("nationality") or {}
        summary_parts = [
            f"Sportmonks 结构化球员资料增强：{name}",
            f"player_id={player_id}",
        ]
        if isinstance(position, dict) and position.get("name"):
            summary_parts.append(f"位置：{position['name']}")
        elif player.get("position_id"):
            summary_parts.append(f"position_id={player['position_id']}")
        if isinstance(nationality, dict) and nationality.get("name"):
            summary_parts.append(f"国籍：{nationality['name']}")
        summary_parts.append(
            "该资料的展示和再分发取决于 Sportmonks 订阅合同；当前只写入规范化字段摘要。"
        )
        evidence_id = "sportmonks-player-" + hashlib.sha256(
            f"{player_id}:{name}".encode()
        ).hexdigest()[:12]
        return Evidence(
            id=evidence_id,
            title=f"{name} 结构化球员资料",
            url="https://www.sportmonks.com/football-api/",
            published_at=datetime.now(UTC),
            source_name="Sportmonks Football API",
            summary="；".join(summary_parts),
            source_id="sportmonks",
            trust_tier="S1_structured_provider",
            evidence_kind="structured",
            verification_status="corroborated",
            source_independence_key="sportmonks",
        )

    @staticmethod
    def _load_skill_text() -> str:
        return _load_agent_skill_text(
            "research-enhancement",
            "增强层只允许调用 Source Registry 已登记的只读资料和许可媒体工具。",
        )

    @staticmethod
    def _manual_visual_hint(need: EnhancementNeed) -> str:
        return (
            "关键画面需人工补充："
            f"{need.target}。建议优先查赛事/俱乐部官方比赛中心、官方高光视频"
            "或已授权图库；GIF/比赛动图当前没有批准自动来源，系统不会抓取"
            "新闻配图、社媒截图或比赛片段。"
        )

    @staticmethod
    def _dedupe_assets(assets: list[MediaAsset]) -> list[MediaAsset]:
        output: list[MediaAsset] = []
        seen: set[str] = set()
        for asset in assets:
            key = str(asset.url)
            if key in seen:
                continue
            seen.add(key)
            output.append(asset)
            if len(output) >= 8:
                break
        return output


class LeaderReviewHarness:
    """Layer 4: global supervisor for column planning and handoff quality."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str,
        max_output_tokens: int,
        max_repair_attempts: int = 1,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._max_repair_attempts = max_repair_attempts

    async def review(
        self,
        request: ConsumerReportRequest,
        *,
        url_result: UrlCollectionResult,
        refinement: RefinementLayerResult,
        enhancement: EnhancementLayerResult,
        evidence: list[Evidence],
    ) -> LeaderReviewResult:
        fallback_plan = self._fallback_editorial_plan(request, evidence)
        model_rounds = 0
        plan_warnings: list[str] = []
        try:
            planned = await self._model_editorial_plan(request, evidence)
            editorial_plan = planned.columns or fallback_plan
            plan_warnings = planned.warnings
            model_rounds = 1
        except (LLMProviderError, ValidationError, ValueError) as exc:
            editorial_plan = fallback_plan
            plan_warnings = [
                "Leader 栏目规划模型输出未通过结构化解析"
                f"（{_model_failure_detail(exc)}），已使用确定性栏目计划。"
            ]
        editorial_plan = self._normalize_plan(editorial_plan, evidence)
        critical_subject_mode = bool(
            matching_critical_entities(" ".join([request.subject, *request.focus]))
        )
        critical_aliases = self._critical_aliases_for(evidence)
        critical_related = [
            item
            for item in evidence
            if self._is_critical_related(item, critical_aliases)
        ]
        topical_count = len(evidence) - len(critical_related)
        coverage_evidence = evidence if critical_subject_mode else [
            item for item in evidence if item not in critical_related
        ]
        categories = self._coverage_categories(coverage_evidence)
        editorial_plan = self._ensure_required_groups(
            editorial_plan, fallback_plan, categories
        )
        degraded_layers = [
            layer.name
            for layer in (url_result.loop, refinement.loop, enhancement.loop)
            if layer.status != "completed"
        ]

        warnings: list[str] = []
        warnings.extend(plan_warnings)
        block_generation = False
        if (
            evidence
            and not critical_subject_mode
            and critical_related
            and topical_count == 0
        ):
            warnings.append("监督层发现普通主题的证据坍缩为关键人物护栏。")
            block_generation = True
        elif (
            not critical_subject_mode
            and len(critical_related) >= 2
            and len(critical_related) > topical_count
        ):
            warnings.append("监督层发现关键人物材料占比过高，写作层需以非人物主线为主。")
        if degraded_layers:
            warnings.append(
                "监督层记录上游降级："
                + ",".join(str(layer) for layer in degraded_layers[:4])
            )
        if (
            request.report_type
            in {ReportType.DAILY_FOOTBALL_DIGEST, ReportType.WORLD_CUP_DAILY}
            and topical_count >= 2
            and len(categories) < 2
        ):
            warnings.append("监督层发现栏目覆盖偏窄，建议合并成少数厚栏目或人工补采。")
        if (
            request.report_type
            in {
                ReportType.DAILY_FOOTBALL_DIGEST,
                ReportType.WORLD_CUP_DAILY,
                ReportType.MATCH_PREDICTION,
            }
            and not enhancement.media_assets
        ):
            warnings.append("监督层未看到可交付的官方视频或许可图片素材。")

        status: LayerStatus = "degraded" if warnings else "completed"
        decision = "block" if block_generation else "approve"
        return LeaderReviewResult(
            block_generation=block_generation,
            editorial_plan=editorial_plan,
            loop=LayerLoopSummary(
                name="leader_review",
                label="第四层：Leader 分栏监督",
                status=status,
                model_rounds=model_rounds,
                tool_rounds=0,
                input_count=len(evidence),
                output_count=len(editorial_plan),
                warnings=warnings[:8],
                checkpoints=[
                    f"decision={decision}",
                    f"columns={len(editorial_plan)}",
                    f"critical_subject_mode={str(critical_subject_mode).lower()}",
                    f"topical_evidence={topical_count}",
                    f"critical_related={len(critical_related)}",
                    f"groups={','.join(self._plan_groups(editorial_plan)) or 'none'}",
                    f"media_assets={len(enhancement.media_assets)}",
                ],
            ),
        )

    async def _model_editorial_plan(
        self, request: ConsumerReportRequest, evidence: list[Evidence]
    ) -> LeaderColumnPlanResult:
        schema = LeaderColumnPlanResult.model_json_schema()
        result = await self._provider.generate_json(
            LLMRequest(
                purpose="leader_column_plan",
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是足球日报 Leader。先规划栏目和负责小组，不写正文。"
                            "从预设小组中选择：match_report、transfer_intel、"
                            "coach_tactics、player_profile、off_field、context。"
                            "如果请求包含 time_scope，栏目规划必须以该北京时间"
                            "自然日为准；窗口外赛果或新闻只能作为背景，不得成为"
                            "当日主线。"
                            "每个栏目必须有清晰主题、category、evidence_ids、"
                            "enrichment_targets 和 media_targets。关键人物状态只作"
                            "事实护栏，除非用户主题就是该人物，否则不要把它规划成"
                            "头条栏目。每个小组最多 4 轮收集/反思，失败转人工。"
                            "先判断 evidence event_state：completed_match、"
                            "upcoming_fixture、off_field、transfer。"
                            "match_report 只能使用 completed_match；"
                            "preview/arrival/kickoff/hotel/schedule/hostile reception "
                            "必须规划到 off_field 或 context。"
                            "输出 JSON，Schema: "
                            f"{json.dumps(schema, ensure_ascii=False)}"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "report_type": request.report_type.value,
                                "subject": request.subject,
                                "report_date": request.report_date.isoformat(),
                                "time_scope": (
                                    request.time_scope.model_dump(mode="json")
                                    if request.time_scope
                                    else None
                                ),
                                "focus": request.focus,
                                "evidence_index": evidence_index(
                                    evidence, summary_chars=420
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                thinking_enabled=False,
                max_output_tokens=min(self._max_output_tokens, 3800),
                metadata={
                    "report_type": request.report_type.value,
                    "subject": request.subject,
                    "evidence_ids": [item.id for item in evidence],
                },
            )
        )
        planned = LeaderColumnPlanResult.model_validate(result.output)
        allowed = {item.id for item in evidence}
        for column in planned.columns:
            if set(column.evidence_ids) - allowed:
                raise ValueError("leader plan cites unknown evidence")
        return planned

    def _fallback_editorial_plan(
        self, request: ConsumerReportRequest, evidence: list[Evidence]
    ) -> list[EditorialColumnPlan]:
        critical_aliases = self._critical_aliases_for(evidence)
        topical = (
            evidence
            if matching_critical_entities(" ".join([request.subject, *request.focus]))
            else [
                item
                for item in evidence
                if not self._is_critical_related(item, critical_aliases)
            ]
        )
        buckets: dict[str, list[Evidence]] = {
            "match_report": [],
            "transfer_intel": [],
            "coach_tactics": [],
            "off_field": [],
            "context": [],
        }
        for item in topical:
            text = f"{item.title} {item.summary}".casefold()
            if re.search(r"transfer|signing|bid|deal|agreement|medical", text):
                buckets["transfer_intel"].append(item)
            elif re.search(r"coach|manager|tactics|tuchel|nagelsmann", text):
                buckets["coach_tactics"].append(item)
            elif is_upcoming_match_evidence(item) or re.search(
                r"politics|fans|pubs|viewership|flag|city", text
            ):
                buckets["off_field"].append(item)
            elif is_completed_match_evidence(item):
                buckets["match_report"].append(item)
            elif evidence_is_match_like(item):
                buckets["context"].append(item)
            else:
                buckets["context"].append(item)
        columns: list[EditorialColumnPlan] = []
        specs = [
            ("match_report", "赛场主线", "match", 1),
            ("transfer_intel", "转会市场", "transfer", 2),
            ("coach_tactics", "战术与教练席", "context", 3),
            ("off_field", "场外与足球社会", "off_field", 4),
            ("context", "背景脉络", "context", 5),
        ]
        for group, title, category, priority in specs:
            members = buckets[group]
            if not members:
                continue
            media_targets = self._media_targets_for(group, members)
            columns.append(
                EditorialColumnPlan(
                    column_id=group,
                    title=title,
                    category=category,
                    specialist_group=group,
                    priority=priority,
                    evidence_ids=[item.id for item in members[:8]],
                    search_iterations=3,
                    enrichment_targets=self._enrichment_targets_for(group, members),
                    media_targets=media_targets,
                    coverage_requirements=self._coverage_requirements_for(group),
                    instructions=self._group_instruction(group),
                )
            )
        if not columns and evidence:
            columns.append(
                EditorialColumnPlan(
                    column_id="context",
                    title="背景脉络",
                    category="context",
                    specialist_group="context",
                    priority=5,
                    evidence_ids=[item.id for item in evidence[:8]],
                    coverage_requirements=self._coverage_requirements_for("context"),
                    instructions=self._group_instruction("context"),
                )
            )
        return columns[:5]

    @staticmethod
    def _normalize_plan(
        columns: list[EditorialColumnPlan], evidence: list[Evidence]
    ) -> list[EditorialColumnPlan]:
        allowed = {item.id for item in evidence}
        normalized: list[EditorialColumnPlan] = []
        seen: set[str] = set()
        group_positions: dict[str, int] = {}
        for index, column in enumerate(sorted(columns, key=lambda item: item.priority)):
            evidence_ids = [item for item in column.evidence_ids if item in allowed]
            if not evidence_ids:
                continue
            group = column.specialist_group
            if group in group_positions:
                position = group_positions[group]
                existing = normalized[position]
                merged_evidence_ids = list(
                    dict.fromkeys([*existing.evidence_ids, *evidence_ids])
                )[:12]
                merged_enrichment = list(
                    dict.fromkeys(
                        [*existing.enrichment_targets, *column.enrichment_targets]
                    )
                )[:8]
                merged_media = list(
                    dict.fromkeys([*existing.media_targets, *column.media_targets])
                )[:8]
                merged_requirements = list(
                    dict.fromkeys(
                        [
                            *existing.coverage_requirements,
                            *column.coverage_requirements,
                        ]
                    )
                )[:8]
                normalized[position] = existing.model_copy(
                    update={
                        "evidence_ids": merged_evidence_ids,
                        "enrichment_targets": merged_enrichment,
                        "media_targets": merged_media,
                        "coverage_requirements": merged_requirements,
                    }
                )
                continue
            column_id = column.column_id or f"column_{index + 1}"
            if column_id in seen:
                column_id = f"{column_id}_{index + 1}"
            seen.add(column_id)
            group_positions[group] = len(normalized)
            normalized.append(
                column.model_copy(
                    update={
                        "column_id": column_id,
                        "title": LeaderReviewHarness._group_title(group),
                        "evidence_ids": evidence_ids[:12],
                        "coverage_requirements": column.coverage_requirements
                        or LeaderReviewHarness._coverage_requirements_for(group),
                        "instructions": column.instructions
                        or LeaderReviewHarness._group_instruction(
                            group
                        ),
                    }
                )
            )
        return normalized[:5]

    @staticmethod
    def _group_title(group: str) -> str:
        return {
            "match_report": "赛场战报",
            "transfer_intel": "转会市场",
            "coach_tactics": "教练与战术",
            "player_profile": "球员侧写",
            "off_field": "场外焦点",
            "context": "背景脉络",
        }.get(group, "背景脉络")

    @staticmethod
    def _ensure_required_groups(
        columns: list[EditorialColumnPlan],
        fallback_plan: list[EditorialColumnPlan],
        categories: list[str],
    ) -> list[EditorialColumnPlan]:
        required_groups = {
            "match": "match_report",
            "transfer": "transfer_intel",
        }
        groups = {column.specialist_group for column in columns}
        expanded = list(columns)
        for category, group in required_groups.items():
            if category not in categories or group in groups:
                continue
            fallback = next(
                (
                    column
                    for column in fallback_plan
                    if column.specialist_group == group
                ),
                None,
            )
            if fallback is None:
                continue
            expanded.append(fallback)
            groups.add(group)
        return sorted(expanded, key=lambda item: item.priority)[:5]

    @staticmethod
    def _plan_groups(columns: list[EditorialColumnPlan]) -> list[str]:
        return list(dict.fromkeys(column.specialist_group for column in columns))

    @staticmethod
    def _group_instruction(group: str) -> str:
        return {
            "match_report": (
                "写成战报小组：只处理已完赛证据，分比赛处理，优先补进球/VAR/终场画面，"
                "不要把多场比赛揉成一段；赛前、抵达、开球安排和酒店接待转给场外或背景。"
            ),
            "transfer_intel": (
                "写成转会小组：必须补球员当前球队、目标球队、阶段、金额/年限"
                "和上赛季资料；没有证据就列为补采缺口。"
            ),
            "coach_tactics": (
                "写成教练/战术小组：区分教练动向、战术变化和球队成就，"
                "未经官方确认的离任只能写成报道线索。"
            ),
            "player_profile": "写成人物小组：补位置、俱乐部、赛季数据和角色。",
            "off_field": "写成场外小组：只处理球迷、政治、转播、城市和赛程影响。",
            "context": "写成背景小组：只做补充脉络，不抢头条。",
        }.get(group, "按证据整理背景，未知项要明确。")

    @staticmethod
    def _coverage_requirements_for(group: str) -> list[str]:
        return {
            "match_report": [
                "每场比赛独立成段",
                "包含对阵双方和最终比分或结果",
                "只有已完赛证据才能进入战报；赛前/抵达/开球安排不得进入战报",
                "没有事件细节时只交付已证实结果，不得用未知占位句填充正文",
                "说明晋级、淘汰或下一场影响",
                "绑定战报图、官方高光或人工补图目标",
            ],
            "transfer_intel": [
                "每条转会独立成段",
                "写清球员、当前球队、目标球队",
                "写清阶段：传闻、接触、报价、协议、体检、官宣或辟谣",
                "金额、合同年限、体检时间没有证据则明确未知",
                "至少区分 publisher_report 与 unverified_lead",
            ],
            "coach_tactics": [
                "区分教练履历、战术变化和离任传闻",
                "教练成绩或年份必须来自证据",
                "非官方离任只写成传闻",
            ],
            "player_profile": [
                "写清球员位置、现俱乐部和相关球队",
                "进球、助攻、出场等数字必须来自证据",
                "需要人物图时绑定许可图片或人工补图目标",
            ],
            "off_field": [
                "说明事件与比赛日、球迷、转播或城市影响的关系",
                "商业、政治或舆论判断必须引用来源观点",
            ],
            "context": [
                "只补背景脉络和未知项",
                "不得抢占战报或转会主标题",
            ],
        }.get(group, ["按证据整理栏目，事实、传闻和未知项必须分开。"])

    @staticmethod
    def _enrichment_targets_for(group: str, evidence: list[Evidence]) -> list[str]:
        targets: list[str] = []
        if group == "transfer_intel":
            for item in evidence:
                targets.extend(
                    re.findall(
                        r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,2}\b",
                        f"{item.title} {item.summary}",
                    )[:2]
                )
        return list(dict.fromkeys(targets))[:6]

    @staticmethod
    def _media_targets_for(group: str, evidence: list[Evidence]) -> list[str]:
        if group != "match_report":
            return []
        targets: list[str] = []
        for item in evidence:
            if not is_completed_match_evidence(item):
                continue
            teams = _known_team_names(f"{item.title} {item.summary}")
            if len(teams) >= 2:
                targets.append(f"{teams[0]} vs {teams[1]} match report image")
            if re.search(
                r"goal|scored|winner|equaliser|penalty|var", item.summary, re.I
            ):
                targets.append(f"{item.title} goal VAR highlight")
        return list(dict.fromkeys(targets))[:8]

    @staticmethod
    def _coverage_categories(evidence: list[Evidence]) -> list[str]:
        categories: list[str] = []
        for item in evidence:
            text = f"{item.title} {item.summary}".casefold()
            if is_completed_match_evidence(item):
                categories.append("match")
            elif is_upcoming_match_evidence(item):
                categories.append("off_field")
            if re.search(
                r"transfer|signing|signs|bid|deal|agreement|medical|target",
                text,
            ):
                categories.append("transfer")
            if re.search(
                r"viewership|fans|pubs|coach|manager|stadium|schedule|city|"
                r"broadcast",
                text,
            ):
                categories.append("off_field")
            if item.evidence_kind == "structured" or item.source_id in {
                "sportmonks",
                "football-data",
            }:
                categories.append("context")
        return sorted(dict.fromkeys(categories))

    @staticmethod
    def _critical_aliases_for(evidence: list[Evidence]) -> list[str]:
        if not evidence:
            return []
        registry = load_critical_entities()
        joined = " ".join(f"{item.id} {item.title} {item.summary}" for item in evidence)
        aliases: list[str] = []
        for entity in registry.entities:
            entity_aliases = [entity.canonical_name, *entity.aliases]
            if f"critical-{entity.id}" in joined or LeaderReviewHarness._contains_alias(
                joined, entity_aliases
            ):
                aliases.extend(entity_aliases)
        return list(dict.fromkeys(aliases))

    @staticmethod
    def _is_critical_related(item: Evidence, aliases: list[str]) -> bool:
        if item.id.startswith("critical-"):
            return True
        return LeaderReviewHarness._contains_alias(
            f"{item.id} {item.title} {item.summary}", aliases
        )

    @staticmethod
    def _contains_alias(text: str, aliases: list[str]) -> bool:
        folded = text.casefold()
        for alias in aliases:
            if re.search(r"[\u4e00-\u9fff]", alias):
                if alias in text:
                    return True
                continue
            alias_folded = alias.casefold()
            if re.search(
                rf"(?<![a-z0-9]){re.escape(alias_folded)}(?![a-z0-9])",
                folded,
            ):
                return True
        return False


class ResearchHarness:
    """Four-layer, bounded research pipeline for public report requests."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str,
        max_output_tokens: int = 1000,
        youtube_api_key: str | None = None,
        youtube_channel_ids: tuple[str, ...] = (),
        media_enabled: bool = True,
        structured_match_enabled: bool = False,
    ) -> None:
        self._url_layer = UrlCollectionHarness(
            provider,
            model=model,
            max_output_tokens=max_output_tokens,
        )
        self._refinement_layer = EvidenceRefinementHarness(
            provider,
            model=model,
            max_output_tokens=max_output_tokens,
            article_reader=read_article_excerpt,
        )
        self._enhancement_layer = EnhancementHarness(
            provider,
            model=model,
            max_output_tokens=max_output_tokens,
            media_enabled=media_enabled,
            youtube_api_key=youtube_api_key,
            youtube_channel_ids=youtube_channel_ids,
        )
        self._leader_layer = LeaderReviewHarness(
            provider,
            model=model,
            max_output_tokens=max_output_tokens,
        )
        self._structured_match_enabled = structured_match_enabled

    async def plan(self, request: ConsumerReportRequest) -> ResearchPlan:
        return await self._url_layer.plan(request)

    async def collect(
        self,
        request: ConsumerReportRequest,
        *,
        max_items: int,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> ResearchBundle:
        leader_seed_items = min(max(max_items, 4), INITIAL_LEADER_SEED_MAX_ITEMS)
        url_result = await self._url_layer.collect(
            request, max_items=max(leader_seed_items * 2, 12)
        )
        structured_evidence: list[Evidence] = []
        if self._structured_match_enabled:
            try:
                structured_evidence = await collect_daily_structured_match_evidence(
                    request
                )
            except Exception:
                structured_evidence = []
        if structured_evidence:
            structured_ids = {item.id for item in structured_evidence}
            merged_candidates = _dedupe_evidence(
                [
                    *structured_evidence,
                    *[
                        item
                        for item in url_result.candidates
                        if item.id not in structured_ids
                    ],
                ],
                max_items=max(leader_seed_items * 2, 12),
            )
            url_result = url_result.model_copy(
                update={
                    "candidates": merged_candidates,
                    "source_attempts": {
                        **url_result.source_attempts,
                        "football_data:daily_matches": f"ok:{len(structured_evidence)}",
                    },
                    "loop": url_result.loop.model_copy(
                        update={
                            "tool_rounds": min(url_result.loop.tool_rounds + 1, 20),
                            "output_count": len(merged_candidates),
                            "checkpoints": [
                                *url_result.loop.checkpoints,
                                f"structured_matches={len(structured_evidence)}",
                            ][:8],
                        }
                    ),
                }
            )
        if progress_callback:
            progress_callback("url_collection", 18)

        refinement_limit = min(20, leader_seed_items + len(structured_evidence))
        refinement = await self._refinement_layer.refine(
            request, url_result.candidates, max_items=refinement_limit
        )
        if structured_evidence:
            refined_ids = {item.id for item in refinement.evidence}
            missing_structured = [
                item for item in structured_evidence if item.id not in refined_ids
            ]
            if missing_structured:
                refinement = refinement.model_copy(
                    update={
                        "evidence": [*missing_structured, *refinement.evidence],
                        "loop": refinement.loop.model_copy(
                            update={
                                "output_count": len(refinement.evidence)
                                + len(missing_structured),
                                "checkpoints": [
                                    *refinement.loop.checkpoints,
                                    "forced_structured_matches="
                                    f"{len(missing_structured)}",
                                ][:8],
                            }
                        ),
                    }
                )
        if progress_callback:
            progress_callback("evidence_refinement", 34)

        seed_enhancement = EnhancementLayerResult(
            warnings=["Leader 初始分组前不做全局增强；增强由各小组内部执行。"],
            loop=LayerLoopSummary(
                name="enhancement",
                label="第三层：全局增强跳过",
                status="skipped",
                input_count=len(refinement.evidence),
                output_count=0,
                warnings=["增强由专栏小组内部执行。"],
                checkpoints=["deferred_to_column_teams=true"],
            ),
        )
        initial_leader = await self._leader_layer.review(
            request,
            url_result=url_result,
            refinement=refinement,
            enhancement=seed_enhancement,
            evidence=refinement.evidence,
        )
        if progress_callback:
            progress_callback("leader_review", 42)

        columns = initial_leader.editorial_plan
        if not columns and refinement.evidence:
            columns = self._leader_layer._fallback_editorial_plan(
                request, refinement.evidence
            )
        team_item_limit = min(24, max(8, max_items))
        team_results = await asyncio.gather(
            *(
                self._run_column_team_loop(
                    request,
                    column,
                    seed_evidence=refinement.evidence,
                    max_items=team_item_limit,
                )
                for column in columns[:6]
            ),
            return_exceptions=True,
        )
        column_team_results = [
            item for item in team_results if isinstance(item, ColumnTeamLoopResult)
        ]
        if progress_callback:
            progress_callback("column_team_loop", 48)

        evidence_limit = min(40, max(max_items, len(columns) * 5, 12))
        evidence = self._merge_column_team_evidence(
            request,
            refinement.evidence,
            column_team_results,
            max_items=evidence_limit,
        )
        media_assets = EnhancementHarness._dedupe_assets(
            [
                asset
                for result in column_team_results
                for asset in result.media_assets
            ]
        )
        team_warnings = [
            warning
            for result in column_team_results
            for warning in result.warnings
        ]
        source_attempts = dict(url_result.source_attempts)
        for result in column_team_results:
            source_attempts.update(result.source_attempts)
        aggregate_enhancement = EnhancementLayerResult(
            media_assets=media_assets,
            warnings=list(dict.fromkeys(team_warnings))[:12],
            loop=LayerLoopSummary(
                name="enhancement",
                label="第三层：小组增强汇总",
                status="completed" if media_assets else "degraded",
                model_rounds=min(
                    20, sum(result.loop.model_rounds for result in column_team_results)
                ),
                tool_rounds=min(
                    20, sum(result.loop.tool_rounds for result in column_team_results)
                ),
                input_count=len(evidence),
                output_count=len(media_assets),
                warnings=list(dict.fromkeys(team_warnings))[:8],
                checkpoints=[
                    f"column_teams={len(column_team_results)}",
                    f"media_assets={len(media_assets)}",
                    f"team_evidence={len(evidence)}",
                ],
            ),
        )
        leader_review = await self._leader_layer.review(
            request,
            url_result=url_result,
            refinement=refinement,
            enhancement=aggregate_enhancement,
            evidence=evidence,
        )
        if progress_callback:
            progress_callback("leader_review", 50)

        warnings = self._warnings(
            url_result.plan,
            evidence,
            source_attempts,
            [
                *url_result.warnings,
                *refinement.warnings,
                *initial_leader.loop.warnings,
                *team_warnings,
                *aggregate_enhancement.warnings,
            ],
        )
        layer_runs = [
            url_result.loop,
            refinement.loop,
            initial_leader.loop.model_copy(
                update={"label": "第三层：Leader 初始分组"}
            ),
            *[result.loop for result in column_team_results],
            aggregate_enhancement.loop,
            leader_review.loop,
            LayerLoopSummary(
                name="writing_handoff",
                label="第五层：撰写层交付",
                status=(
                    "completed"
                    if evidence and not leader_review.block_generation
                    else "degraded"
                ),
                model_rounds=0,
                tool_rounds=0,
                input_count=len(evidence),
                output_count=len(evidence),
                warnings=[
                    "已按 Leader 写作合同交付精简证据包与增强素材；撰写层不得重新研究。"
                ],
                checkpoints=[
                    f"leader_decision={leader_review.loop.checkpoints[0]}",
                    f"handoff_evidence={len(evidence)}",
                    f"handoff_media={len(media_assets)}",
                    f"collection_warnings={len(warnings)}",
                ],
            ),
        ]
        minimum_items = url_result.plan.min_items
        critical_subject_mode = bool(
            matching_critical_entities(" ".join([request.subject, *request.focus]))
        )
        if critical_subject_mode and any(
            item.id.startswith("critical-") for item in evidence
        ):
            minimum_items = 1
        if leader_review.block_generation:
            raise EvidenceCollectionError(
                "Leader 监督层判定资料覆盖坍缩为关键人物护栏；"
                "已停止生成普通主题报告，避免输出偏题或误导内容。"
            )
        if len(evidence) < minimum_items:
            raise EvidenceCollectionError(
                "四层资料流水线没有取得可用资料；请换一个更具体的英文球队、球员或赛事主题"
            )
        return ResearchBundle(
            evidence=evidence,
            warnings=warnings,
            plan=url_result.plan,
            source_attempts=source_attempts,
            layer_runs=layer_runs,
            media_assets=media_assets,
            editorial_plan=leader_review.editorial_plan,
            tool_rounds_used=sum(item.tool_rounds for item in layer_runs),
        )

    async def _run_column_team_loop(
        self,
        request: ConsumerReportRequest,
        column: EditorialColumnPlan,
        *,
        seed_evidence: list[Evidence],
        max_items: int,
    ) -> ColumnTeamLoopResult:
        seed_by_id = {item.id: item for item in seed_evidence}
        column_seed = [
            seed_by_id[item] for item in column.evidence_ids if item in seed_by_id
        ]
        all_evidence: list[Evidence] = list(column_seed)
        all_candidates: list[Evidence] = list(column_seed)
        media_assets: list[MediaAsset] = []
        warnings: list[str] = []
        source_attempts: dict[str, str] = {}
        model_rounds = 0
        tool_rounds = 0
        iterations = 0
        stagnant_rounds = 0
        max_iterations = min(
            max(column.search_iterations, 2), COLUMN_TEAM_MAX_ITERATIONS
        )

        for iteration in range(max_iterations):
            iterations += 1
            before_evidence = {item.id for item in all_evidence}
            before_media = {str(item.url) for item in media_assets}
            column_request = self._column_team_request(request, column, iteration)
            url_result = await self._url_layer.collect(
                column_request, max_items=max(max_items * 2, 12)
            )
            model_rounds += url_result.loop.model_rounds
            tool_rounds += url_result.loop.tool_rounds
            source_attempts.update(
                {
                    f"{column.column_id}:{key}": value
                    for key, value in url_result.source_attempts.items()
                }
            )
            warnings.extend(url_result.warnings)
            all_candidates = _dedupe_evidence(
                [*all_candidates, *url_result.candidates],
                max_items=max(max_items * 4, 24),
            )
            refinement = await self._refinement_layer.refine(
                column_request, all_candidates, max_items=max_items
            )
            model_rounds += refinement.loop.model_rounds
            tool_rounds += refinement.loop.tool_rounds
            warnings.extend(refinement.warnings)
            enhancement = await self._enhancement_layer.enhance(
                column_request, refinement.evidence
            )
            model_rounds += enhancement.loop.model_rounds
            tool_rounds += enhancement.loop.tool_rounds
            warnings.extend(enhancement.warnings)
            all_evidence = self._merge_evidence(
                column_request,
                [*all_evidence, *refinement.evidence],
                enhancement.additional_evidence,
                max_items=max_items,
            )
            media_assets = EnhancementHarness._dedupe_assets(
                [*media_assets, *enhancement.media_assets]
            )
            new_evidence = {item.id for item in all_evidence} - before_evidence
            new_media = {str(item.url) for item in media_assets} - before_media
            coverage_gaps = self._column_contract_gaps(
                column, all_evidence, media_assets
            )
            warnings.extend(f"{column.title} 缺口：{gap}" for gap in coverage_gaps)
            if not coverage_gaps:
                break
            if not new_evidence and not new_media:
                stagnant_rounds += 1
            if stagnant_rounds >= 2:
                warnings.append(f"{column.title} 小组连续两轮没有新增交付，已停止。")
                break

        updated_column = column.model_copy(
            update={
                "evidence_ids": [item.id for item in all_evidence[:12]],
                "search_iterations": iterations,
            }
        )
        final_gaps = self._column_contract_gaps(
            updated_column, all_evidence, media_assets
        )
        status: LayerStatus = "completed" if not final_gaps else "degraded"
        return ColumnTeamLoopResult(
            column=updated_column,
            evidence=all_evidence,
            media_assets=media_assets,
            warnings=list(dict.fromkeys(warnings))[:12],
            source_attempts=source_attempts,
            loop=LayerLoopSummary(
                name="column_team_loop",
                label=f"小组循环：{column.title}",
                status=status,
                model_rounds=min(model_rounds, 20),
                tool_rounds=min(tool_rounds, 20),
                input_count=len(column_seed),
                output_count=len(all_evidence) + len(media_assets),
                warnings=list(dict.fromkeys(warnings))[:8],
                checkpoints=[
                    f"column={column.column_id}",
                    f"group={column.specialist_group}",
                    f"iterations={iterations}/{max_iterations}",
                    f"evidence={len(all_evidence)}",
                    f"media={len(media_assets)}",
                    f"coverage={status}",
                    f"gaps={len(final_gaps)}",
                ],
            ),
        )

    @staticmethod
    def _column_team_request(
        request: ConsumerReportRequest, column: EditorialColumnPlan, iteration: int
    ) -> ConsumerReportRequest:
        group_terms = {
            "match_report": "match report goals highlights scorer minute venue",
            "transfer_intel": (
                "transfer fee agreement current club target club season stats"
            ),
            "coach_tactics": "manager coach tactics achievements future",
            "player_profile": "player profile position club season statistics image",
            "off_field": "fans broadcast politics schedule city impact",
            "context": "football background explain context",
        }
        focus = list(
            dict.fromkeys(
                [
                    *request.focus,
                    column.title,
                    column.specialist_group,
                    *column.enrichment_targets,
                    *column.media_targets,
                    *column.coverage_requirements,
                ]
            )
        )
        retry_hint = (
            "broaden search second source official image"
            if iteration
            else "primary sources latest"
        )
        subject = truncate_text(
            " ".join(
                [
                    request.subject,
                    column.title,
                    group_terms.get(column.specialist_group, ""),
                    retry_hint,
                ]
            ),
            130,
        )
        return request.model_copy(update={"subject": subject, "focus": focus[:8]})

    @staticmethod
    def _column_coverage_ok(
        column: EditorialColumnPlan,
        evidence: list[Evidence],
        media_assets: list[MediaAsset],
    ) -> bool:
        return not ResearchHarness._column_contract_gaps(
            column, evidence, media_assets
        )

    @staticmethod
    def _column_contract_gaps(
        column: EditorialColumnPlan,
        evidence: list[Evidence],
        media_assets: list[MediaAsset],
    ) -> list[str]:
        if not evidence:
            return ["没有可用证据"]
        text = " ".join(f"{item.title} {item.summary}" for item in evidence).casefold()
        gaps: list[str] = []
        if column.specialist_group == "match_report":
            if not completed_match_items(evidence):
                gaps.append(
                    "match_report lacks completed-match evidence; preview, arrival, "
                    "kickoff, hotel, and schedule items must be off_field/context"
                )
            elif not re.search(
                r"goal|scored|score|beat|defeat|var|penalty|highlights?|\d{1,2}-\d{1,2}",
                text,
            ):
                gaps.append("缺少比分、结果或关键比赛事件")
            if column.media_targets and not media_assets:
                gaps.append("缺少官方高光、战报图或人工补图交付")
            return gaps
        if column.specialist_group == "transfer_intel":
            if not re.search(
                r"transfer|signing|signs|signed|bid|deal|fee|club|agreement|medical",
                text,
            ):
                gaps.append("缺少转会阶段或俱乐部关系")
            if not re.search(r"current club|target club|from|to|club|球队|目标", text):
                gaps.append("缺少当前球队或目标球队字段")
            return gaps
        if column.specialist_group == "coach_tactics":
            if not re.search(
                r"coach|manager|tactic|tuchel|nagelsmann|教练|主帅|战术",
                text,
            ):
                gaps.append("缺少教练、战术或履历证据")
            return gaps
        if column.specialist_group == "player_profile":
            if not re.search(
                r"position|club|season|goals?|assists?|位置|俱乐部|赛季|进球|助攻",
                text,
            ):
                gaps.append("缺少球员位置、俱乐部或赛季数据证据")
            return gaps
        return gaps

    @staticmethod
    def _merge_column_team_evidence(
        request: ConsumerReportRequest,
        seed_evidence: list[Evidence],
        team_results: list[ColumnTeamLoopResult],
        *,
        max_items: int,
    ) -> list[Evidence]:
        required_structured = [
            item
            for item in seed_evidence
            if item.evidence_kind == "structured"
            and item.source_id == "football-data-org"
        ]
        prioritized: list[Evidence] = []
        for result in sorted(team_results, key=lambda item: item.column.priority):
            prioritized.extend(result.evidence[:5])
        merged = finish_evidence_selection(
            request, [*prioritized, *seed_evidence], max_items=max_items
        )
        if required_structured:
            required_ids = {item.id for item in required_structured}
            merged = _dedupe_evidence(
                [
                    *required_structured,
                    *[item for item in merged if item.id not in required_ids],
                ],
                max_items=max_items,
            )
        known = {item.id for item in merged}
        for item in prioritized:
            if item.id in known:
                continue
            if len(merged) >= max_items:
                break
            merged.append(item)
            known.add(item.id)
        return _dedupe_evidence(merged, max_items=max_items)

    @staticmethod
    def _merge_evidence(
        request: ConsumerReportRequest,
        refined: list[Evidence],
        additional: list[Evidence],
        *,
        max_items: int,
    ) -> list[Evidence]:
        merged = finish_evidence_selection(
            request, [*refined, *additional], max_items=max_items
        )
        known = {item.id for item in merged}
        for item in additional:
            if item.id in known:
                continue
            if len(merged) < max_items:
                merged.append(item)
                known.add(item.id)
                continue
            discovery_index = next(
                (
                    index
                    for index, existing in enumerate(merged)
                    if existing.verification_status == "unverified_lead"
                ),
                None,
            )
            if discovery_index is not None:
                merged[discovery_index] = item
                known.add(item.id)
        return _dedupe_evidence(merged, max_items=max_items)

    @staticmethod
    def _warnings(
        plan: ResearchPlan,
        evidence: list[Evidence],
        attempts: dict[str, str],
        layer_warnings: list[str],
    ) -> list[str]:
        warnings: list[str] = []
        warnings.extend(ResearchHarness._reader_warnings(layer_warnings))
        verified = [
            item for item in evidence if item.verification_status != "unverified_lead"
        ]
        if not verified and evidence:
            warnings.append(
                "本次只找到发现层线索，没有足够的正文级核验来源；报告会按传闻/线索处理。"
            )
        if datetime.now(UTC).year >= 2026 and len(evidence) < 3:
            warnings.append("资料覆盖偏薄，发布前建议人工补充官方或第二独立来源。")
        return list(dict.fromkeys(warnings))[:12]

    @staticmethod
    def _reader_warnings(layer_warnings: list[str]) -> list[str]:
        reader: list[str] = []
        internal_patterns = (
            "资料精简层模型不可用",
            "资料精简层模型输出未通过结构化解析",
            "Leader 栏目规划模型输出未通过结构化解析",
            "增强层模型输出未通过结构化解析",
            "未能补到结构化球员资料",
            "未找到可自动附加的许可图片",
            "未找到可自动附加的官方视频",
            "关键画面需人工补充",
            "GIF/比赛动图",
            "俱乐部增强需要已授权球队资料接口",
            "增强层没有收到可用精简资料",
            "URL 收集层达到工具轮次预算",
            "连续两轮没有新增候选 URL",
            "mock 模式",
        )
        for warning in layer_warnings:
            if any(pattern in warning for pattern in internal_patterns):
                continue
        return reader
