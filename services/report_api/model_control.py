from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from services.report_api.claim_ledger import build_numeric_claim_ledger
from services.report_api.domain import DeskDraft, Evidence, ReportRequest

StageName = Literal[
    "daily_research",
    "daily_desk_write",
    "daily_final",
    "daily_stable_final",
    "prediction_opinion",
    "prediction_judge",
]


@dataclass(frozen=True)
class ModelStagePolicy:
    stage: StageName
    thinking_enabled: bool
    max_output_tokens: int
    max_input_chars: int


DAILY_FINAL_CONTRACT = {
    "title": "简体中文标题",
    "executive_summary": "10句以内中文摘要，明确事实、传闻和未知项",
    "sections": [
        {
            "heading": "栏目标题",
            "body": "中文正文；同一 story_cluster_id 不重复写；传闻必须显式标注",
            "evidence_ids": ["只能使用 evidence_index 中存在的 id"],
            "category": "match / transfer / off_field / context",
        }
    ],
    "warnings": ["覆盖不足、来源冲突、模型降级或发布前复核事项"],
    "prediction": None,
    "enrichment": {
        "player_spotlights": [],
        "match_timeline": [],
        "media_assets": [],
    },
}


PREDICTION_ANALYSIS_CONTRACT = (
    "prediction.analysis_process 必须写 3-6 条中文分析步骤，每条都用 evidence_ids "
    "支撑；先解释输入与不确定性，再解释概率如何形成。"
)


def stage_policy(
    stage: StageName,
    *,
    configured_output_tokens: int,
    length: str = "standard",
) -> ModelStagePolicy:
    if stage == "daily_final":
        output = {"concise": 4200, "standard": 6000, "deep": 8000}[length]
        return ModelStagePolicy(
            stage=stage,
            thinking_enabled=False,
            max_output_tokens=min(configured_output_tokens, output),
            max_input_chars=28_000,
        )
    if stage == "daily_stable_final":
        output = {"concise": 3200, "standard": 5000, "deep": 6500}[length]
        return ModelStagePolicy(
            stage=stage,
            thinking_enabled=False,
            max_output_tokens=min(configured_output_tokens, output),
            max_input_chars=24_000,
        )
    if stage == "prediction_judge":
        return ModelStagePolicy(
            stage=stage,
            thinking_enabled=True,
            max_output_tokens=min(configured_output_tokens, 4500),
            max_input_chars=32_000,
        )
    if stage == "daily_desk_write":
        return ModelStagePolicy(
            stage=stage,
            thinking_enabled=True,
            max_output_tokens=min(configured_output_tokens, 5000),
            max_input_chars=28_000,
        )
    if stage == "prediction_opinion":
        return ModelStagePolicy(
            stage=stage,
            thinking_enabled=True,
            max_output_tokens=min(configured_output_tokens, 1800),
            max_input_chars=20_000,
        )
    return ModelStagePolicy(
        stage=stage,
        thinking_enabled=True,
        max_output_tokens=min(configured_output_tokens, 2200),
        max_input_chars=30_000,
    )


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def evidence_index(evidence: list[Evidence], *, summary_chars: int = 180) -> list[dict]:
    return [
        {
            "id": item.id,
            "source": item.source_name,
            "source_id": item.source_id,
            "independence_key": item.source_independence_key or item.source_id,
            "status": item.verification_status,
            "kind": item.evidence_kind,
            "cluster": item.story_cluster_id,
            "published_at": item.published_at.isoformat(),
            "title": truncate_text(item.title, 140),
            "summary": truncate_text(item.summary, summary_chars),
        }
        for item in evidence
    ]


def compact_desk_drafts(desk_drafts: list[DeskDraft]) -> list[dict]:
    compacted = []
    for draft in desk_drafts:
        compacted.append(
            {
                "desk": draft.desk,
                "heading": truncate_text(draft.heading, 90),
                "summary": truncate_text(draft.summary, 500),
                "sections": [
                    {
                        "heading": truncate_text(section.heading, 90),
                        "body": truncate_text(section.body, 900),
                        "evidence_ids": section.evidence_ids[:8],
                    }
                    for section in draft.sections[:4]
                ],
                "warnings": [truncate_text(item, 180) for item in draft.warnings[:4]],
            }
        )
    return compacted


def final_skill_contract(skill_instructions: str | None) -> str:
    if not skill_instructions:
        return ""
    return (
        "本阶段只执行最终合稿，不重新研究。必须保留来源引用、传闻标签、"
        "证据边界和必要的人工复核提示；不得新增证据外事实。"
    )


def _clip_json_payload(
    payload: dict, *, raw_evidence: list[Evidence], max_chars: int
) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    payload = dict(payload)
    payload["evidence_index"] = evidence_index(raw_evidence, summary_chars=90)
    payload["numeric_claim_ledger"] = payload.get("numeric_claim_ledger", [])[:40]
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= max_chars:
        return text

    compacted_drafts = []
    for draft in payload.get("desk_drafts", []):
        compacted_drafts.append(
            {
                **draft,
                "summary": truncate_text(str(draft.get("summary", "")), 260),
                "sections": [
                    {
                        **section,
                        "body": truncate_text(str(section.get("body", "")), 360),
                    }
                    for section in draft.get("sections", [])[:3]
                    if isinstance(section, dict)
                ],
            }
        )
    payload["desk_drafts"] = compacted_drafts
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= max_chars:
        return text

    payload["evidence_index"] = [
        {
            "id": item.id,
            "source": item.source_name,
            "status": item.verification_status,
            "cluster": item.story_cluster_id,
            "title": truncate_text(item.title, 90),
        }
        for item in raw_evidence
    ]
    payload["numeric_claim_ledger"] = payload.get("numeric_claim_ledger", [])[:16]
    return json.dumps(payload, ensure_ascii=False)


def build_daily_final_messages(
    request: ReportRequest,
    *,
    desk_drafts: list[DeskDraft],
    outline_json: str,
    skill_instructions: str | None,
    max_input_chars: int,
) -> list[dict[str, str]]:
    payload = {
        "report_type": request.report_type.value,
        "subject": request.subject,
        "report_date": request.report_date.isoformat(),
        "data_cutoff": request.data_cutoff.isoformat(),
        "time_scope": (
            request.time_scope.model_dump(mode="json") if request.time_scope else None
        ),
        "length": request.length.value,
        "focus": request.focus,
        "previous_story_memory": request.previous_story_memory,
        "collection_warnings": request.collection_warnings,
        "leader_editorial_plan": [
            column.model_dump(mode="json") for column in request.editorial_plan
        ],
        "evidence_index": evidence_index(request.evidence),
        "numeric_claim_ledger": build_numeric_claim_ledger(request.evidence),
        "Harness 生成的确定性合稿提纲": outline_json,
        "desk_drafts": compact_desk_drafts(desk_drafts),
    }
    payload_text = _clip_json_payload(
        payload, raw_evidence=request.evidence, max_chars=max_input_chars
    )
    return [
        {
            "role": "system",
            "content": (
                "你是《今日球脉》最终总编辑。你只整理已经完成的分桌草稿和"
                "证据索引，不重新研究，不扩写证据外事实。输出严格 JSON，"
                "不要 Markdown 代码围栏。正文必须是简体中文，evidence_id "
                "只能出现在 evidence_ids 数组里，不能写进可见正文。"
                "report_date 是北京时间自然日；如果 payload 含 time_scope，"
                "所有“今天/昨日/明日/近期”判断必须服从 time_scope.local_window_label、"
                "window_start_utc、window_end_utc 和 data_cutoff_utc，"
                "不得按服务器日期、英美发布时间或模型常识自行换日。"
                "写作要像给球迷和内容创作者的一份清楚报告：开头抓住今天"
                "真正变化，段落里交代球员/球队背景、比赛转折和下一步看点；"
                "最终日报必须按 3-5 个大栏目组织，但 sections 是读者看到的二级标题；"
                "战报栏目必须每场比赛单独一个二级标题，转会栏目必须每个重点转会"
                "单独一个二级标题，不要把多场比赛或多条转会揉成一段。"
                "栏目顺序和标题优先服从 leader_editorial_plan；总标题只概括"
                "最高优先级的主线栏目，不得把事实护栏或背景悼念写成标题钩子。"
                "栏目优先使用 category=match、transfer、off_field 或 context；"
                "每个二级标题正文写成 180-420 字的完整段落。"
                "比赛事件按“何时何地、谁对谁、谁在第几分钟用什么方式进球、"
                "比分如何变化、比赛过程和晋级/淘汰影响”写清楚，"
                "方便前端把对应图片或官方视频插在该事件下方；"
                "来源边界自然嵌入正文，不要把报告写成安全声明。"
                "previous_story_memory 只用于判断“相比上一版/昨日是否有变化”，"
                "不能单独当作事实来源；如果今天证据没有支持，不能复写成今天事实。"
                "必须区分已完赛证据和赛前证据：preview、arrival、kickoff、hotel、schedule、"
                "team news、hostile reception 只能写成赛前/场外/赛程内容，不得写成"
                "今日已开赛、已经完成或最终比分。"
                "质量门会逐句检查 claim：凡是比分、分钟、金额、年份、出场、"
                "进球、合同等数字，必须原样来自对应 evidence_ids 的证据摘要；"
                "优先从 numeric_claim_ledger 选择可写数字，若某个数字不在该小节"
                "引用的 evidence_ids 对应 ledger 中，就删除数字或改写成无数字表达；"
                "没有证据的球员履历、教练成绩和赛事数据宁可写未知。"
                "没有出现在证据索引或分桌草稿里的发布会、社交媒体、悼念方式、"
                "首发安排、赛程日期、主帅/队长表态和直接引语，一律不要写；"
                "证据里没有原文引语时只能转述，不得加引号。"
                "正文不要用“关键事件尚待确认、暂未明朗、比赛进程存疑、未知、待补充”"
                "来填充小节；这类缺口只放 warnings。"
                "unverified_lead 必须写成传闻、据报道、未核实、线索或尚未确认。"
                "同一 story_cluster_id 只写一次，保留重要分歧和未知项。"
                "输出契约如下："
                + json.dumps(DAILY_FINAL_CONTRACT, ensure_ascii=False)
                + final_skill_contract(skill_instructions)
            ),
        },
        {
            "role": "user",
            "content": payload_text,
        },
    ]


def message_chars(messages: list[dict[str, str]]) -> int:
    return sum(len(item.get("content", "")) for item in messages)
