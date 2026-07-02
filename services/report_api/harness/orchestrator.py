from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from services.report_api.domain import ReportRequest
from services.report_api.harness.memory import InMemoryRunMemory
from services.report_api.harness.models import (
    HarnessRunResponse,
    HarnessStep,
    HarnessTrace,
    RunStatus,
    StepStatus,
)
from services.report_api.harness.skills import SkillRegistry
from services.report_api.service import ReportService


class ReportHarness:
    """Explicit workflow controller around the non-deterministic model call."""

    def __init__(
        self,
        report_service: ReportService,
        skill_registry: SkillRegistry,
        memory: InMemoryRunMemory,
    ) -> None:
        self._report_service = report_service
        self._skills = skill_registry
        self._memory = memory

    async def run(
        self,
        request: ReportRequest,
        tool_rounds_used: int = 0,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> HarnessRunResponse:
        skill = self._skills.for_report_type(request.report_type)
        now = datetime.now(UTC)
        trace = HarnessTrace(
            run_id=str(uuid4()),
            report_type=request.report_type,
            skill_id=skill.id,
            skill_version=skill.version,
            status=RunStatus.RUNNING,
            phase="route",
            max_model_rounds=skill.max_model_rounds,
            max_tool_rounds=skill.max_tool_rounds,
            evidence_count=len(request.evidence),
            tool_rounds_used=tool_rounds_used,
            created_at=now,
        )
        self._memory.put(trace)

        try:
            self._complete_step(
                trace,
                name="route",
                label="选择任务 Skill",
                detail=f"{skill.id}@{skill.version}",
            )
            self._complete_step(
                trace,
                name="context",
                label="构建时间点上下文",
                detail=f"已装载 {len(request.evidence)} 条带时间证据",
            )

            trace.phase = "generate"
            self._memory.put(trace)
            generation_started = datetime.now(UTC)
            started = perf_counter()
            report = await self._report_service.generate(
                request,
                max_attempts=min(2, skill.max_model_rounds),
                skill_instructions=skill.instructions,
                progress_callback=progress_callback,
            )
            trace.model_rounds_used = report.attempts
            self._append_step(
                trace,
                HarnessStep(
                    name="generate",
                    label="生成结构化报告",
                    status=StepStatus.COMPLETED,
                    detail=(
                        f"{report.provider}/{report.model}，"
                        f"使用 {report.attempts} 个模型轮次"
                    ),
                    started_at=generation_started,
                    completed_at=datetime.now(UTC),
                    duration_ms=max(0, int((perf_counter() - started) * 1000)),
                ),
            )
            self._complete_step(
                trace,
                name="quality_gate",
                label="通过确定性质量门",
                detail="引用、时点、schema 与概率规则已验证",
            )
            if progress_callback:
                progress_callback("quality_gate", 94)
            self._complete_step(
                trace,
                name="checkpoint",
                label="写入运行检查点",
                detail="仅保存报告、版本与安全运行摘要",
            )
            trace.status = RunStatus.COMPLETED
            trace.phase = "completed"
            trace.completed_at = datetime.now(UTC)
            self._memory.put(trace)
            return HarnessRunResponse(
                run=trace, report=report, evidence=request.evidence
            )
        except Exception as exc:
            self._failed_step(trace, exc)
            raise

    def _complete_step(
        self, trace: HarnessTrace, name: str, label: str, detail: str
    ) -> None:
        timestamp = datetime.now(UTC)
        self._append_step(
            trace,
            HarnessStep(
                name=name,
                label=label,
                status=StepStatus.COMPLETED,
                detail=detail,
                started_at=timestamp,
                completed_at=timestamp,
                duration_ms=0,
            ),
        )

    def _append_step(self, trace: HarnessTrace, step: HarnessStep) -> None:
        trace.steps.append(step)
        trace.phase = step.name
        self._memory.put(trace)

    def _failed_step(self, trace: HarnessTrace, exc: Exception) -> None:
        timestamp = datetime.now(UTC)
        safe_error = self._safe_error_name(exc)
        trace.steps.append(
            HarnessStep(
                name=trace.phase,
                label="任务安全停止",
                status=StepStatus.FAILED,
                detail=safe_error,
                started_at=timestamp,
                completed_at=timestamp,
                duration_ms=0,
            )
        )
        trace.status = RunStatus.FAILED
        trace.phase = "failed"
        trace.completed_at = timestamp
        self._memory.put(trace)

    @staticmethod
    def _safe_error_name(exc: Exception) -> str:
        allowed = {
            "ReportGenerationError": "报告未通过质量门",
            "LLMProviderError": "模型服务暂时不可用",
        }
        return allowed.get(exc.__class__.__name__, "任务执行失败")
