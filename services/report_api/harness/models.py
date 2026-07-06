from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from services.report_api.domain import Evidence, ReportResponse, ReportType


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StepStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class SkillDefinition(BaseModel):
    id: str
    version: str
    report_type: ReportType
    max_model_rounds: int = Field(ge=1, le=20)
    max_tool_rounds: int = Field(ge=0, le=20)
    phases: list[str] = Field(min_length=1)
    mcp_servers: list[str]
    memory_read: list[str]
    memory_write: list[str]
    quality_gates: list[str] = Field(min_length=1)
    instructions: str = Field(min_length=1, exclude=True)


class HarnessStep(BaseModel):
    name: str
    label: str
    status: StepStatus
    detail: str
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)


class HarnessTrace(BaseModel):
    run_id: str
    report_type: ReportType
    skill_id: str
    skill_version: str
    status: RunStatus
    phase: str
    max_model_rounds: int
    model_rounds_used: int = 0
    max_tool_rounds: int
    tool_rounds_used: int = 0
    evidence_count: int
    steps: list[HarnessStep] = Field(default_factory=list)
    created_at: datetime
    completed_at: datetime | None = None


class HarnessRunResponse(BaseModel):
    run: HarnessTrace
    report: ReportResponse
    evidence: list[Evidence] = Field(default_factory=list)


class SkillCapability(BaseModel):
    id: str
    version: str
    report_type: ReportType
    max_model_rounds: int
    max_tool_rounds: int
    phases: list[str]


class MCPServerCapability(BaseModel):
    id: str
    status: str
    read_only: bool
    configured: bool = True
    required_env: list[str] = Field(default_factory=list)
    production_status: str | None = None
    notes: str | None = None


class SystemCapabilities(BaseModel):
    provider: str
    model: str
    skills: list[SkillCapability]
    mcp_servers: list[MCPServerCapability]
    privacy: list[str]
