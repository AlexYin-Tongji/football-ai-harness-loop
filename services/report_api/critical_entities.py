from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, HttpUrl

from services.report_api.domain import ConsumerReportRequest, Evidence


class CriticalEntity(BaseModel):
    id: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    preferred_name: str | None = None
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    critical_status: str
    event_date: datetime
    source_id: str
    source_name: str
    source_url: HttpUrl
    source_title: str
    summary: str
    report_summary: str | None = None
    editorial_rule: str


class CriticalEntityRegistry(BaseModel):
    version: str
    entities: list[CriticalEntity] = Field(default_factory=list)


def _registry_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "critical_entities.json"


@lru_cache(maxsize=1)
def load_critical_entities() -> CriticalEntityRegistry:
    path = _registry_path()
    if not path.exists():
        return CriticalEntityRegistry(version="missing", entities=[])
    return CriticalEntityRegistry.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )


def _contains_alias(text: str, aliases: list[str]) -> bool:
    folded = text.casefold()
    for alias in aliases:
        alias_folded = alias.casefold()
        if re.search(r"[\u4e00-\u9fff]", alias):
            if alias in text:
                return True
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(alias_folded)}(?![a-z0-9])", folded):
            return True
    return False


def matching_critical_entities(text: str) -> list[CriticalEntity]:
    registry = load_critical_entities()
    return [
        entity
        for entity in registry.entities
        if _contains_alias(text, [entity.canonical_name, *entity.aliases])
    ]


def expand_subject_with_critical_aliases(value: str) -> str:
    matches = matching_critical_entities(value)
    if not matches:
        return value
    aliases = []
    for entity in matches:
        aliases.extend([entity.canonical_name, *entity.aliases[:3]])
    return f"{value} {' '.join(dict.fromkeys(aliases))}"


def collect_critical_entity_evidence(
    request: ConsumerReportRequest,
    *,
    extra_text: str = "",
) -> list[Evidence]:
    text = " ".join([request.subject, *request.focus, extra_text])
    matches = matching_critical_entities(text)
    evidence: list[Evidence] = []
    for entity in matches:
        published_at = entity.event_date
        if published_at.tzinfo is None or published_at.utcoffset() is None:
            published_at = published_at.replace(tzinfo=UTC)
        evidence.append(
            Evidence(
                id=f"critical-{entity.id}",
                title=entity.source_title,
                url=entity.source_url,
                published_at=published_at,
                source_name=entity.source_name,
                summary=(
                    f"重大人物状态：{entity.canonical_name} 已被官方来源确认为"
                    f"{entity.critical_status}。{entity.summary} "
                    f"编辑规则：{entity.editorial_rule}"
                ),
                source_id=entity.source_id,
                trust_tier="S0",
                evidence_kind="official",
                verification_status="official",
                source_independence_key=entity.source_id,
            )
        )
    return evidence
