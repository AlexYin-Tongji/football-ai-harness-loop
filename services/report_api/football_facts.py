from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from services.mcp_servers.football_data import list_competition_matches
from services.report_api.domain import ConsumerReportRequest, Evidence, ReportType
from services.report_api.time_scope import format_beijing, scope_for_request

STAGE_LABELS = {
    "GROUP_STAGE": "小组赛",
    "LAST_32": "32强淘汰赛（LAST_32）",
    "LAST_16": "16强淘汰赛（LAST_16）",
    "QUARTER_FINALS": "1/4决赛",
    "SEMI_FINALS": "半决赛",
    "THIRD_PLACE": "三四名决赛",
    "FINAL": "决赛",
}


class MatchScoreFact(BaseModel):
    home: int = Field(ge=0, le=30)
    away: int = Field(ge=0, le=30)
    duration: str = Field(default="REGULAR", max_length=80)
    regular_home: int | None = Field(default=None, ge=0, le=30)
    regular_away: int | None = Field(default=None, ge=0, le=30)
    penalties_home: int | None = Field(default=None, ge=0, le=30)
    penalties_away: int | None = Field(default=None, ge=0, le=30)


class MatchEventFact(BaseModel):
    minute: str = Field(min_length=1, max_length=20)
    event_type: str = Field(min_length=1, max_length=80)
    team: str | None = Field(default=None, max_length=120)
    player: str | None = Field(default=None, max_length=120)
    score_after: str | None = Field(default=None, max_length=20)
    source_id: str = Field(min_length=1, max_length=100)


class MatchFact(BaseModel):
    fact_id: str = Field(min_length=1, max_length=100)
    provider_match_id: str = Field(min_length=1, max_length=100)
    source_id: str = "football-data-org"
    kickoff_utc: datetime
    kickoff_local: str = Field(min_length=1, max_length=80)
    status: str = Field(min_length=1, max_length=80)
    stage: str = Field(min_length=1, max_length=120)
    home: str = Field(min_length=1, max_length=160)
    away: str = Field(min_length=1, max_length=160)
    score: MatchScoreFact | None = None
    winner: str | None = Field(default=None, max_length=160)
    events: list[MatchEventFact] = Field(default_factory=list, max_length=80)
    event_source_status: Literal[
        "not_requested", "unavailable", "not_configured", "not_covered", "partial", "ok"
    ] = "unavailable"


class FactCoverageIssue(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    severity: Literal["info", "warning", "blocked"] = "warning"
    message: str = Field(min_length=1, max_length=500)
    match_fact_id: str | None = Field(default=None, max_length=100)


class DailyFootballFactPack(BaseModel):
    report_type: ReportType
    subject: str
    scope_label: str
    window_start_utc: datetime
    window_end_utc: datetime
    data_cutoff_utc: datetime
    matches: list[MatchFact] = Field(default_factory=list, max_length=100)
    coverage_issues: list[FactCoverageIssue] = Field(
        default_factory=list,
        max_length=80,
    )
    source_attempts: dict[str, str] = Field(default_factory=dict)


def _kickoff_utc(match: dict) -> datetime | None:
    raw = str(match.get("utc_date") or "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _score(match: dict) -> MatchScoreFact | None:
    score = match.get("score") or {}
    full_time = score.get("fullTime") or {}
    home = full_time.get("home")
    away = full_time.get("away")
    if not isinstance(home, int) or not isinstance(away, int):
        return None
    regular = score.get("regularTime") or {}
    penalties = score.get("penalties") or {}
    regular_home = regular.get("home")
    regular_away = regular.get("away")
    return MatchScoreFact(
        home=home,
        away=away,
        duration=str(score.get("duration") or "REGULAR"),
        regular_home=regular_home if isinstance(regular_home, int) else None,
        regular_away=regular_away if isinstance(regular_away, int) else None,
        penalties_home=(
            penalties.get("home") if isinstance(penalties.get("home"), int) else None
        ),
        penalties_away=(
            penalties.get("away") if isinstance(penalties.get("away"), int) else None
        ),
    )


def _winner(home: str, away: str, score: MatchScoreFact | None) -> str | None:
    if score is None:
        return None
    if score.home > score.away:
        return home
    if score.away > score.home:
        return away
    return "draw"


def _fact_id(match: dict, kickoff: datetime) -> str:
    seed = "|".join(
        [
            str(match.get("id") or ""),
            str(match.get("home") or ""),
            str(match.get("away") or ""),
            kickoff.isoformat(),
        ]
    )
    return "football-data-match-" + hashlib.sha256(seed.encode()).hexdigest()[:12]


def _to_match_fact(match: dict) -> MatchFact | None:
    kickoff = _kickoff_utc(match)
    if kickoff is None:
        return None
    home = str(match.get("home") or "TBD")
    away = str(match.get("away") or "TBD")
    score = _score(match)
    return MatchFact(
        fact_id=_fact_id(match, kickoff),
        provider_match_id=str(match.get("id") or _fact_id(match, kickoff)),
        kickoff_utc=kickoff,
        kickoff_local=format_beijing(kickoff),
        status=str(match.get("status") or "UNKNOWN"),
        stage=str(match.get("stage") or "UNKNOWN"),
        home=home,
        away=away,
        score=score,
        winner=_winner(home, away, score),
        event_source_status=(
            "not_configured" if not os.getenv("SPORTMONKS_API_TOKEN") else "not_covered"
        ),
    )


def _score_text(match: MatchFact) -> str:
    if match.score is None:
        return f"{match.home} vs {match.away}"
    text = f"{match.home} {match.score.home}-{match.score.away} {match.away}"
    if match.score.duration == "PENALTY_SHOOTOUT":
        details = []
        if (
            match.score.regular_home is not None
            and match.score.regular_away is not None
        ):
            details.append(
                f"常规时间 {match.score.regular_home}-{match.score.regular_away}"
            )
        if (
            match.score.penalties_home is not None
            and match.score.penalties_away is not None
        ):
            details.append(
                f"点球 {match.score.penalties_home}-{match.score.penalties_away}"
            )
        suffix = "；".join(details)
        return f"{text}（点球大战后；{suffix}）" if suffix else f"{text}（点球大战后）"
    if match.score.duration == "EXTRA_TIME":
        if (
            match.score.regular_home is not None
            and match.score.regular_away is not None
        ):
            return (
                f"{text}（加时后；常规时间 "
                f"{match.score.regular_home}-{match.score.regular_away}）"
            )
        return f"{text}（加时后）"
    return text


def _stage_text(stage: str) -> str:
    normalized = stage.strip().upper()
    return STAGE_LABELS.get(normalized, stage)


def _match_coverage_issues(match: MatchFact) -> list[FactCoverageIssue]:
    if match.status != "FINISHED":
        return []
    if match.events:
        return []
    reason = (
        "Sportmonks 未配置，当前批准结构化源只提供赛程赛果。"
        if match.event_source_status == "not_configured"
        else (
            "当前 Sportmonks 授权未返回 World Cup fixture events；"
            "football-data 不提供进球者/分钟。"
        )
    )
    return [
        FactCoverageIssue(
            code="match_events_unavailable",
            severity="warning",
            match_fact_id=match.fact_id,
            message=(
                f"{match.home} vs {match.away} "
                "缺少结构化进球者、红黄牌、换人和分钟事件；"
                f"{reason} 后续报道不得编写未入证据的进球时间线。"
            ),
        )
    ]


async def collect_daily_football_fact_pack(
    request: ConsumerReportRequest,
) -> DailyFootballFactPack:
    scope = scope_for_request(request)
    pack = DailyFootballFactPack(
        report_type=request.report_type,
        subject=request.subject,
        scope_label=scope.local_window_label,
        window_start_utc=scope.window_start_utc,
        window_end_utc=scope.window_end_utc,
        data_cutoff_utc=scope.data_cutoff_utc,
    )
    if request.report_type not in {
        ReportType.DAILY_FOOTBALL_DIGEST,
        ReportType.WORLD_CUP_DAILY,
    }:
        return pack
    if not os.getenv("FOOTBALL_DATA_API_KEY"):
        pack.coverage_issues.append(
            FactCoverageIssue(
                code="fixture_source_not_configured",
                severity="blocked",
                message="FOOTBALL_DATA_API_KEY 未配置，无法生成当天结构化赛程清单。",
            )
        )
        pack.source_attempts["football-data:daily_matches"] = "not_configured"
        return pack

    date_from = scope.window_start_utc.date().isoformat()
    date_to = (scope.window_end_utc - timedelta(seconds=1)).date().isoformat()
    payload = await list_competition_matches(
        "WC", date_from=date_from, date_to=date_to
    )
    pack.source_attempts["football-data:daily_matches"] = (
        f"ok:{len(payload.get('matches', []))}"
    )
    matches: list[MatchFact] = []
    for raw_match in payload.get("matches", []):
        match = _to_match_fact(raw_match)
        if match is None:
            continue
        if scope.window_start_utc <= match.kickoff_utc < scope.window_end_utc:
            matches.append(match)
    pack.matches = sorted(matches, key=lambda item: item.kickoff_utc)
    if not pack.matches:
        pack.coverage_issues.append(
            FactCoverageIssue(
                code="no_matches_in_report_window",
                severity="warning",
                message=(
                    f"{scope.local_window_label} "
                    "内没有 football-data 返回的世界杯比赛。"
                ),
            )
        )
    for match in pack.matches:
        pack.coverage_issues.extend(_match_coverage_issues(match))
    return pack


def fact_pack_to_evidence(pack: DailyFootballFactPack) -> list[Evidence]:
    evidence: list[Evidence] = []
    model_safe_scope_label = pack.scope_label.replace(" 00:00-24:00", " 全日窗口")
    issues_by_match: dict[str, list[FactCoverageIssue]] = {}
    for issue in pack.coverage_issues:
        if issue.match_fact_id:
            issues_by_match.setdefault(issue.match_fact_id, []).append(issue)
    for match in pack.matches:
        score_text = _score_text(match)
        if match.score is None:
            title = (
                f"结构化赛程｜{match.home} vs {match.away}"
                f"（北京时间 {match.kickoff_local}）"
            )
            result_text = "尚无完场比分；赛前资料不得写成赛果"
        else:
            title = f"结构化赛果｜{score_text}（北京时间 {match.kickoff_local}）"
            result_text = (
                f"结果方向按对阵顺序记录为 {match.home} vs {match.away}；"
                f"结论：{match.winner or '未知'}"
            )
        if match.events:
            event_text = "结构化事件：" + "；".join(
                f"{event.minute} {event.event_type} {event.player or ''}".strip()
                for event in match.events[:12]
            )
        else:
            event_text = (
                "结构化事件：当前无进球者/分钟/红黄牌/换人事件；"
                "不得编写未入证据的时间线"
            )
        issue_text = " ".join(
            issue.message for issue in issues_by_match.get(match.fact_id, [])
        )
        summary = (
            f"football facts 结构化赛事实据：{model_safe_scope_label}；"
            f"北京时间 {match.kickoff_local}，阶段 {_stage_text(match.stage)}，"
            f"{score_text}，"
            f"状态 {match.status}，{result_text}。{event_text}。{issue_text}"
        ).strip()
        evidence.append(
            Evidence(
                id=match.fact_id,
                title=title,
                url="https://www.football-data.org/",
                published_at=pack.data_cutoff_utc,
                source_name="football-data.org",
                summary=summary,
                source_id="football-data-org",
                trust_tier="S1_structured_provider",
                evidence_kind="structured",
                verification_status="corroborated",
                source_independence_key="football-data-org",
            )
        )
    return evidence


def fact_pack_warnings(pack: DailyFootballFactPack) -> list[str]:
    warnings: list[str] = []
    for issue in pack.coverage_issues:
        if issue.severity in {"warning", "blocked"}:
            warnings.append(issue.message)
    return list(dict.fromkeys(warnings))[:12]
