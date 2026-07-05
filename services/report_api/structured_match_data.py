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
    ReportType,
)
from services.report_api.time_scope import format_beijing, scope_for_request


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


def _kickoff_utc(match: dict) -> datetime | None:
    raw = str(match.get("utc_date") or "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _score_summary(match: dict) -> str:
    result = _score(match)
    home = str(match.get("home") or "TBD")
    away = str(match.get("away") or "TBD")
    status = str(match.get("status") or "UNKNOWN")
    score = match.get("score") or {}
    duration = str(score.get("duration") or "REGULAR")
    if result is None:
        return f"{home} vs {away}，状态 {status}，未记录完场比分"
    home_goals, away_goals = result
    if duration == "PENALTY_SHOOTOUT":
        regular = score.get("regularTime") or {}
        penalties = score.get("penalties") or {}
        return (
            f"{home} {home_goals}-{away_goals} {away}（点球大战后；"
            f"常规时间 {regular.get('home')}-{regular.get('away')}，"
            f"点球 {penalties.get('home')}-{penalties.get('away')}）"
        )
    if duration == "EXTRA_TIME":
        regular = score.get("regularTime") or {}
        return (
            f"{home} {home_goals}-{away_goals} {away}（加时后；"
            f"常规时间 {regular.get('home')}-{regular.get('away')}）"
        )
    return f"{home} {home_goals}-{away_goals} {away}"


def _winner_text(match: dict) -> str:
    result = _score(match)
    if result is None:
        return "未开赛或未完赛"
    home_goals, away_goals = result
    home = str(match.get("home") or "主队")
    away = str(match.get("away") or "客队")
    if home_goals > away_goals:
        return f"{home} 胜"
    if away_goals > home_goals:
        return f"{away} 胜"
    return "平局"


def _daily_match_evidence_id(match: dict, kickoff: datetime) -> str:
    seed = "|".join(
        [
            str(match.get("id") or ""),
            str(match.get("home") or ""),
            str(match.get("away") or ""),
            kickoff.isoformat(),
        ]
    )
    return "football-data-match-" + hashlib.sha256(seed.encode()).hexdigest()[:12]


def _daily_match_evidence(match: dict, *, published_at: datetime) -> Evidence | None:
    kickoff = _kickoff_utc(match)
    if kickoff is None:
        return None
    home = str(match.get("home") or "TBD")
    away = str(match.get("away") or "TBD")
    status = str(match.get("status") or "UNKNOWN")
    stage = str(match.get("stage") or "UNKNOWN")
    kickoff_local = format_beijing(kickoff)
    result = _score(match)
    if result is None:
        title = f"结构化赛程｜{home} vs {away}（北京时间 {kickoff_local}）"
        summary = (
            "football-data.org 结构化赛程："
            f"北京时间 {kickoff_local}，{stage}，{home} vs {away}，"
            f"状态 {status}，尚无完场比分。赛前资料不得写成赛果。"
        )
    else:
        title = f"结构化赛果｜{_score_summary(match)}（北京时间 {kickoff_local}）"
        summary = (
            "football-data.org 结构化赛果："
            f"北京时间 {kickoff_local}，{stage}，{_score_summary(match)}，"
            f"状态 {status}，结果方向按对阵顺序记录为 {home} vs {away}；"
            f"结论：{_winner_text(match)}。"
        )
    return Evidence(
        id=_daily_match_evidence_id(match, kickoff),
        title=title,
        url="https://www.football-data.org/",
        published_at=published_at,
        source_name="football-data.org",
        summary=summary,
        source_id="football-data-org",
        trust_tier="S1_structured_provider",
        evidence_kind="structured",
        verification_status="corroborated",
        source_independence_key="football-data-org",
    )


async def collect_daily_structured_match_evidence(
    request: ConsumerReportRequest,
) -> list[Evidence]:
    """Return fixtures/results inside the requested Beijing report day."""
    if request.report_type not in {
        ReportType.DAILY_FOOTBALL_DIGEST,
        ReportType.WORLD_CUP_DAILY,
    }:
        return []
    if not os.getenv("FOOTBALL_DATA_API_KEY"):
        return []
    scope = scope_for_request(request)
    date_from = scope.window_start_utc.date().isoformat()
    date_to = (scope.window_end_utc - timedelta(seconds=1)).date().isoformat()
    payload = await list_competition_matches(
        "WC", date_from=date_from, date_to=date_to
    )
    evidence: list[Evidence] = []
    for match in payload.get("matches", []):
        kickoff = _kickoff_utc(match)
        if kickoff is None:
            continue
        if not (scope.window_start_utc <= kickoff < scope.window_end_utc):
            continue
        item = _daily_match_evidence(match, published_at=scope.data_cutoff_utc)
        if item is not None:
            evidence.append(item)
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
