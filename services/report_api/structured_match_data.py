from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime, timedelta

from services.mcp_servers.football_data import list_competition_matches
from services.report_api.domain import (
    ConsumerReportRequest,
    Evidence,
    MatchModelContext,
    RecentMatchSample,
)
from services.report_api.football_facts import (
    collect_daily_football_fact_pack,
    fact_pack_to_evidence,
    fact_pack_warnings,
)
from services.report_api.time_scope import scope_for_request


def _teams_from_subject(subject: str, matches: list[dict]) -> tuple[str, str] | None:
    normalized = subject.casefold()
    candidates: list[tuple[int, str, str]] = []
    for match in matches:
        home = str(match.get("home") or "")
        away = str(match.get("away") or "")
        both_teams_match = (
            home.casefold() in normalized and away.casefold() in normalized
        )
        if home and away and both_teams_match:
            candidates.append((len(home) + len(away), home, away))
    if candidates:
        _, home, away = max(candidates)
        return home, away

    parts = re.split(r"\s+(?:vs?\.?|versus|对阵)\s+", subject, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        home = re.split(r"[|｜]", parts[0], maxsplit=1)[0].strip()
        away = re.split(r"[|｜]", parts[1], maxsplit=1)[0].strip()
        if home and away:
            return home, away
    return None


def _score(match: dict) -> tuple[int, int] | None:
    score = match.get("score") or {}
    full_time = score.get("fullTime") or {}
    home = full_time.get("home")
    away = full_time.get("away")
    if isinstance(home, int) and isinstance(away, int):
        return home, away
    return None


async def collect_daily_structured_match_evidence_with_warnings(
    request: ConsumerReportRequest,
) -> tuple[list[Evidence], list[str]]:
    """Return fixtures/results inside the requested Beijing report day."""
    pack = await collect_daily_football_fact_pack(request)
    return fact_pack_to_evidence(pack), fact_pack_warnings(pack)


async def collect_daily_structured_match_evidence(
    request: ConsumerReportRequest,
) -> list[Evidence]:
    evidence, _warnings = await collect_daily_structured_match_evidence_with_warnings(
        request
    )
    return evidence


async def collect_structured_match_context(
    request: ConsumerReportRequest,
) -> tuple[list[Evidence], MatchModelContext | None]:
    """Load sourced recent World Cup results when the approved API is configured."""
    if not os.getenv("FOOTBALL_DATA_API_KEY"):
        return [], None
    cutoff_date = scope_for_request(request).data_cutoff_utc.date()
    payload = await list_competition_matches(
        "WC",
        date_from=(cutoff_date - timedelta(days=365)).isoformat(),
        date_to=cutoff_date.isoformat(),
    )
    matches = payload.get("matches", [])
    teams = _teams_from_subject(request.subject, matches)
    if teams is None:
        return [], None
    home_team, away_team = teams
    home_recent: list[RecentMatchSample] = []
    away_recent: list[RecentMatchSample] = []
    lines: list[str] = []
    for match in matches:
        result = _score(match)
        if result is None or match.get("status") != "FINISHED":
            continue
        home_goals, away_goals = result
        match_home = str(match.get("home") or "")
        match_away = str(match.get("away") or "")
        if home_team in {match_home, match_away}:
            goals_for, goals_against = (
                (home_goals, away_goals)
                if match_home == home_team
                else (away_goals, home_goals)
            )
            home_recent.append(
                RecentMatchSample(goals_for=goals_for, goals_against=goals_against)
            )
        if away_team in {match_home, match_away}:
            goals_for, goals_against = (
                (home_goals, away_goals)
                if match_home == away_team
                else (away_goals, home_goals)
            )
            away_recent.append(
                RecentMatchSample(goals_for=goals_for, goals_against=goals_against)
            )
        if home_team in {match_home, match_away} or away_team in {
            match_home,
            match_away,
        }:
            lines.append(f"{match_home} {home_goals}-{away_goals} {match_away}")

    now = datetime.now(UTC)
    evidence_id = "football-data-" + hashlib.sha256(
        (home_team + away_team + "|".join(lines)).encode()
    ).hexdigest()[:12]
    evidence = Evidence(
        id=evidence_id,
        title=f"{home_team} 与 {away_team} 近期结构化赛果快照",
        url="https://www.football-data.org/",
        published_at=now,
        source_name="football-data.org",
        summary="；".join(lines[-20:]) or "未找到已结束比赛",
        source_id="football-data-org",
        trust_tier="S1_structured_provider",
        evidence_kind="structured",
        verification_status="corroborated",
    )
    context = MatchModelContext(
        home_team=home_team,
        away_team=away_team,
        home_recent=home_recent[-10:],
        away_recent=away_recent[-10:],
        evidence_ids=[evidence_id],
    )
    return [evidence], context
