from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

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
        output = {"concise": 1800, "standard": 2600, "deep": 3200}[length]
        return ModelStagePolicy(
            stage=stage,
            thinking_enabled=False,
            max_output_tokens=min(configured_output_tokens, output),
            max_input_chars=18_000,
        )
    if stage == "daily_stable_final":
        output = {"concise": 1400, "standard": 2200, "deep": 2600}[length]
        return ModelStagePolicy(
            stage=stage,
            thinking_enabled=False,
            max_output_tokens=min(configured_output_tokens, output),
            max_input_chars=14_000,
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
            max_output_tokens=min(configured_output_tokens, 3000),
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
        "证据边界、人工复核提示和不自动发布边界；不得新增证据外事实。"
    )


def _clip_json_payload(
    payload: dict, *, raw_evidence: list[Evidence], max_chars: int
) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    payload = dict(payload)
    payload["evidence_index"] = evidence_index(raw_evidence, summary_chars=90)
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
        "length": request.length.value,
        "focus": request.focus,
        "evidence_index": evidence_index(request.evidence),
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
