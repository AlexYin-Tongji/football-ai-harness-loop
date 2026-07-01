from __future__ import annotations

import copy
import re

from pydantic import ValidationError

from services.report_api.domain import (
    GeneratedReport,
    MatchStage,
    ReportRequest,
    ReportType,
)


class ReportValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def normalize_generated_output(
    raw_output: dict[str, object], request: ReportRequest
) -> dict[str, object]:
    """Apply narrow, auditable repairs before the strict quality gate."""
    normalized = copy.deepcopy(raw_output)
    prediction = normalized.get("prediction")
    if not isinstance(prediction, dict):
        return normalized

    externals = prediction.get("external_predictions")
    if isinstance(externals, list):
        evidence_by_id = {item.id: item for item in request.evidence}
        for external in externals:
            if not isinstance(external, dict):
                continue
            name = str(external.get("source_name") or "").strip()
            evidence_ids = external.get("evidence_ids") or []
            cited_text = " ".join(
                (
                    f"{evidence_by_id[item_id].source_name} "
                    f"{evidence_by_id[item_id].title} "
                    f"{evidence_by_id[item_id].summary}"
                )
                for item_id in evidence_ids
                if item_id in evidence_by_id
            )
            for canonical in ("Opta", "Stats Perform"):
                if canonical.casefold() in name.casefold() and canonical.casefold() in (
                    cited_text.casefold()
                ):
                    external["source_name"] = canonical
                    break

    if request.match_stage == MatchStage.KNOCKOUT and not prediction.get(
        "qualification"
    ):
        try:
            home = float(prediction["home_win"])
            draw = float(prediction["draw"])
            away = float(prediction["away_win"])
            total = home + draw + away
        except (KeyError, TypeError, ValueError):
            return normalized
        if total > 0:
            home_qualification = (home + draw / 2) / total
            prediction["qualification"] = {
                "home": round(home_qualification, 4),
                "away": round(1 - home_qualification, 4),
            }
            warnings = normalized.setdefault("warnings", [])
            if isinstance(warnings, list):
                fallback_warning = (
                    "模型遗漏淘汰赛晋级概率；系统以90分钟平局双方均分的保守规则补齐，"
                    "未单独建模加时赛和点球。"
                )
                if len(warnings) < 12:
                    warnings.append(fallback_warning)
                elif warnings:
                    warnings[-1] = fallback_warning
    elif request.match_stage == MatchStage.GROUP:
        prediction["qualification"] = None
    return normalized


def validate_generated_report(
    raw_output: dict[str, object], request: ReportRequest
) -> GeneratedReport:
    try:
        report = GeneratedReport.model_validate(raw_output)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(item) for item in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
        raise ReportValidationError(errors) from exc

    errors: list[str] = []
    if not re.search(r"[\u4e00-\u9fff]", report.title):
        errors.append("title must be written in Simplified Chinese")
    if len(re.findall(r"[\u4e00-\u9fff]", report.executive_summary)) < 10:
        errors.append("executive_summary must be written in Simplified Chinese")
    allowed_ids = {item.id for item in request.evidence}
    referenced_ids: set[str] = set()

    visible_text = " ".join(
        [report.executive_summary, *(section.body for section in report.sections)]
    )
    if any(evidence_id in visible_text for evidence_id in allowed_ids):
        errors.append("visible report text must not expose internal evidence IDs")

    for section in report.sections:
        referenced_ids.update(section.evidence_ids)
        discovery_ids = {
            item.id
            for item in request.evidence
            if item.verification_status == "unverified_lead"
        }
        if discovery_ids.intersection(section.evidence_ids) and not re.search(
            r"传闻|据报道|未核实|线索|尚未确认", section.body
        ):
            errors.append(
                f"section '{section.heading}' cites discovery leads "
                "without a rumor label"
            )

    for spotlight in report.enrichment.player_spotlights:
        referenced_ids.update(spotlight.evidence_ids)
        source_text = " ".join(
            f"{item.title} {item.summary}"
            for item in request.evidence
            if item.id in spotlight.evidence_ids
        )
        for metric in spotlight.metrics:
            if metric.value.casefold() not in source_text.casefold():
                errors.append(
                    f"player metric is not present in cited evidence: {metric.value}"
                )

    for event in report.enrichment.match_timeline:
        referenced_ids.update(event.evidence_ids)
        source_text = " ".join(
            f"{item.title} {item.summary}"
            for item in request.evidence
            if item.id in event.evidence_ids
        )
        minute_number = event.minute.split("+", 1)[0]
        if minute_number.isdigit() and not re.search(
            rf"(?<!\d){re.escape(minute_number)}(?:st|nd|rd|th|\s*分钟|'|’)?",
            source_text,
            re.I,
        ):
            errors.append(
                f"timeline minute is not present in cited evidence: {event.minute}"
            )

    if report.enrichment.media_assets:
        errors.append("media_assets must be injected by the harness")

    if request.report_type == ReportType.MATCH_PREDICTION:
        if report.prediction is None:
            errors.append("prediction is required for match_prediction")
        else:
            prediction = report.prediction
            total = prediction.home_win + prediction.draw + prediction.away_win
            if abs(total - 1.0) > 0.001:
                errors.append("home_win + draw + away_win must equal 1")

            for factor in [
                *prediction.supporting_factors,
                *prediction.counter_factors,
            ]:
                referenced_ids.update(factor.evidence_ids)
            for external in prediction.external_predictions:
                referenced_ids.update(external.evidence_ids)
                cited = [
                    item
                    for item in request.evidence
                    if item.id in external.evidence_ids
                ]
                source_text = " ".join(
                    f"{item.source_name} {item.title} {item.summary}" for item in cited
                )
                if external.source_name.casefold() not in source_text.casefold():
                    errors.append(
                        f"external prediction source is not present in cited evidence: "
                        f"{external.source_name}"
                    )
                if external.home_win is not None and not re.search(
                    r"\d+(?:\.\d+)?\s*%|probability|chance|odds|概率|胜率",
                    source_text,
                    re.I,
                ):
                    errors.append(
                        "numeric external prediction lacks a numeric source statement"
                    )
            if prediction.statistical_baseline is not None:
                errors.append("statistical_baseline must be injected by the harness")

            if request.match_stage == MatchStage.KNOCKOUT:
                if prediction.qualification is None:
                    errors.append("qualification is required for knockout matches")
                else:
                    qualification_total = (
                        prediction.qualification.home + prediction.qualification.away
                    )
                    if abs(qualification_total - 1.0) > 0.001:
                        errors.append("qualification probabilities must equal 1")
            elif prediction.qualification is not None:
                errors.append("qualification must be null for group matches")
    elif report.prediction is not None:
        errors.append("prediction must be null for non-match reports")

    unknown_ids = referenced_ids - allowed_ids
    if unknown_ids:
        errors.append(f"unknown evidence IDs: {sorted(unknown_ids)}")

    if errors:
        raise ReportValidationError(errors)
    return report
