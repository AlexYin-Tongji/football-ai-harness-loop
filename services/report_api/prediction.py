from __future__ import annotations

import math
import re

from services.report_api.domain import (
    Evidence,
    ExternalPrediction,
    MatchModelContext,
    StatisticalBaseline,
)

SHRINKAGE_MATCHES = 3
TOURNAMENT_GOALS_PER_TEAM = 1.35
PREDICTION_SIGNAL = re.compile(
    r"Opta|Stats Perform|probability|chance(?:s)? of (?:victory|winning)|"
    r"win probability|预测概率|胜率",
    re.I,
)


def _shrunk_average(values: list[int]) -> float:
    sample_total = sum(values)
    return (sample_total + SHRINKAGE_MATCHES * TOURNAMENT_GOALS_PER_TEAM) / (
        len(values) + SHRINKAGE_MATCHES
    )


def _poisson_probability(goals: int, expected: float) -> float:
    return math.exp(-expected) * expected**goals / math.factorial(goals)


def build_statistical_baseline(
    context: MatchModelContext | None,
) -> StatisticalBaseline | None:
    """Create a reproducible pre-match baseline from sourced recent results."""
    if context is None:
        return None
    if len(context.home_recent) < 3 or len(context.away_recent) < 3:
        return None

    home_for = _shrunk_average([item.goals_for for item in context.home_recent])
    home_against = _shrunk_average(
        [item.goals_against for item in context.home_recent]
    )
    away_for = _shrunk_average([item.goals_for for item in context.away_recent])
    away_against = _shrunk_average(
        [item.goals_against for item in context.away_recent]
    )
    expected_home = (home_for + away_against) / 2
    expected_away = (away_for + home_against) / 2
    method = "poisson"

    if context.home_elo is not None and context.away_elo is not None:
        method = "elo_poisson"
        multiplier = math.exp((context.home_elo - context.away_elo) / 800)
        multiplier = max(0.65, min(1.55, multiplier))
        expected_home *= multiplier
        expected_away /= multiplier

    expected_home = max(0.2, min(4.5, expected_home))
    expected_away = max(0.2, min(4.5, expected_away))
    home_win = draw = away_win = 0.0
    covered_mass = 0.0
    for home_goals in range(9):
        for away_goals in range(9):
            probability = _poisson_probability(
                home_goals, expected_home
            ) * _poisson_probability(away_goals, expected_away)
            covered_mass += probability
            if home_goals > away_goals:
                home_win += probability
            elif home_goals == away_goals:
                draw += probability
            else:
                away_win += probability

    home_win /= covered_mass
    draw /= covered_mass
    away_win = 1 - home_win - draw
    return StatisticalBaseline(
        method=method,
        home_win=round(home_win, 4),
        draw=round(draw, 4),
        away_win=round(away_win, 4),
        expected_home_goals=round(expected_home, 2),
        expected_away_goals=round(expected_away, 2),
        sample_size_home=len(context.home_recent),
        sample_size_away=len(context.away_recent),
        evidence_ids=context.evidence_ids,
    )


def extract_external_predictions(evidence: list[Evidence]) -> list[ExternalPrediction]:
    """Extract only explicitly sourced external forecast statements."""
    predictions: list[ExternalPrediction] = []
    for item in evidence:
        text = f"{item.title}. {item.summary}"
        match = PREDICTION_SIGNAL.search(text)
        if match is None:
            continue
        sentences = re.split(r"(?<=[.!?。！？])\s+", text)
        statement = next(
            (
                sentence
                for sentence in sentences
                if PREDICTION_SIGNAL.search(sentence) and "%" in sentence
            ),
            next(
                (
                    sentence
                    for sentence in sentences
                    if PREDICTION_SIGNAL.search(sentence)
                ),
                text,
            ),
        )[:1000]
        source_name = (
            "Opta"
            if re.search(r"Opta", statement, re.I)
            else "Stats Perform"
            if re.search(r"Stats Perform", statement, re.I)
            else item.source_name
        )
        predictions.append(
            ExternalPrediction(
                source_name=source_name,
                summary=statement,
                evidence_ids=[item.id],
            )
        )
        if len(predictions) >= 6:
            break
    return predictions


def multiclass_brier(probabilities: tuple[float, float, float], outcome: int) -> float:
    """Return the three-class Brier score; outcome is 0/home, 1/draw, 2/away."""
    if outcome not in {0, 1, 2}:
        raise ValueError("outcome must be 0, 1 or 2")
    return sum(
        (probability - (1.0 if index == outcome else 0.0)) ** 2
        for index, probability in enumerate(probabilities)
    )


def multiclass_log_loss(
    probabilities: tuple[float, float, float], outcome: int
) -> float:
    if outcome not in {0, 1, 2}:
        raise ValueError("outcome must be 0, 1 or 2")
    return -math.log(max(1e-12, probabilities[outcome]))
