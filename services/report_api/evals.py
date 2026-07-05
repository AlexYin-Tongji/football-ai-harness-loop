from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from pydantic import BaseModel, Field


class EvalFinding(BaseModel):
    check: str
    passed: bool
    detail: str


class EvalReport(BaseModel):
    name: str
    passed: bool
    score: float = Field(ge=0, le=1)
    findings: list[EvalFinding]


REQUIRED_DAILY_TRACE = {
    "research_url_collection",
    "research_evidence_refinement",
    "research_leader_review",
    "research_column_team_loop",
    "research_writing_handoff",
    "generate",
    "quality_gate",
}


def _visible_text(report: dict) -> str:
    sections = report.get("sections") or []
    return " ".join(
        [
            str(report.get("title") or ""),
            str(report.get("executive_summary") or ""),
            *[
                f"{section.get('heading') or ''} {section.get('body') or ''}"
                for section in sections
                if isinstance(section, dict)
            ],
        ]
    )


def _section_references(report: dict) -> set[str]:
    refs: set[str] = set()
    for section in report.get("sections") or []:
        if isinstance(section, dict):
            refs.update(str(item) for item in section.get("evidence_ids") or [])
    return refs


MINUTE_EVENT_RE = re.compile(
    r"\b\d{1,3}(?:\+\d{1,2})?(?:st|nd|rd|th)[-\s]?minute\b|"
    r"第\s*\d{1,3}(?:\+\d{1,2})?\s*分钟",
    re.I,
)
MATCH_EVENT_RE = re.compile(
    r"goal|scored|penalty|sent off|red card|VAR|进球|破门|点球|红牌|VAR",
    re.I,
)


def evaluate_daily_digest_result(
    payload: dict, *, name: str = "daily-digest"
) -> EvalReport:
    run = payload.get("run") or {}
    response = payload.get("report") or {}
    report = response.get("report") or {}
    evidence = payload.get("evidence") or []
    evidence_by_id = {
        str(item.get("id")): item for item in evidence if isinstance(item, dict)
    }
    sections = [item for item in report.get("sections") or [] if isinstance(item, dict)]
    findings: list[EvalFinding] = []

    trace_steps = {str(item.get("name")) for item in run.get("steps") or []}
    missing_trace = sorted(REQUIRED_DAILY_TRACE - trace_steps)
    findings.append(
        EvalFinding(
            check="trace.required_steps",
            passed=not missing_trace,
            detail="missing=" + ",".join(missing_trace) if missing_trace else "ok",
        )
    )

    refs = _section_references(report)
    unknown_refs = sorted(refs - set(evidence_by_id))
    findings.append(
        EvalFinding(
            check="citations.known_ids",
            passed=not unknown_refs and bool(refs),
            detail=(
                "unknown=" + ",".join(unknown_refs)
                if unknown_refs
                else f"refs={len(refs)}"
            ),
        )
    )

    text = _visible_text(report)
    leaked = [evidence_id for evidence_id in evidence_by_id if evidence_id in text]
    findings.append(
        EvalFinding(
            check="citations.no_visible_internal_ids",
            passed=not leaked,
            detail="leaked=" + ",".join(leaked[:5]) if leaked else "ok",
        )
    )

    discovery_ids = {
        evidence_id
        for evidence_id, item in evidence_by_id.items()
        if item.get("verification_status") == "unverified_lead"
    }
    rumor_errors = []
    for section in sections:
        ids = {str(item) for item in section.get("evidence_ids") or []}
        body = str(section.get("body") or "")
        if ids.intersection(discovery_ids) and not re.search(
            r"传闻|据报道|未核实|线索|尚未确认", body
        ):
            rumor_errors.append(str(section.get("heading") or "untitled"))
    findings.append(
        EvalFinding(
            check="rumor.per_section_label",
            passed=not rumor_errors,
            detail="errors=" + ",".join(rumor_errors) if rumor_errors else "ok",
        )
    )

    categories = {str(section.get("category") or "") for section in sections}
    evidence_text = " ".join(
        f"{item.get('title') or ''} {item.get('summary') or ''}".casefold()
        for item in evidence
        if isinstance(item, dict)
    )
    expected_categories = set()
    if re.search(
        r"goal|beat|defeat|world cup|match|var|penalty|世界杯|进球",
        evidence_text,
    ):
        expected_categories.add("match")
    if re.search(
        r"transfer|signing|bid|agreement|medical|转会|报价|协议",
        evidence_text,
    ):
        expected_categories.add("transfer")
    missing_categories = sorted(expected_categories - categories)
    findings.append(
        EvalFinding(
            check="coverage.expected_categories",
            passed=not missing_categories,
            detail=(
                "missing=" + ",".join(missing_categories)
                if missing_categories
                else "ok"
            ),
        )
    )

    minute_event_ids = {
        evidence_id
        for evidence_id, item in evidence_by_id.items()
        for text in [f"{item.get('title') or ''} {item.get('summary') or ''}"]
        if MINUTE_EVENT_RE.search(text) and MATCH_EVENT_RE.search(text)
    }
    timeline_ids = {
        str(evidence_id)
        for event in ((report.get("enrichment") or {}).get("match_timeline") or [])
        if isinstance(event, dict)
        for evidence_id in event.get("evidence_ids") or []
    }
    missing_timeline = sorted(minute_event_ids - timeline_ids)
    findings.append(
        EvalFinding(
            check="match_timeline.minute_event_coverage",
            passed=not missing_timeline,
            detail=(
                "missing=" + ",".join(missing_timeline[:5])
                if missing_timeline
                else "ok"
            ),
        )
    )

    media_assets = ((report.get("enrichment") or {}).get("media_assets") or [])
    bad_media = []
    for asset in media_assets:
        if not isinstance(asset, dict):
            continue
        ids = {str(item) for item in asset.get("evidence_ids") or []}
        if ids and not ids.intersection(refs):
            bad_media.append(str(asset.get("title") or asset.get("url") or "media"))
    findings.append(
        EvalFinding(
            check="media.bound_to_rendered_evidence",
            passed=not bad_media,
            detail="bad=" + ",".join(bad_media[:5]) if bad_media else "ok",
        )
    )

    passed_count = sum(1 for item in findings if item.passed)
    score = passed_count / max(1, len(findings))
    return EvalReport(
        name=name,
        passed=all(item.passed for item in findings),
        score=score,
        findings=findings,
    )


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(
            "usage: python -m services.report_api.evals <result-json>",
            file=sys.stderr,
        )
        return 2
    payload = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    report = evaluate_daily_digest_result(payload, name=Path(args[0]).stem)
    print(report.model_dump_json(indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
