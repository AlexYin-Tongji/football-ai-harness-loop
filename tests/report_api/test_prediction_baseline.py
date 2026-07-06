from services.report_api.domain import Evidence, MatchModelContext, RecentMatchSample
from services.report_api.prediction import (
    build_statistical_baseline,
    extract_external_predictions,
    multiclass_brier,
    multiclass_log_loss,
)


def sample(goals_for: int, goals_against: int) -> RecentMatchSample:
    return RecentMatchSample(goals_for=goals_for, goals_against=goals_against)


def test_elo_poisson_baseline_is_normalized_and_reproducible() -> None:
    context = MatchModelContext(
        home_team="Alpha",
        away_team="Beta",
        home_recent=[sample(2, 0), sample(2, 1), sample(1, 0), sample(3, 1)],
        away_recent=[sample(0, 1), sample(1, 1), sample(1, 2), sample(0, 0)],
        home_elo=1900,
        away_elo=1650,
        evidence_ids=["stats-1"],
    )

    baseline = build_statistical_baseline(context)

    assert baseline is not None
    assert baseline.method == "elo_poisson"
    assert baseline.home_win > baseline.away_win
    assert abs(baseline.home_win + baseline.draw + baseline.away_win - 1) < 0.001
    assert baseline.evidence_ids == ["stats-1"]


def test_baseline_requires_three_recent_matches_per_team() -> None:
    context = MatchModelContext(
        home_team="Alpha",
        away_team="Beta",
        home_recent=[sample(1, 0)],
        away_recent=[sample(0, 1)],
    )

    assert build_statistical_baseline(context) is None


def test_prediction_metrics_penalize_wrong_confidence() -> None:
    good = multiclass_brier((0.8, 0.1, 0.1), 0)
    bad = multiclass_brier((0.05, 0.05, 0.9), 0)

    assert good < bad
    assert multiclass_log_loss((0.8, 0.1, 0.1), 0) < multiclass_log_loss(
        (0.05, 0.05, 0.9), 0
    )


def test_external_prediction_requires_an_explicit_source_statement() -> None:
    evidence = [
        Evidence(
            id="opta-1",
            title="Match preview",
            url="https://example.com/preview",
            published_at="2026-07-01T08:00:00Z",
            source_name="Publisher",
            summary="Opta rates the home side's chance of victory at 73.9%.",
        ),
        Evidence(
            id="ordinary-1",
            title="Team training update",
            url="https://example.com/training",
            published_at="2026-07-01T07:00:00Z",
            source_name="Publisher",
            summary="The squad completed training.",
        ),
    ]

    predictions = extract_external_predictions(evidence)

    assert len(predictions) == 1
    assert predictions[0].source_name == "Opta"
    assert predictions[0].evidence_ids == ["opta-1"]
    assert "73.9%" in predictions[0].summary
