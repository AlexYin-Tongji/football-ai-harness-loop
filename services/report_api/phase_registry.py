from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from services.report_api.domain import ReportType


@dataclass(frozen=True)
class PhaseDefinition:
    id: str
    label: str
    progress: int
    description: str
    product_visible: bool = True


PHASES: dict[str, PhaseDefinition] = {
    "queued": PhaseDefinition("queued", "排队等待", 0, "任务已创建，等待运行名额。"),
    "waiting_for_capacity": PhaseDefinition(
        "waiting_for_capacity", "等待容量", 2, "后台正在等待可用研究席位。"
    ),
    "waiting_for_resume": PhaseDefinition(
        "waiting_for_resume", "等待恢复", 3, "服务重启后任务已重新进入恢复队列。"
    ),
    "collecting_sources": PhaseDefinition(
        "collecting_sources", "准备资料", 10, "启动今日资料收集流水线。"
    ),
    "url_collection": PhaseDefinition(
        "url_collection", "Seed 收集", 18, "按来源 Playbook 收集候选链接和元数据。"
    ),
    "evidence_refinement": PhaseDefinition(
        "evidence_refinement", "Seed 精简", 34, "把候选链接压缩成可回溯证据摘要。"
    ),
    "leader_review": PhaseDefinition(
        "leader_review", "Leader 分栏", 50, "规划或复审栏目、小组和交付覆盖。"
    ),
    "column_team_loop": PhaseDefinition(
        "column_team_loop", "小组循环", 48, "专栏小组各自收集、精简、增强和检查覆盖。"
    ),
    "evidence_ready": PhaseDefinition(
        "evidence_ready", "证据就绪", 54, "结构化证据包、媒体候选和栏目合同已交付。"
    ),
    "research_desks": PhaseDefinition(
        "research_desks", "专栏研究", 60, "各专栏小组生成研究简报和栏目草稿。"
    ),
    "desk_drafts_ready": PhaseDefinition(
        "desk_drafts_ready", "草稿就绪", 75, "专栏草稿已交给总编辑。"
    ),
    "prediction_council": PhaseDefinition(
        "prediction_council", "多席研判", 60, "独立分析席位正在生成赛前意见。"
    ),
    "prediction_council_ready": PhaseDefinition(
        "prediction_council_ready", "研判就绪", 72, "预测席位意见已交给终审。"
    ),
    "editor_synthesis": PhaseDefinition(
        "editor_synthesis", "覆盖合稿", 82, "总编辑按栏目合同去重、排序和补齐覆盖。"
    ),
    "claim_repair": PhaseDefinition(
        "claim_repair",
        "质量修复",
        86,
        "合稿未过 claim 校验时，按证据账本修复或进入保守交付。",
    ),
    "deterministic_finalizer": PhaseDefinition(
        "deterministic_finalizer",
        "保守合稿",
        88,
        "模型合稿失败后使用已验证草稿保守收敛。",
    ),
    "licensed_media": PhaseDefinition(
        "licensed_media", "媒体挂载", 90, "筛选并挂载许可图片或官方视频缩略图。"
    ),
    "quality_gate": PhaseDefinition(
        "quality_gate", "声明校验", 94, "检查引用、传闻标签、事件字段和 claim 证据。"
    ),
    "completed": PhaseDefinition("completed", "生成完成", 100, "报告已生成并保存。"),
    "failed": PhaseDefinition("failed", "安全停止", 100, "任务已停止并保留失败原因。"),
    "interrupted": PhaseDefinition(
        "interrupted", "运行中断", 100, "服务重启中断了任务，需要重新生成。"
    ),
}


REPORT_PHASE_ORDER: dict[ReportType, list[str]] = {
    ReportType.DAILY_FOOTBALL_DIGEST: [
        "url_collection",
        "evidence_refinement",
        "leader_review",
        "column_team_loop",
        "research_desks",
        "editor_synthesis",
        "claim_repair",
        "quality_gate",
    ],
    ReportType.WORLD_CUP_DAILY: [
        "url_collection",
        "evidence_refinement",
        "leader_review",
        "column_team_loop",
        "research_desks",
        "editor_synthesis",
        "claim_repair",
        "quality_gate",
    ],
    ReportType.TRANSFER_DAILY: [
        "url_collection",
        "evidence_refinement",
        "leader_review",
        "column_team_loop",
        "research_desks",
        "editor_synthesis",
        "claim_repair",
        "quality_gate",
    ],
    ReportType.MATCH_PREDICTION: [
        "url_collection",
        "evidence_refinement",
        "leader_review",
        "prediction_council",
        "editor_synthesis",
        "quality_gate",
    ],
}


class PhaseView(BaseModel):
    id: str
    label: str
    progress: int = Field(ge=0, le=100)
    description: str


def phase_definition(phase: str) -> PhaseDefinition:
    return PHASES.get(
        phase,
        PhaseDefinition(phase, phase.replace("_", " "), 0, "自定义运行阶段。"),
    )


def phase_progress(phase: str, fallback: int = 0) -> int:
    definition = PHASES.get(phase)
    return definition.progress if definition else fallback


def phase_label(phase: str) -> str:
    return phase_definition(phase).label


def phase_views(report_type: ReportType) -> list[PhaseView]:
    return [
        PhaseView(
            id=definition.id,
            label=definition.label,
            progress=definition.progress,
            description=definition.description,
        )
        for phase_id in REPORT_PHASE_ORDER.get(
            report_type, REPORT_PHASE_ORDER[ReportType.DAILY_FOOTBALL_DIGEST]
        )
        if (definition := PHASES.get(phase_id)) is not None
    ]
