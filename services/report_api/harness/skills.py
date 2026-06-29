from __future__ import annotations

import json
from pathlib import Path

from services.report_api.domain import ReportType
from services.report_api.harness.models import SkillDefinition


class SkillRegistry:
    def __init__(self, skills: list[SkillDefinition]) -> None:
        self._by_report_type = {skill.report_type: skill for skill in skills}
        missing = set(ReportType) - set(self._by_report_type)
        if missing:
            missing_names = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"missing skill definitions: {missing_names}")

    @classmethod
    def from_directory(cls, root: Path) -> SkillRegistry:
        skills = []
        for path in sorted(root.glob("*/runtime.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            skill_path = path.parent / "SKILL.md"
            if not skill_path.exists():
                raise ValueError(f"missing SKILL.md beside {path}")
            payload["instructions"] = skill_path.read_text(encoding="utf-8")
            skills.append(SkillDefinition.model_validate(payload))
        if not skills:
            raise ValueError(f"no skills found in {root}")
        return cls(skills)

    def for_report_type(self, report_type: ReportType) -> SkillDefinition:
        return self._by_report_type[report_type]

    def list(self) -> list[SkillDefinition]:
        return sorted(self._by_report_type.values(), key=lambda item: item.id)


def default_skill_registry() -> SkillRegistry:
    repository_root = Path(__file__).resolve().parents[3]
    return SkillRegistry.from_directory(repository_root / "agent_skills")
