from __future__ import annotations

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
    allowed_ids = {item.id for item in request.evidence}
    referenced_ids: set[str] = set()

    for section in report.sections:
        referenced_ids.update(section.evidence_ids)

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
