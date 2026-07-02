from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field, ValidationError

from services.mcp_servers.common import get_json
from services.report_api.domain import (
    ConsumerReportRequest,
    Evidence,
    MediaAsset,
    ReportType,
)
from services.report_api.evidence import (
    EvidenceCollectionError,
    collect_bbc_evidence,
    collect_gdelt_evidence,
    collect_guardian_evidence,
    collect_newsapi_evidence,
    finish_evidence_selection,
)
from services.report_api.media import (
    search_commons_player_image,
    search_official_youtube_video,
)
from services.report_api.model_control import evidence_index, truncate_text
from services.report_api.providers.base import LLMProvider, LLMProviderError, LLMRequest

ResearchSource = Literal["rss", "gdelt", "newsapi"]
LayerName = Literal[
    "url_collection",
    "evidence_refinement",
    "enhancement",
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


class LayerLoopSummary(BaseModel):
    name: LayerName
    label: str
    status: LayerStatus
    model_rounds: int = Field(default=0, ge=0, le=4)
    tool_rounds: int = Field(default=0, ge=0, le=20)
    input_count: int = Field(default=0, ge=0)
    output_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=8)


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


@dataclass(frozen=True)
class ResearchBundle:
    evidence: list[Evidence]
    warnings: list[str]
    plan: ResearchPlan
    source_attempts: dict[str, str]
    layer_runs: list[LayerLoopSummary] = field(default_factory=list)
    media_assets: list[MediaAsset] = field(default_factory=list)
    tool_rounds_used: int = 0


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


def fallback_research_plan(request: ConsumerReportRequest) -> ResearchPlan:
    focus = " ".join(request.focus)
    seed = " ".join(_english_terms(f"{request.subject} {focus}")).strip()
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
                query=f"{seed} team news injuries lineup preview",
                purpose="prediction_context",
                sources=["rss", "gdelt", "newsapi"],
            ),
            ResearchQuery(
                query=f"{seed} Opta Stats Perform prediction probability",
                purpose="external_prediction",
                sources=["gdelt", "newsapi"],
            ),
            ResearchQuery(
                query=f"{request.subject} football tactical preview",
                purpose="prediction_context",
                sources=["gdelt", "newsapi"],
            ),
        ]
    elif request.report_type == ReportType.TRANSFER_DAILY:
        queries = [
            ResearchQuery(
                query=f"{seed} transfer talks bid agreement medical",
                purpose="transfer_market",
                sources=["rss", "gdelt", "newsapi"],
            ),
            ResearchQuery(
                query="football transfer news today bid talks agreement",
                purpose="transfer_market",
                sources=["gdelt", "newsapi"],
            ),
        ]
    else:
        queries = [
            ResearchQuery(
                query=f"{seed} match news fixtures results injury",
                purpose="match_news",
                sources=["rss", "gdelt", "newsapi"],
            ),
            ResearchQuery(
                query=f"{seed} transfer news signing talks",
                purpose="transfer_market",
                sources=["rss", "gdelt", "newsapi"],
            ),
            ResearchQuery(
                query="FIFA World Cup football news transfer team news today",
                purpose="match_news",
                sources=["gdelt", "newsapi"],
            ),
        ]
    return ResearchPlan(queries=queries, min_items=1, allow_discovery_only=True)


def _dedupe_evidence(items: list[Evidence], *, max_items: int) -> list[Evidence]:
    deduped: list[Evidence] = []
    seen: set[str] = set()
    for item in items:
        key = str(item.url).split("#", 1)[0]
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
                                "不写事实。查询词优先英文；只能使用 rss、gdelt、"
                                "newsapi 三类已登记发现源；不要加入社媒、任意网页"
                                "抓取或未登记来源。输出 JSON，"
                                f"Schema: {json.dumps(schema, ensure_ascii=False)}"
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
            return fallback
        return plan if plan.queries else fallback

    async def collect(
        self, request: ConsumerReportRequest, *, max_items: int
    ) -> UrlCollectionResult:
        plan = await self.plan(request)
        warnings: list[str] = []
        combined: list[Evidence] = []
        attempts: dict[str, str] = {}
        tool_rounds = 0
        empty_rounds = 0
        candidate_limit = max(max_items * 4, 24)

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
            )
            tool_rounds += used
            if len(combined) == before:
                empty_rounds += 1
            else:
                empty_rounds = 0
            if empty_rounds >= 2:
                warnings.append("连续两轮没有新增候选 URL，URL 收集层提前停止。")
                break

        candidates = _dedupe_evidence(combined, max_items=candidate_limit)
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
    ) -> int:
        query_request = request.model_copy(update={"subject": query.query})
        source_limit = max(4, min(12, max_items // 2))
        sources = query.sources or ["rss", "gdelt", "newsapi"]
        tasks: list[tuple[str, object]] = []
        if "rss" in sources:
            tasks.extend(
                [
                    (
                        "guardian_rss",
                        collect_guardian_evidence(
                            query_request, max_items=source_limit
                        ),
                    ),
                    (
                        "bbc_rss",
                        collect_bbc_evidence(query_request, max_items=source_limit),
                    ),
                ]
            )
        if "gdelt" in sources:
            tasks.append(
                (
                    "gdelt",
                    collect_gdelt_evidence(query_request, max_items=source_limit),
                )
            )
        if "newsapi" in sources:
            tasks.append(
                (
                    "newsapi",
                    collect_newsapi_evidence(query_request, max_items=source_limit),
                )
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
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_output_tokens = max_output_tokens

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
                ),
            )

        selected = finish_evidence_selection(request, candidates, max_items=max_items)
        model_rounds = 0
        try:
            refined = await self._run_model_refinement(request, selected)
            model_rounds = 1
            evidence = self._apply_refinement(selected, refined)
            warnings = refined.warnings
            status: LayerStatus = "completed"
        except (LLMProviderError, ValidationError, ValueError):
            evidence = self._fallback_refinement(selected)
            warnings = ["资料精简层模型不可用，已使用确定性短摘要兜底。"]
            status = "degraded"

        return RefinementLayerResult(
            evidence=evidence,
            warnings=warnings,
            loop=LayerLoopSummary(
                name="evidence_refinement",
                label="第二层：资料精简提炼",
                status=status,
                model_rounds=model_rounds,
                tool_rounds=0,
                input_count=len(selected),
                output_count=len(evidence),
                warnings=warnings[:8],
            ),
        )

    async def _run_model_refinement(
        self, request: ConsumerReportRequest, selected: list[Evidence]
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
                            "每条输出必须引用 source_evidence_id；保留不确定性、传闻"
                            "标签、比分/金额/时间等关键数字，删除重复和空话。"
                            "输出 JSON，"
                            f"Schema: {json.dumps(schema, ensure_ascii=False)}"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "report_type": request.report_type.value,
                                "subject": request.subject,
                                "candidates": candidates,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                thinking_enabled=False,
                max_output_tokens=min(self._max_output_tokens, 2200),
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
            output.append(
                original.model_copy(
                    update={
                        "title": truncate_text(item.title, 240),
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
                update={"summary": truncate_text(f"精简提炼：{item.summary}", 1400)}
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
        max_tool_rounds: int = 6,
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
        except (LLMProviderError, ValidationError, ValueError):
            plan = self._fallback_plan(request, evidence)
        additional: list[Evidence] = []
        assets: list[MediaAsset] = []
        warnings = list(plan.warnings)
        tool_rounds = 0

        for need in sorted(plan.needs, key=lambda item: item.priority)[:8]:
            if tool_rounds >= self._max_tool_rounds:
                warnings.append("增强层达到工具轮次预算，已停止补采。")
                break
            if need.kind == "gif":
                warnings.append(
                    "GIF/比赛动图当前没有批准来源，已列为人工补充项，不自动抓取。"
                )
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
                max_output_tokens=min(self._max_output_tokens, 1400),
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
        return EnhancementPlan(needs=needs)

    async def _collect_commons_image(self, need: EnhancementNeed) -> MediaAsset | None:
        if not self._media_enabled:
            return None
        try:
            return await search_commons_player_image(need.target)
        except Exception:
            return None

    async def _collect_official_video(self, need: EnhancementNeed) -> MediaAsset | None:
        if not (
            self._media_enabled and self._youtube_api_key and self._youtube_channel_ids
        ):
            return None
        try:
            return await search_official_youtube_video(
                need.target,
                self._youtube_api_key,
                list(self._youtube_channel_ids),
            )
        except Exception:
            return None

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
        root = Path(__file__).resolve().parents[2]
        path = root / "agent_skills" / "research-enhancement" / "SKILL.md"
        if not path.exists():
            return "增强层只允许调用 Source Registry 已登记的只读资料和许可媒体工具。"
        return path.read_text(encoding="utf-8")

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
        )
        self._enhancement_layer = EnhancementHarness(
            provider,
            model=model,
            max_output_tokens=max_output_tokens,
            media_enabled=media_enabled,
            youtube_api_key=youtube_api_key,
            youtube_channel_ids=youtube_channel_ids,
        )

    async def plan(self, request: ConsumerReportRequest) -> ResearchPlan:
        return await self._url_layer.plan(request)

    async def collect(
        self,
        request: ConsumerReportRequest,
        *,
        max_items: int,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> ResearchBundle:
        url_result = await self._url_layer.collect(
            request, max_items=max(max_items * 2, 12)
        )
        if progress_callback:
            progress_callback("url_collection", 18)

        refinement = await self._refinement_layer.refine(
            request, url_result.candidates, max_items=max_items
        )
        if progress_callback:
            progress_callback("evidence_refinement", 34)

        enhancement = await self._enhancement_layer.enhance(
            request, refinement.evidence
        )
        if progress_callback:
            progress_callback("enhancement", 42)

        evidence = self._merge_evidence(
            request,
            refinement.evidence,
            enhancement.additional_evidence,
            max_items=max_items,
        )
        warnings = self._warnings(
            url_result.plan,
            evidence,
            url_result.source_attempts,
            [
                *url_result.warnings,
                *refinement.warnings,
                *enhancement.warnings,
            ],
        )
        layer_runs = [
            url_result.loop,
            refinement.loop,
            enhancement.loop,
            LayerLoopSummary(
                name="writing_handoff",
                label="第四层：撰写层交付",
                status="completed" if evidence else "degraded",
                model_rounds=0,
                tool_rounds=0,
                input_count=len(evidence),
                output_count=len(evidence),
                warnings=[
                    "已向撰写层交付精简证据包与增强素材；撰写层不得重新研究。"
                ],
            ),
        ]
        if len(evidence) < url_result.plan.min_items:
            raise EvidenceCollectionError(
                "四层资料流水线没有取得可用资料；请换一个更具体的英文球队、球员或赛事主题"
            )
        return ResearchBundle(
            evidence=evidence,
            warnings=warnings,
            plan=url_result.plan,
            source_attempts=url_result.source_attempts,
            layer_runs=layer_runs,
            media_assets=enhancement.media_assets,
            tool_rounds_used=sum(item.tool_rounds for item in layer_runs),
        )

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
        warnings.extend(layer_warnings)
        verified = [
            item for item in evidence if item.verification_status != "unverified_lead"
        ]
        if not verified and evidence:
            warnings.append(
                "本次只找到发现层线索，没有足够的正文级核验来源；报告会按传闻/线索处理。"
            )
        failed = [key for key, value in attempts.items() if not value.startswith("ok")]
        if failed:
            warnings.append(
                "部分来源暂时不可用，已使用其他批准来源继续："
                + "、".join(sorted(failed)[:5])
            )
        if len(plan.queries) > 1:
            warnings.append(
                f"URL 收集层生成 {len(plan.queries)} 组检索词，"
                "后续层已做精简、增强和撰写交付。"
            )
        if datetime.now(UTC).year >= 2026 and len(evidence) < 3:
            warnings.append("资料覆盖偏薄，发布前建议人工补充官方或第二独立来源。")
        return list(dict.fromkeys(warnings))[:12]
