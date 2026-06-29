from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RetentionClass(StrEnum):
    TRANSIENT = "transient"
    OPERATIONAL = "operational"
    AUDIT = "audit"


class DataEntity(BaseModel):
    name: str
    purpose: str
    retention: RetentionClass
    contains_full_article: bool = False
    mutable_by: list[str] = Field(default_factory=list)


class AdminCatalog(BaseModel):
    generated_at: datetime
    layers: dict[str, list[DataEntity]]
    invariants: list[str]


def data_catalog(now: datetime) -> AdminCatalog:
    return AdminCatalog(
        generated_at=now,
        layers={
            "source": [
                DataEntity(
                    name="source_registry",
                    purpose="来源权限与信任等级",
                    retention="audit",
                    mutable_by=["source_admin"],
                ),
                DataEntity(
                    name="source_items",
                    purpose="标题、URL、时间和短摘录",
                    retention="operational",
                    mutable_by=["ingestion_worker"],
                ),
                DataEntity(
                    name="evidence",
                    purpose="归一化且可引用的证据",
                    retention="audit",
                    mutable_by=["evidence_reviewer"],
                ),
            ],
            "football": [
                DataEntity(
                    name="matches",
                    purpose="赛程、状态与赛果快照",
                    retention="audit",
                    mutable_by=["result_writer"],
                ),
                DataEntity(
                    name="teams_players",
                    purpose="球队与球员主数据",
                    retention="operational",
                    mutable_by=["data_steward"],
                ),
            ],
            "editorial": [
                DataEntity(
                    name="story_clusters",
                    purpose="转载去重和事件演进",
                    retention="operational",
                    mutable_by=["workflow"],
                ),
                DataEntity(
                    name="reports_versions",
                    purpose="报告版本、引用与更正链",
                    retention="audit",
                    mutable_by=["editor"],
                ),
            ],
            "agent": [
                DataEntity(
                    name="workflow_runs_steps",
                    purpose="有界 Loop 检查点",
                    retention="operational",
                    mutable_by=["harness"],
                ),
                DataEntity(
                    name="model_opinions",
                    purpose="预测委员会结构化意见",
                    retention="transient",
                    mutable_by=["harness"],
                ),
                DataEntity(
                    name="prediction_snapshots",
                    purpose="截止时点概率与赛后校准",
                    retention="audit",
                    mutable_by=["prediction_writer"],
                ),
            ],
            "governance": [
                DataEntity(
                    name="connector_health",
                    purpose="数据源可用性与限流",
                    retention="operational",
                    mutable_by=["monitor"],
                ),
                DataEntity(
                    name="audit_logs",
                    purpose="权限敏感操作审计",
                    retention="audit",
                    mutable_by=["audit_writer"],
                ),
            ],
        },
        invariants=[
            "LLM 对话历史不是业务事实库",
            "不存储完整新闻正文、密钥或隐藏思维链",
            "报告、更正、赛果和模型配置写入需要显式权限",
            "每个事实引用 evidence_id 并可追溯到 URL 与截止时间",
        ],
    )
