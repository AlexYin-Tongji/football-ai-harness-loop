from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from services.report_api.claim_ledger import (
    build_numeric_claim_ledger,
    clean_evidence_text,
    sanitize_text_against_evidence,
    unsupported_numeric_tokens,
)
from services.report_api.critical_entities import CriticalEntity, load_critical_entities
from services.report_api.domain import (
    DeskBrief,
    DeskDraft,
    EditorialColumnPlan,
    Evidence,
    EvidenceFactor,
    GeneratedReport,
    MatchTimelineEvent,
    MediaAsset,
    PlayerMetric,
    PlayerSpotlight,
    PredictionOpinion,
    ReportRequest,
    ReportResponse,
    ReportSection,
    ReportType,
    TokenUsage,
)
from services.report_api.evidence_state import (
    completed_match_items,
    evidence_is_match_like,
    has_completed_match_claim,
    is_completed_match_evidence,
    is_transfer_evidence,
    is_upcoming_match_evidence,
    match_evidence_state,
    placeholder_copy_errors,
)
from services.report_api.media import collect_report_media
from services.report_api.model_control import (
    PREDICTION_ANALYSIS_CONTRACT,
    build_daily_final_messages,
    message_chars,
    stage_policy,
)
from services.report_api.prediction import (
    build_statistical_baseline,
    extract_external_predictions,
)
from services.report_api.prompts import (
    PROMPT_VERSION,
    append_revision_request,
    build_messages,
)
from services.report_api.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMRequest,
    LLMResult,
)
from services.report_api.validation import (
    ReportValidationError,
    normalize_generated_output,
    validate_generated_report,
)

FINAL_EVIDENCE_SUMMARY_CHARS = 420
MINUTE_GOAL_BY_RE = re.compile(
    r"(?P<minute>\d{1,3})(?:st|nd|rd|th)[-\s]?minute\s+"
    r"(?:winner|goal|equaliser|equalizer|penalty)[^.;:]{0,80}?"
    r"(?:by|from)\s+(?P<player>[A-Z][A-Za-zÀ-ÿ'’.-]+"
    r"(?:\s+[A-Z][A-Za-zÀ-ÿ'’.-]+){0,3})",
    re.I,
)
PLAYER_SCORED_MINUTE_RE = re.compile(
    r"(?P<player>[A-Z][A-Za-zÀ-ÿ'’.-]+(?:\s+[A-Z][A-Za-zÀ-ÿ'’.-]+){0,3})"
    r"[^.;:]{0,80}?\b(?:scored|scores|netted|struck|headed|converted)"
    r"[^.;:]{0,120}?(?P<minute>\d{1,3})(?:st|nd|rd|th)[-\s]?minute",
    re.I,
)
PLAYER_NAME_PATTERN = r"[A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,3}"
PLAYER_RESULT_MINUTE_RE = re.compile(
    rf"(?P<player>{PLAYER_NAME_PATTERN})"
    r"[^.;:]{0,100}?\b(?:made it|levelled|leveled|equalised|equalized|"
    rf"opened the scoring|put {PLAYER_NAME_PATTERN} ahead|"
    rf"gave {PLAYER_NAME_PATTERN})"
    r"[^.;:]{0,120}?(?:in|on|at)?\s*(?:the\s*)?"
    r"(?P<minute>\d{1,3}(?:\+\d{1,2})?)(?:st|nd|rd|th)?[-\s]?minute",
    re.I,
)
PLAYER_CARD_MINUTE_RE = re.compile(
    rf"(?P<player>{PLAYER_NAME_PATTERN})"
    r"[^.;:]{0,100}?\b(?:was\s+)?(?:sent off|shown (?:a )?(?:red|yellow) card|"
    r"booked|red-carded)"
    r"[^.;:]{0,100}?(?:in|on|at)?\s*(?:the\s*)?"
    r"(?P<minute>\d{1,3}(?:\+\d{1,2})?)(?:st|nd|rd|th)?[-\s]?minute",
    re.I,
)
PLAYER_SEASON_GOAL_RE = re.compile(
    rf"(?P<player>{PLAYER_NAME_PATTERN})"
    r"[^.;:]{0,100}?\b(?:scored|scores|netted|struck|converted)"
    r"[^.;:]{0,120}?(?:his|her|their)?\s*"
    r"(?P<value>\d{1,3})(?:st|nd|rd|th)?\s+(?:goal|goals)",
    re.I,
)
CN_MINUTE_PLAYER_ACTION_RE = re.compile(
    r"第\s*(?P<minute>\d{1,3})(?:\+\d{1,2})?\s*分钟"
    r"[^。；;.!?]{0,80}?"
    r"(?P<player>[\u4e00-\u9fff·]{2,12}|[A-Z][A-Za-zÀ-ÿ'’.-]+"
    r"(?:\s+[A-Z][A-Za-zÀ-ÿ'’.-]+){0,3})"
    r"[^。；;.!?]{0,80}?"
    r"(?:破门|进球|扳平|反超|点射|头球|低射|世界波|建功)",
    re.I,
)
SCORE_RE = re.compile(
    r"(?<![\d:-])(?P<score>\d{1,2}-\d{1,2})(?![\d:-])"
)
VISIBLE_SCORE_RE = re.compile(
    r"(?<![\d:-])(?P<home>\d{1,2})\s*[-–]\s*(?P<away>\d{1,2})(?![\d:-])"
)
TEAM_WORDS = (
    "France",
    "Paraguay",
    "Morocco",
    "Canada",
    "Portugal",
    "Croatia",
    "Spain",
    "Austria",
    "England",
    "Mexico",
    "Australia",
    "Egypt",
)
TEAM_ALIASES = {
    "France": ("France", "法国"),
    "Paraguay": ("Paraguay", "巴拉圭"),
    "Morocco": ("Morocco", "摩洛哥"),
    "Canada": ("Canada", "加拿大"),
    "Portugal": ("Portugal", "葡萄牙"),
    "Croatia": ("Croatia", "克罗地亚"),
    "Spain": ("Spain", "西班牙"),
    "Austria": ("Austria", "奥地利"),
    "England": ("England", "英格兰"),
    "Mexico": ("Mexico", "墨西哥"),
    "Australia": ("Australia", "澳大利亚", "澳洲"),
    "Egypt": ("Egypt", "埃及"),
    "Switzerland": ("Switzerland", "瑞士"),
    "Algeria": ("Algeria", "阿尔及利亚"),
    "Japan": ("Japan", "日本"),
}
MATCH_CATEGORY_RE = re.compile(
    r"世界杯|比赛|进球|VAR|点球|淘汰赛|晋级|击败|战胜|克罗地亚|葡萄牙|西班牙|奥地利|"
    r"英格兰|墨西哥|澳大利亚|埃及",
    re.I,
)
TRANSFER_CATEGORY_RE = re.compile(r"转会|签下|报价|热刺|费尔南德斯|英镑|record", re.I)
OFF_FIELD_CATEGORY_RE = re.compile(
    r"收视|球迷|酒吧|选帅|舆论|场外|日本|墨西哥城|赞助|阿美|污染|社区|"
    r"手表|礼物|YouTuber|国际足联|FIFA|规则|酒店|抵达|开球|商业|环境|"
    r"\b(?:sponsor|aramco|pollution|community|watch|watches|gift|hotel|arrival|"
    r"kick[- ]?off|hostile reception|all[- ]?nighter)\b",
    re.I,
)


class ReportGenerationError(RuntimeError):
    """Raised after the bounded generation loop cannot produce a valid report."""


ProgressCallback = Callable[..., None]


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _emit_progress(
    progress_callback: ProgressCallback | None,
    phase: str,
    progress: int,
    payload: dict[str, object] | None = None,
) -> None:
    if not progress_callback:
        return
    try:
        progress_callback(phase, progress, payload)
    except TypeError:
        progress_callback(phase, progress)


def _compact_request_for_final_editor(request: ReportRequest) -> ReportRequest:
    """Keep citation targets but avoid repeating full evidence in the final pass."""
    compact_evidence = [
        item.model_copy(
            update={
                "title": _shorten(item.title, 220),
                "summary": _shorten(item.summary, FINAL_EVIDENCE_SUMMARY_CHARS),
            }
        )
        for item in request.evidence
    ]
    return request.model_copy(update={"evidence": compact_evidence})


def _final_output_budget(request: ReportRequest, configured_limit: int) -> int:
    if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
        return min(
            configured_limit,
            {"concise": 2400, "standard": 3600, "deep": 4500}[request.length.value],
        )
    if request.report_type == ReportType.MATCH_PREDICTION:
        return min(configured_limit, 4500)
    return min(
        configured_limit,
        {"concise": 1800, "standard": 3500, "deep": 6000}[request.length.value],
    )


def _stable_final_output_budget(request: ReportRequest, configured_limit: int) -> int:
    if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
        return min(
            configured_limit,
            {"concise": 2000, "standard": 2800, "deep": 3300}[request.length.value],
        )
    if request.report_type == ReportType.MATCH_PREDICTION:
        return min(configured_limit, 3000)
    return min(
        configured_limit,
        {"concise": 1600, "standard": 2600, "deep": 3200}[request.length.value],
    )


def _is_recoverable_final_provider_error(exc: LLMProviderError) -> bool:
    return exc.kind in {"timeout", "transient", "rate_limit", "invalid_response"}


def _append_report_warning(report: GeneratedReport, warning: str) -> None:
    if warning in report.warnings:
        return
    if len(report.warnings) < 12:
        report.warnings.append(warning)
    elif report.warnings:
        report.warnings[-1] = warning


NOISY_READER_WARNING_TERMS = (
    "关键画面需人工补充",
    "GIF/比赛动图",
    "资料覆盖偏薄",
    "覆盖有限",
    "发布前建议",
    "发布前需",
    "发布前请",
    "复核官方公告",
    "无证据冲突",
    "稳定合稿",
    "模型连接异常",
    "高思考",
    "建议关注后续",
    "建议人工",
    "人工补充",
    "未发现发布会",
    "缺乏第三方独立核实",
)


def _clean_report_warnings(warnings: list[str]) -> list[str]:
    cleaned = []
    for warning in warnings:
        if any(term in warning for term in NOISY_READER_WARNING_TERMS):
            continue
        if _is_bad_critical_entity_warning(warning):
            continue
        if warning not in cleaned:
            cleaned.append(warning)
        if len(cleaned) >= 12:
            break
    return cleaned


CRITICAL_ENTITY_UNCERTAINTY_RE = re.compile(
    r"(若塔|Diogo\s+Jota|Jota|迪奥戈)[^。；;.!?]*(?:后续证实|等待|未确认|尚未确认|缺乏.*核实)",
    re.I,
)


def _is_bad_critical_entity_warning(warning: str) -> bool:
    return bool(CRITICAL_ENTITY_UNCERTAINTY_RE.search(warning))


def _merge_media_assets(
    first: list[MediaAsset], second: list[MediaAsset]
) -> list[MediaAsset]:
    merged: list[MediaAsset] = []
    seen: set[str] = set()
    has_cover = False
    for asset in [*first, *second]:
        key = str(asset.url)
        if key in seen:
            continue
        if asset.placement == "report_cover":
            if has_cover:
                continue
            has_cover = True
        seen.add(key)
        merged.append(asset)
        if len(merged) >= 8:
            break
    return merged


def _referenced_evidence_ids(report: GeneratedReport) -> set[str]:
    referenced: set[str] = set()
    for section in report.sections:
        referenced.update(section.evidence_ids)
    for spotlight in report.enrichment.player_spotlights:
        referenced.update(spotlight.evidence_ids)
    for event in report.enrichment.match_timeline:
        referenced.update(event.evidence_ids)
    return referenced


def _report_visible_text(report: GeneratedReport) -> str:
    return " ".join(
        [
            report.title,
            report.executive_summary,
            *[
                f"{section.heading} {section.body}"
                for section in report.sections
            ],
        ]
    ).casefold()


def _asset_target_is_referenced(asset: MediaAsset, report_text: str) -> bool:
    target_text = f"{asset.target or ''} {asset.title}".strip()
    tokens = [
        token.casefold()
        for token in re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’.-]{2,}", target_text)
        if token.casefold()
        not in {
            "the",
            "and",
            "for",
            "with",
            "football",
            "world",
            "cup",
            "goal",
            "official",
            "highlight",
            "highlights",
        }
    ]
    if len(tokens) >= 2 and all(token in report_text for token in tokens[:2]):
        return True
    return bool(target_text and target_text.casefold() in report_text)


def _asset_target_title_is_plausible(asset: MediaAsset) -> bool:
    if asset.asset_type != "image" or not asset.target:
        return True
    target = asset.target.casefold()
    metadata = f"{asset.title} {asset.attribution}".casefold()
    if re.search(r"stadium|estadio|azteca|球场|体育场", target, re.I):
        return bool(
            re.search(r"stadium|estadio|azteca|mexico city|球场|体育场", metadata, re.I)
        )
    if asset.placement == "spotlight":
        tokens = [
            token.casefold()
            for token in re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’.-]{2,}", asset.target)
        ]
        if len(tokens) >= 2:
            return tokens[-1] in metadata or " ".join(tokens[:2]) in metadata
    return True


def _filter_prefetched_media_assets(
    assets: list[MediaAsset], report: GeneratedReport
) -> list[MediaAsset]:
    referenced = _referenced_evidence_ids(report)
    report_text = _report_visible_text(report)
    primary_ids = set(report.sections[0].evidence_ids) if report.sections else set()
    timeline_ids: set[str] = set()
    for event in report.enrichment.match_timeline:
        timeline_ids.update(event.evidence_ids)
    filtered: list[MediaAsset] = []
    for asset in assets:
        if not _asset_target_title_is_plausible(asset):
            continue
        if (
            asset.placement == "report_cover"
            and asset.evidence_ids
            and not primary_ids.intersection(asset.evidence_ids)
        ):
            continue
        if asset.evidence_ids and referenced.intersection(asset.evidence_ids):
            if asset.placement == "timeline" and not timeline_ids.intersection(
                asset.evidence_ids
            ):
                filtered.append(asset.model_copy(update={"placement": "section"}))
            else:
                filtered.append(asset)
        elif asset.asset_type == "image" and _asset_target_is_referenced(
            asset, report_text
        ):
            filtered.append(asset)
    return filtered[:8]


def _ensure_prediction_analysis(report: GeneratedReport) -> None:
    prediction = report.prediction
    if prediction is None or prediction.analysis_process:
        return
    cited = [
        *(prediction.supporting_factors[:2]),
        *(prediction.counter_factors[:1]),
    ]
    if not cited:
        return
    prediction.analysis_process = [
        EvidenceFactor(
            claim="先读取赛前证据包，区分已确认事实、来源观点和未知项。",
            evidence_ids=cited[0].evidence_ids,
        ),
        EvidenceFactor(
            claim=(
                "再对支持因素和反方因素分别加权，避免只按单一标题给出胜负倾向。"
            ),
            evidence_ids=list(
                dict.fromkeys(eid for item in cited for eid in item.evidence_ids)
            ),
        ),
        EvidenceFactor(
            claim="最后把不确定性反映到胜平负概率和置信度，而不是给确定赛果。",
            evidence_ids=cited[-1].evidence_ids,
        ),
    ]


def _infer_team(text: str) -> str | None:
    for team in TEAM_WORDS:
        if re.search(rf"\b{re.escape(team)}\b", text, re.I):
            return team
    return None


def _goal_method(text: str) -> str:
    lowered = text.casefold()
    if "penalty" in lowered:
        return "点球"
    if "header" in lowered or "headed" in lowered:
        return "头球"
    if "free-kick" in lowered or "free kick" in lowered:
        return "任意球"
    return "完成进球"


def _clean_player_name(value: str) -> str:
    value = value.strip(" .,:;")
    if ". " in value:
        value = value.rsplit(". ", 1)[-1]
    return re.split(
        r"\s+(?:gave|gives|made|makes|levelled|leveled|equalised|equalized|"
        r"opened|put|scored|scores|netted|struck|converted|was|were|sent|sends|"
        r"helped|helps|with|after|as)\b",
        value,
        maxsplit=1,
        flags=re.I,
    )[0].strip()


def _score_after(text: str) -> str | None:
    match = SCORE_RE.search(text)
    return match.group("score") if match else None


def _source_scoreline(evidence: list[Evidence]) -> str | None:
    counts: dict[str, int] = {}
    for item in evidence:
        text = f"{item.title} {item.summary}"
        for match in VISIBLE_SCORE_RE.finditer(text):
            home = int(match.group("home"))
            away = int(match.group("away"))
            if home > 15 or away > 15:
                continue
            score = f"{home}-{away}"
            counts[score] = counts.get(score, 0) + 1
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def _teams_in_text(text: str) -> set[str]:
    folded = text.casefold()
    teams: set[str] = set()
    for team, aliases in TEAM_ALIASES.items():
        for alias in aliases:
            if (
                alias in text
                if re.search(r"[\u4e00-\u9fff]", alias)
                else alias.casefold() in folded
            ):
                teams.add(team)
                break
    return teams


def _teams_in_text_ordered(text: str) -> list[str]:
    folded = text.casefold()
    matches: list[tuple[int, str]] = []
    for team, aliases in TEAM_ALIASES.items():
        positions = [
            text.find(alias)
            if re.search(r"[\u4e00-\u9fff]", alias)
            else folded.find(alias.casefold())
            for alias in aliases
        ]
        positions = [position for position in positions if position >= 0]
        if positions:
            matches.append((min(positions), team))
    return [team for _position, team in sorted(matches)]


def _source_team_scores(evidence: list[Evidence]) -> dict[str, int]:
    scores: dict[str, int] = {}
    for item in evidence:
        for text in (item.title, item.summary):
            for match in VISIBLE_SCORE_RE.finditer(text):
                before = _teams_in_text_ordered(text[: match.start()])
                after = _teams_in_text_ordered(text[match.end() :])
                if not before or not after:
                    continue
                home_team = before[-1]
                away_team = after[0]
                if home_team == away_team:
                    continue
                scores[home_team] = int(match.group("home"))
                scores[away_team] = int(match.group("away"))
    return scores


def _evidence_mentions_teams(item: Evidence, teams: set[str]) -> bool:
    text = f"{item.title} {item.summary}"
    return teams.issubset(_teams_in_text(text))


def _source_scoreline_for_teams(
    evidence: list[Evidence], teams: set[str]
) -> str | None:
    if len(teams) < 2:
        return None
    scoped = [item for item in evidence if _evidence_mentions_teams(item, teams)]
    return _source_scoreline(scoped)


def _replace_conflicting_scores(text: str, source_score: str) -> str:
    def replace(match: re.Match[str]) -> str:
        score = f"{int(match.group('home'))}-{int(match.group('away'))}"
        return match.group(0) if score == source_score else source_score

    return VISIBLE_SCORE_RE.sub(replace, text)


def _repair_score_orientation(text: str, evidence: list[Evidence]) -> str:
    team_scores = _source_team_scores(evidence)
    if len(team_scores) < 2:
        return text
    teams = _teams_in_text_ordered(text)
    if len(teams) < 2:
        return text
    first, second = teams[0], teams[1]
    if first not in team_scores or second not in team_scores:
        return text
    expected = f"{team_scores[first]}-{team_scores[second]}"
    return VISIBLE_SCORE_RE.sub(expected, text, count=1)


def _repair_scorelines_by_sentence(text: str, evidence: list[Evidence]) -> str:
    pieces = re.split(r"(?<=[。！？.!?；;，,])\s*", text)
    repaired: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        oriented = _repair_score_orientation(piece, evidence)
        if oriented != piece:
            repaired.append(oriented)
            continue
        source_score = _source_scoreline_for_teams(evidence, _teams_in_text(piece))
        repaired.append(
            _replace_conflicting_scores(piece, source_score)
            if source_score
            else piece
        )
    return "".join(repaired)


def _repair_report_scorelines(report: GeneratedReport, request: ReportRequest) -> None:
    evidence_by_id = {item.id: item for item in request.evidence}
    report.title = _repair_scorelines_by_sentence(report.title, request.evidence)
    report.executive_summary = _repair_scorelines_by_sentence(
        report.executive_summary, request.evidence
    )
    for section in report.sections:
        cited = [
            evidence_by_id[evidence_id]
            for evidence_id in section.evidence_ids
            if evidence_id in evidence_by_id
        ]
        source_score = _source_scoreline(cited)
        original = section.body
        section.heading = _repair_scorelines_by_sentence(section.heading, cited)
        section.body = _repair_scorelines_by_sentence(section.body, cited)
        if section.body == original and source_score:
            section.body = _replace_conflicting_scores(section.body, source_score)


def _timeline_event_from_match(
    *,
    minute: str,
    player: str,
    text: str,
    evidence_id: str,
) -> MatchTimelineEvent:
    method = _goal_method(text)
    score = _score_after(text)
    team = _infer_team(text)
    player_name = _clean_player_name(player)
    score_part = f"，比分来到 {score}" if score else ""
    return MatchTimelineEvent(
        minute=minute,
        event_type="penalty" if method == "点球" else "goal",
        player=player_name,
        team=team,
        score_after=score,
        description=f"第 {minute} 分钟，{player_name} {method}{score_part}。",
        evidence_ids=[evidence_id],
    )


def _inject_deterministic_match_timeline(
    report: GeneratedReport, request: ReportRequest
) -> None:
    existing_ids = {
        (event.minute, tuple(event.evidence_ids))
        for event in report.enrichment.match_timeline
    }
    additions: list[MatchTimelineEvent] = []
    for item in request.evidence:
        text = f"{item.title}. {item.summary}"
        for pattern in (
            MINUTE_GOAL_BY_RE,
            PLAYER_SCORED_MINUTE_RE,
            CN_MINUTE_PLAYER_ACTION_RE,
        ):
            for match in pattern.finditer(text):
                minute = match.group("minute")
                if "+" in match.group(0) and "+" not in minute:
                    extra = re.search(
                        rf"{re.escape(minute)}\+(\d{{1,2}})",
                        match.group(0),
                    )
                    if extra:
                        minute = f"{minute}+{extra.group(1)}"
                event = _timeline_event_from_match(
                    minute=minute,
                    player=match.group("player"),
                    text=text,
                    evidence_id=item.id,
                )
                key = (event.minute, tuple(event.evidence_ids))
                if key not in existing_ids:
                    additions.append(event)
                    existing_ids.add(key)
                if len(additions) >= 6:
                    break
            if len(additions) >= 6:
                break
        if len(additions) >= 6:
            break
        if re.search(r"\bVAR\b", text, re.I) and re.search(
            r"denied|disallowed|ruled out|equaliser|equalizer", text, re.I
        ):
            event = MatchTimelineEvent(
                minute="终场",
                event_type="key_moment",
                player=None,
                team=_infer_team(text),
                score_after=_score_after(text),
                description=(
                    "终场前后，VAR 介入改变了扳平球判定，成为比赛收束的关键画面。"
                ),
                evidence_ids=[item.id],
            )
            key = (event.minute, tuple(event.evidence_ids))
            if key not in existing_ids:
                additions.append(event)
                existing_ids.add(key)
        if len(additions) >= 6:
            break
    if additions:
        report.enrichment.match_timeline = [
            *report.enrichment.match_timeline,
            *additions,
        ][:20]


def _event_minute(match: re.Match[str]) -> str:
    minute = match.group("minute")
    if "+" in minute:
        return minute
    if "+" in match.group(0):
        extra = re.search(rf"{re.escape(minute)}\+(\d{{1,2}})", match.group(0))
        if extra:
            return f"{minute}+{extra.group(1)}"
    return minute


def _timeline_goal_event_from_match(
    *,
    minute: str,
    player: str,
    text: str,
    evidence_text: str,
    evidence_id: str,
) -> MatchTimelineEvent:
    method = _goal_method(text)
    score = _score_after(text)
    player_name = _clean_player_name(player)
    score_part = f"，比分来到 {score}" if score else ""
    return MatchTimelineEvent(
        minute=minute,
        event_type="penalty" if method == "点球" else "goal",
        player=player_name,
        team=_infer_team(evidence_text),
        score_after=score,
        description=f"第 {minute} 分钟，{player_name} {method}{score_part}。",
        evidence_ids=[evidence_id],
    )


def _timeline_card_event_from_match(
    *, minute: str, player: str, evidence_text: str, evidence_id: str
) -> MatchTimelineEvent:
    player_name = _clean_player_name(player)
    return MatchTimelineEvent(
        minute=minute,
        event_type="card",
        player=player_name,
        team=_infer_team(evidence_text),
        score_after=_score_after(evidence_text),
        description=f"第 {minute} 分钟，{player_name} 出现红黄牌或被罚下事件。",
        evidence_ids=[evidence_id],
    )


def _timeline_event_key(event: MatchTimelineEvent) -> tuple[str, str | None, str]:
    return (event.minute, event.player, event.evidence_ids[0])


def _add_timeline_event(
    additions: list[MatchTimelineEvent],
    seen: set[tuple[str, str | None, str]],
    event: MatchTimelineEvent,
) -> None:
    key = _timeline_event_key(event)
    if key in seen:
        return
    additions.append(event)
    seen.add(key)


def _inject_additional_match_timeline(
    report: GeneratedReport, request: ReportRequest
) -> None:
    seen = {_timeline_event_key(event) for event in report.enrichment.match_timeline}
    additions: list[MatchTimelineEvent] = []
    for item in request.evidence:
        evidence_text = f"{item.title}. {item.summary}"
        for pattern in (PLAYER_RESULT_MINUTE_RE,):
            for match in pattern.finditer(evidence_text):
                event = _timeline_goal_event_from_match(
                    minute=_event_minute(match),
                    player=match.group("player"),
                    text=match.group(0),
                    evidence_text=evidence_text,
                    evidence_id=item.id,
                )
                _add_timeline_event(additions, seen, event)
                if len(additions) >= 8:
                    break
        for match in PLAYER_CARD_MINUTE_RE.finditer(evidence_text):
            event = _timeline_card_event_from_match(
                minute=_event_minute(match),
                player=match.group("player"),
                evidence_text=evidence_text,
                evidence_id=item.id,
            )
            _add_timeline_event(additions, seen, event)
            if len(additions) >= 8:
                break
        if len(additions) >= 8:
            break
    if additions:
        report.enrichment.match_timeline = [
            *report.enrichment.match_timeline,
            *additions,
        ][:20]


def _looks_like_player_name(value: str) -> bool:
    folded = value.casefold()
    if not value or folded in {team.casefold() for team in TEAM_WORDS}:
        return False
    return len(value.split()) <= 4 and not re.search(
        r"\b(?:world|cup|fifa|official|football|match|goal)\b", folded
    )


def _inject_deterministic_player_spotlights(
    report: GeneratedReport, request: ReportRequest
) -> None:
    if len(report.enrichment.player_spotlights) >= 6:
        return
    existing = {
        spotlight.name.casefold() for spotlight in report.enrichment.player_spotlights
    }
    evidence_by_id = {item.id: item for item in request.evidence}
    metric_by_name: dict[str, PlayerMetric] = {}
    for item in request.evidence:
        text = f"{item.title}. {item.summary}"
        for match in PLAYER_SEASON_GOAL_RE.finditer(text):
            name = _clean_player_name(match.group("player"))
            value = match.group("value")
            if _looks_like_player_name(name):
                metric_by_name.setdefault(
                    name.casefold(), PlayerMetric(label="进球数", value=value)
                )

    additions: list[PlayerSpotlight] = []
    for event in report.enrichment.match_timeline:
        if not event.player or not _looks_like_player_name(event.player):
            continue
        key = event.player.casefold()
        if key in existing:
            continue
        source = next(
            (
                evidence_by_id[item_id]
                for item_id in event.evidence_ids
                if item_id in evidence_by_id
            ),
            None,
        )
        if source is None:
            continue
        additions.append(
            PlayerSpotlight(
                name=event.player,
                media_search_name=event.player,
                narrative=(
                    f"引用资料将 {event.player} 与本场关键事件联系在一起；"
                    "除证据已写出的进球、分钟或比分外，其他履历信息保持未知。"
                ),
                metrics=[metric_by_name[key]] if key in metric_by_name else [],
                evidence_ids=event.evidence_ids,
            )
        )
        existing.add(key)
        if len(report.enrichment.player_spotlights) + len(additions) >= 6:
            break
    if additions:
        report.enrichment.player_spotlights = [
            *report.enrichment.player_spotlights,
            *additions,
        ][:6]


def _assign_section_categories(
    report: GeneratedReport, request: ReportRequest | None = None
) -> None:
    evidence_by_id = {item.id: item for item in request.evidence} if request else {}
    for section in report.sections:
        text = f"{section.heading} {section.body}"
        cited = [
            evidence_by_id[evidence_id]
            for evidence_id in section.evidence_ids
            if evidence_id in evidence_by_id
        ]
        has_transfer_source = not request or any(
            is_transfer_evidence(item) for item in cited
        )
        has_match_source = not request or any(
            is_completed_match_evidence(item) for item in cited
        )
        can_reclassify = (
            section.category is None
            or section.category == "context"
            or (
                section.category == "transfer"
                and request is not None
                and not has_transfer_source
            )
            or (
                section.category == "match"
                and request is not None
                and not has_match_source
            )
        )
        if not can_reclassify:
            continue
        if TRANSFER_CATEGORY_RE.search(text) and has_transfer_source:
            section.category = "transfer"
        elif OFF_FIELD_CATEGORY_RE.search(text):
            section.category = "off_field"
        elif MATCH_CATEGORY_RE.search(text) and has_match_source:
            section.category = "match"
        else:
            section.category = "context"


def _append_coverage_warnings(
    report: GeneratedReport,
    request: ReportRequest,
    *,
    statistical_baseline: object | None,
) -> None:
    for warning in request.collection_warnings:
        _append_report_warning(report, warning)
    independent = {
        item.source_independence_key or item.source_id for item in request.evidence
    }
    verified_independent = {
        item.source_independence_key or item.source_id
        for item in request.evidence
        if item.verification_status != "unverified_lead"
    }
    if len(independent) < 2:
        _append_report_warning(
            report,
            "本次资料覆盖少于两个独立来源，适合做初步整理，发布前建议补充核验。",
        )
    elif len(verified_independent) < 2:
        _append_report_warning(
            report,
            "本次有多条线索但已核验独立来源不足两家，传闻和发现层内容请谨慎发布。",
        )
    if (
        request.report_type == ReportType.MATCH_PREDICTION
        and statistical_baseline is None
    ):
        _append_report_warning(
            report,
            "结构化近期赛果不足，未展示可复现统计基线；当前概率主要是证据型 AI 研判。",
        )
    if (
        request.report_type == ReportType.MATCH_PREDICTION
        and report.prediction
        and not report.prediction.external_predictions
    ):
        _append_report_warning(
            report,
            "输入证据中没有可引用的外部公开预测，系统未补造 Opta、FIFA "
            "或媒体概率。",
        )


def _daily_editor_outline(request: ReportRequest, desk_drafts: list[DeskDraft]) -> str:
    """Build a deterministic synthesis scaffold before the final LLM pass."""
    clusters: dict[str, dict[str, object]] = {}
    for item in request.evidence:
        if not item.story_cluster_id:
            continue
        cluster = clusters.setdefault(
            item.story_cluster_id,
            {
                "evidence_ids": [],
                "sources": set(),
                "titles": [],
                "unverified": 0,
            },
        )
        cluster["evidence_ids"].append(item.id)  # type: ignore[index,union-attr]
        cluster["sources"].add(  # type: ignore[union-attr]
            item.source_independence_key or item.source_id
        )
        cluster["titles"].append(item.title)  # type: ignore[index,union-attr]
        if item.verification_status == "unverified_lead":
            cluster["unverified"] = int(cluster["unverified"]) + 1

    cluster_rows = []
    for cluster_id, cluster in sorted(
        clusters.items(),
        key=lambda pair: len(pair[1]["evidence_ids"]),  # type: ignore[arg-type]
        reverse=True,
    )[:8]:
        cluster_rows.append(
            {
                "cluster_id": cluster_id,
                "evidence_ids": cluster["evidence_ids"][:8],
                "independent_sources": len(cluster["sources"]),
                "unverified_leads": cluster["unverified"],
                "sample_titles": cluster["titles"][:3],
            }
        )

    columns = []
    for draft in desk_drafts:
        evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for section in draft.sections
                for evidence_id in section.evidence_ids
            )
        )
        columns.append(
            {
                "desk": draft.desk,
                "heading": draft.heading,
                "section_count": len(draft.sections),
                "evidence_ids": evidence_ids[:12],
                "warnings": draft.warnings[:4],
            }
        )

    outline = {
        "editorial_contract": [
            "整合为《今日球脉》，按 leader_editorial_plan 的优先级和栏目边界合稿。",
            "总标题只概括最高优先级主线，不把事实护栏、悼念背景或低优先级线索写成标题钩子。",
            "同一 story_cluster_id 只写一次，保留不同来源的分歧。",
            "unverified_lead 必须继续标注为传闻/线索，不得升级为官宣。",
            "不要新增证据外事实；引用只能使用 evidence_ids。",
        ],
        "leader_editorial_plan": [
            column.model_dump(mode="json") for column in request.editorial_plan
        ],
        "columns": columns,
        "story_clusters": cluster_rows,
    }
    return json.dumps(outline, ensure_ascii=False)


def _attempt_summary(output: dict[str, object]) -> dict[str, object]:
    sections = output.get("sections")
    section_rows = []
    if isinstance(sections, list):
        for section in sections[:8]:
            if not isinstance(section, dict):
                continue
            section_rows.append(
                {
                    "heading": _shorten(str(section.get("heading") or ""), 120),
                    "evidence_ids": section.get("evidence_ids") or [],
                    "body_preview": _shorten(str(section.get("body") or ""), 320),
                }
            )
    return {
        "title": _shorten(str(output.get("title") or ""), 160),
        "section_count": len(sections) if isinstance(sections, list) else 0,
        "sections": section_rows,
    }


def _desk_draft_trace_payload(desk_drafts: list[DeskDraft]) -> dict[str, object]:
    category_counts: dict[str, int] = {}
    for draft in desk_drafts:
        for section in draft.sections:
            category = section.category or "uncategorized"
            category_counts[category] = category_counts.get(category, 0) + 1
    return {
        "desk_count": len(desk_drafts),
        "category_counts": category_counts,
        "desks": [
            {
                "desk": draft.desk,
                "heading": draft.heading,
                "section_count": len(draft.sections),
                "section_categories": [
                    section.category or "uncategorized"
                    for section in draft.sections[:6]
                ],
                "evidence_ids": list(
                    dict.fromkeys(
                        evidence_id
                        for section in draft.sections
                        for evidence_id in section.evidence_ids
                    )
                )[:12],
                "warnings": draft.warnings[:4],
            }
            for draft in desk_drafts[:8]
        ],
    }


def _claim_revision_hints(
    errors: list[str], request: ReportRequest, output: dict[str, object]
) -> list[str]:
    numeric_errors = [error for error in errors if "numeric claim" in error]
    if not numeric_errors:
        return []
    hints = [
        "数字 claim 修复要求：不要凭足球常识补 16强、年份、分钟、比分、金额或出场数。",
        "对每个 numeric claim 错误，只能三选一：删除该数字；"
        "改成无数字表述；或把该小节 evidence_ids 换成含有该数字的证据。",
    ]
    evidence_by_id = {item.id: item for item in request.evidence}
    sections = output.get("sections")
    if isinstance(sections, list):
        for section in sections[:8]:
            if not isinstance(section, dict):
                continue
            evidence_ids = [
                str(item) for item in section.get("evidence_ids") or []
                if str(item) in evidence_by_id
            ]
            unsupported = unsupported_numeric_tokens(
                str(section.get("body") or ""), request.evidence, evidence_ids
            )
            if unsupported:
                hints.append(
                    f"小节《{section.get('heading') or ''}》的这些数字"
                    "没有被当前引用支持："
                    + "、".join(unsupported[:8])
                )
    ledger = build_numeric_claim_ledger(request.evidence, max_entries=24)
    if ledger:
        hints.append(
            "可用数字 claim ledger 摘要："
            + json.dumps(ledger[:24], ensure_ascii=False)
        )
    return hints[:8]


def _sanitize_section_claims(
    section: ReportSection, request: ReportRequest
) -> tuple[ReportSection, list[str]]:
    body = re.sub(r"[“”‘’\"]", "", section.body)
    if body != section.body:
        section = section.model_copy(update={"body": body})
    sanitized_body, changed = sanitize_text_against_evidence(
        section.body, request.evidence, section.evidence_ids
    )
    if not changed:
        return section, []
    return section.model_copy(update={"body": sanitized_body}), changed


def _clean_fact_from_evidence(item: Evidence) -> str:
    title = clean_evidence_text(item.title)
    summary = clean_evidence_text(item.summary)
    if summary and summary != title:
        return f"{item.source_name}资料显示：{title}。摘要信息：{summary}。"
    return f"{item.source_name}资料显示：{title}。"


def _story_groups_for_column(
    request: ReportRequest, column: EditorialColumnPlan
) -> list[list[Evidence]]:
    by_id = {item.id: item for item in request.evidence}
    groups: dict[str, list[Evidence]] = {}
    for evidence_id in column.evidence_ids:
        item = by_id.get(evidence_id)
        if item is None:
            continue
        key = item.story_cluster_id or item.id
        groups.setdefault(key, []).append(item)
    return list(groups.values())


def _evidence_has_transfer_signal(item: Evidence) -> bool:
    return is_transfer_evidence(item)


def _fallback_category_for_group(
    column: EditorialColumnPlan, group: list[Evidence]
) -> str:
    if column.specialist_group == "match_report":
        if any(is_completed_match_evidence(item) for item in group):
            return "match"
        return "off_field"
    if column.specialist_group == "transfer_intel":
        if any(_evidence_has_transfer_signal(item) for item in group):
            return "transfer"
        text = " ".join(f"{item.title} {item.summary}" for item in group)
        return "off_field" if OFF_FIELD_CATEGORY_RE.search(text) else "context"
    if column.specialist_group == "off_field":
        return "off_field"
    return column.category


def _fallback_heading_prefix(column: EditorialColumnPlan, category: str) -> str:
    if column.specialist_group == "match_report" and category != "match":
        return "赛前观察" if category == "off_field" else "背景脉络"
    if column.specialist_group == "transfer_intel" and category != "transfer":
        return "场外焦点" if category == "off_field" else "背景脉络"
    if column.specialist_group == "off_field":
        return "场外焦点"
    return column.title


def _section_category_counts(sections: list[ReportSection]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for section in sections:
        category = section.category or "uncategorized"
        counts[category] = counts.get(category, 0) + 1
    return counts


def _column_for_desk(
    request: ReportRequest, desk: str
) -> EditorialColumnPlan | None:
    return next(
        (column for column in request.editorial_plan if column.column_id == desk),
        None,
    )


def _section_matches_column(
    section: ReportSection, column: EditorialColumnPlan
) -> bool:
    if not set(column.evidence_ids).intersection(section.evidence_ids):
        return False
    if column.specialist_group == "context":
        return True
    if column.specialist_group == "match_report":
        return section.category == "match"
    if column.specialist_group == "transfer_intel":
        return section.category == "transfer"
    if column.specialist_group == "off_field":
        return section.category == "off_field"
    if column.category:
        return section.category in {None, column.category}
    return True


def _normalize_daily_section_category(
    section: ReportSection,
    request: ReportRequest,
    column: EditorialColumnPlan | None,
) -> ReportSection:
    by_id = {item.id: item for item in request.evidence}
    cited = [
        by_id[evidence_id]
        for evidence_id in section.evidence_ids
        if evidence_id in by_id
    ]
    if not cited:
        return section
    text = f"{section.heading} {section.body}"
    category = section.category
    if column and column.specialist_group == "match_report":
        category = (
            "match"
            if any(is_completed_match_evidence(item) for item in cited)
            else "off_field"
        )
    elif column and column.specialist_group == "transfer_intel":
        category = (
            "transfer"
            if any(_evidence_has_transfer_signal(item) for item in cited)
            else "off_field"
            if OFF_FIELD_CATEGORY_RE.search(text)
            else "context"
        )
    elif column and column.specialist_group == "off_field":
        category = "off_field"
    elif category in {None, "context"}:
        if TRANSFER_CATEGORY_RE.search(text):
            category = "transfer"
        elif OFF_FIELD_CATEGORY_RE.search(text):
            category = "off_field"
        else:
            category = category or "context"
    return section.model_copy(update={"category": category})


def _fallback_sections_for_column(
    request: ReportRequest, column: EditorialColumnPlan
) -> list[ReportSection]:
    raw_groups = _story_groups_for_column(request, column)
    groups = raw_groups
    if column.specialist_group == "match_report":
        completed_groups = [
            [item for item in group if is_completed_match_evidence(item)]
            for group in raw_groups
        ]
        groups = [group for group in completed_groups if group] or raw_groups
    if not groups:
        return []
    needed = 1
    if column.specialist_group in {"match_report", "transfer_intel"}:
        needed = min(4, max(1, len(groups)))
    sections: list[ReportSection] = []
    for index, group in enumerate(groups[:needed], start=1):
        evidence_ids = [item.id for item in group[:3]]
        lead = group[0]
        body = " ".join(_clean_fact_from_evidence(item) for item in group[:2])
        category = _fallback_category_for_group(column, group)
        heading_prefix = _fallback_heading_prefix(column, category)
        section = ReportSection(
            heading=(
                heading_prefix
                if needed == 1
                else (
                    f"{heading_prefix}｜"
                    f"{_shorten(clean_evidence_text(lead.title), 48)}"
                )
            ),
            body=body,
            evidence_ids=evidence_ids,
            category=category,
        )
        sanitized, _changed = _sanitize_section_claims(section, request)
        sections.append(sanitized)
        if index >= needed:
            break
    return sections


def _minimal_daily_sections(request: ReportRequest) -> list[ReportSection]:
    sections: list[ReportSection] = []
    for item in request.evidence[:6]:
        section = ReportSection(
            heading=f"证据摘要｜{_shorten(clean_evidence_text(item.title), 48)}",
            body=_clean_fact_from_evidence(item),
            evidence_ids=[item.id],
            category="context",
        )
        sanitized, _changed = _sanitize_section_claims(section, request)
        sections.append(sanitized)
    return sections


def _safe_daily_sections(
    request: ReportRequest, desk_drafts: list[DeskDraft]
) -> tuple[list[ReportSection], list[str]]:
    warnings: list[str] = []
    sections: list[ReportSection] = []
    for draft in desk_drafts:
        column = _column_for_desk(request, draft.desk)
        for section in draft.sections:
            normalized = _normalize_daily_section_category(section, request, column)
            sanitized, changed = _sanitize_section_claims(normalized, request)
            if changed:
                warnings.append(
                    f"保守合稿已移除或泛化无证数字：{sanitized.heading} "
                    + "、".join(changed[:6])
                )
            sections.append(sanitized)
    for column in request.editorial_plan or _fallback_daily_columns(request):
        matches = [
            section
            for section in sections
            if _section_matches_column(section, column)
            or (
                column.specialist_group in {"match_report", "transfer_intel"}
                and set(column.evidence_ids).intersection(section.evidence_ids)
                and column.specialist_group == "match_report"
                and not any(
                    is_completed_match_evidence(item)
                    for group in _story_groups_for_column(request, column)
                    for item in group
                )
            )
        ]
        required = 1
        if column.specialist_group == "match_report":
            required = min(max(1, len(_match_story_keys(request, column))), 4)
        if column.specialist_group == "transfer_intel":
            transfer_count = _transfer_story_count(request, column)
            if transfer_count == 0:
                continue
            required = min(max(1, transfer_count), 4)
        if len(matches) >= required:
            continue
        needed = required - len(matches)
        sections.extend(_fallback_sections_for_column(request, column)[:needed])
    if not sections:
        fallback_column = EditorialColumnPlan(
            column_id="evidence_digest",
            title="证据摘要",
            category="context",
            specialist_group="context",
            evidence_ids=[item.id for item in request.evidence[:4]],
        )
        sections.extend(_fallback_sections_for_column(request, fallback_column))
    return sections[:12], warnings[:8]


def _critical_entities_for_request(request: ReportRequest) -> list[CriticalEntity]:
    registry = load_critical_entities()
    evidence_ids = {item.id for item in request.evidence}
    evidence_text = " ".join(
        f"{item.title} {item.summary} {item.source_name}" for item in request.evidence
    ).casefold()
    matches = []
    for entity in registry.entities:
        if f"critical-{entity.id}" in evidence_ids:
            matches.append(entity)
            continue
        aliases = [entity.canonical_name, *entity.aliases]
        if any(alias.casefold() in evidence_text for alias in aliases):
            matches.append(entity)
    return matches


def _critical_status_label(entity: CriticalEntity) -> str:
    if entity.critical_status == "deceased":
        return "去世"
    return entity.critical_status


def _critical_entity_sentence(entity: CriticalEntity) -> str:
    if entity.report_summary:
        return entity.report_summary
    name = entity.preferred_name or entity.canonical_name
    if name != entity.canonical_name:
        name = f"{entity.canonical_name}（{name}）"
    event_date = entity.event_date.date().isoformat()
    return (
        f"{entity.source_name} 确认，{name} 已于 {event_date} "
        f"被官方确认为{_critical_status_label(entity)}。"
    )


def _fallback_daily_columns(request: ReportRequest) -> list[EditorialColumnPlan]:
    buckets: dict[str, list[str]] = {
        "match_report": [],
        "transfer_intel": [],
        "off_field": [],
        "context": [],
    }
    for item in request.evidence:
        text = f"{item.title} {item.summary}".casefold()
        if re.search(r"transfer|signing|bid|deal|agreement|medical", text):
            buckets["transfer_intel"].append(item.id)
        elif is_upcoming_match_evidence(item) or re.search(
            r"politics|fans|pubs|viewership|flag|city", text
        ):
            buckets["off_field"].append(item.id)
        elif is_completed_match_evidence(item):
            buckets["match_report"].append(item.id)
        elif evidence_is_match_like(item):
            buckets["context"].append(item.id)
        else:
            buckets["context"].append(item.id)
    specs = [
        ("match_report", "赛场主线", "match", 1),
        ("transfer_intel", "转会市场", "transfer", 2),
        ("off_field", "场外与赛程", "off_field", 3),
        ("context", "背景脉络", "context", 4),
    ]
    columns: list[EditorialColumnPlan] = []
    for group, title, category, priority in specs:
        ids = buckets[group]
        if ids:
            columns.append(
                EditorialColumnPlan(
                    column_id=group,
                    title=title,
                    category=category,
                    specialist_group=group,
                    priority=priority,
                    evidence_ids=ids[:8],
                    coverage_requirements=_coverage_requirements_for(group),
                    instructions=_specialist_instruction(group),
                )
            )
    return columns


def _coverage_requirements_for(group: str) -> list[str]:
    return {
        "match_report": [
            "每场比赛独立成段",
            "包含对阵双方和最终比分或结果",
            "只有已完赛证据才能进入战报；赛前、抵达、开球安排和敌意接待必须进入场外或背景",
            "没有事件细节时只写证据支持的结果，不得用未知占位句填充正文",
            "说明晋级、淘汰或下一场影响",
            "绑定战报图、官方高光或人工补图目标",
        ],
        "transfer_intel": [
            "每条转会独立成段",
            "写清球员、当前球队、目标球队和转会阶段",
            "金额、合同年限、体检时间没有证据则明确未知",
            "区分 publisher_report 与 unverified_lead",
        ],
        "coach_tactics": [
            "教练成绩或年份必须来自证据",
            "非官方离任只写成传闻",
        ],
        "player_profile": [
            "球员位置、现俱乐部和数据必须来自证据",
            "人物图只能用许可图片或人工补图目标",
        ],
        "off_field": [
            "说明事件与比赛日、球迷、转播或城市影响的关系",
            "判断性内容必须引用来源观点",
        ],
        "context": ["只补背景脉络和未知项", "不得抢占战报或转会主标题"],
    }.get(group, ["按证据整理栏目，事实、传闻和未知项必须分开。"])


def _specialist_instruction(group: str) -> str:
    return {
        "match_report": (
            "你是战报小组。每场比赛分开处理，优先找比分、进球者、分钟、进球方式、"
            "VAR/红牌/点球等转折和下一轮影响。没有结构化事件时不要编分钟，"
            "也不要用“关键事件待确认”等占位句扩写。赛前材料只能交给场外或背景栏目。"
            "每场至少提出一个图片目标和一个官方高光候选。"
        ),
        "transfer_intel": (
            "你是转会小组。每条转会必须回答：球员是谁、当前球队、目标球队、"
            "阶段、金额/合同/体检是否有证据、上赛季表现或角色。"
            "缺任何一项就列 unknowns。"
        ),
        "coach_tactics": (
            "你是教练与战术小组。区分战术变化、教练履历/成就和离任传闻；"
            "非官方离任不得写成已发生。"
        ),
        "player_profile": (
            "你是人物小组。补位置、俱乐部、国家队、近期表现和证据里的数据，"
            "不凭记忆补身价或赛季统计。"
        ),
        "off_field": (
            "你是场外小组。处理球迷、政治、转播、城市、赛程和商业影响，"
            "不要抢比赛战报或转会标题。"
        ),
        "context": "你是背景小组。只做补充脉络和未知项，不把背景写成头条。",
    }.get(group, "按证据整理栏目，事实、传闻和未知项必须分开。")


def _column_evidence_text(
    request: ReportRequest, column: EditorialColumnPlan
) -> tuple[str, list[str]]:
    evidence_by_id = {item.id: item for item in request.evidence}
    ids = [item for item in column.evidence_ids if item in evidence_by_id]
    if not ids:
        ids = [item.id for item in request.evidence[:4]]
    text = "\n".join(
        (
            f"[{item.id}] [{item.verification_status}/{item.trust_tier}] "
            f"{item.title}: {item.summary}"
        )
        for item in (evidence_by_id[item_id] for item_id in ids)
    )
    return text, ids


def _column_section_matches(
    report: GeneratedReport, column: EditorialColumnPlan
) -> list[int]:
    matches: list[int] = []
    for index, section in enumerate(report.sections):
        if _section_matches_column(section, column):
            matches.append(index)
    return matches


def _match_story_keys(request: ReportRequest, column: EditorialColumnPlan) -> set[str]:
    by_id = {item.id: item for item in request.evidence}
    keys: set[str] = set()
    for evidence_id in column.evidence_ids:
        item = by_id.get(evidence_id)
        if not item:
            continue
        if not is_completed_match_evidence(item):
            continue
        text = f"{item.title} {item.summary}"
        teams = [
            team
            for team in TEAM_WORDS
            if re.search(rf"(?<![A-Za-z]){re.escape(team)}(?![A-Za-z])", text, re.I)
        ]
        if len(teams) >= 2:
            keys.add(" vs ".join(teams[:2]))
            continue
        if re.search(
            r"goal|scored|score|beat|defeat|var|penalty|highlights?|世界杯|进球|战胜",
            text,
            re.I,
        ):
            keys.add(item.story_cluster_id or item.id)
    return keys


def _transfer_story_count(request: ReportRequest, column: EditorialColumnPlan) -> int:
    by_id = {item.id: item for item in request.evidence}
    keys: set[str] = set()
    for evidence_id in column.evidence_ids:
        item = by_id.get(evidence_id)
        if not item:
            continue
        if _evidence_has_transfer_signal(item):
            keys.add(item.story_cluster_id or item.id)
    return len(keys)


MATCH_DETAIL_SOURCE_RE = re.compile(
    r"\b\d{1,2}-\d{1,2}\b|"
    r"\b\d{1,3}(?:\+\d{1,2})?(?:st|nd|rd|th)[-\s]?minute\b|"
    r"\b(?:scored|goal|penalty|sent off|red card|VAR)\b",
    re.I,
)
MATCH_DETAIL_COPY_RE = re.compile(
    r"\b\d{1,2}-\d{1,2}\b|"
    r"\b\d{1,3}(?:\+\d{1,2})?(?:st|nd|rd|th)[-\s]?minute\b|"
    r"第\s*\d{1,3}(?:\+\d{1,2})?\s*分钟|"
    r"进球|破门|点球|红牌|黄牌|VAR",
    re.I,
)


def _match_detail_required(request: ReportRequest, column: EditorialColumnPlan) -> bool:
    by_id = {item.id: item for item in request.evidence}
    return any(
        is_completed_match_evidence(item)
        and MATCH_DETAIL_SOURCE_RE.search(f"{item.title} {item.summary}")
        for evidence_id in column.evidence_ids
        if (item := by_id.get(evidence_id)) is not None
    )


def _matched_sections_have_match_detail(
    report: GeneratedReport, section_indexes: list[int]
) -> bool:
    return any(
        MATCH_DETAIL_COPY_RE.search(
            f"{report.sections[index].heading} {report.sections[index].body}"
        )
        for index in section_indexes
    )


def _daily_coverage_gaps(report: GeneratedReport, request: ReportRequest) -> list[str]:
    if request.report_type != ReportType.DAILY_FOOTBALL_DIGEST:
        return []
    gaps: list[str] = []
    evidence_by_id = {item.id: item for item in request.evidence}
    for section in report.sections:
        gaps.extend(placeholder_copy_errors(section))
        cited = [
            evidence_by_id[evidence_id]
            for evidence_id in section.evidence_ids
            if evidence_id in evidence_by_id
        ]
        is_upcoming = match_evidence_state(cited) == "upcoming_match"
        has_completed_copy = has_completed_match_claim(
            f"{section.heading} {section.body}"
        )
        if is_upcoming and has_completed_copy:
            gaps.append(
                f"section '{section.heading}' uses pre-match/upcoming evidence "
                "as if the match was already played"
            )
    for column in request.editorial_plan:
        if column.specialist_group == "match_report":
            cited = [
                evidence_by_id[evidence_id]
                for evidence_id in column.evidence_ids
                if evidence_id in evidence_by_id
            ]
            if not completed_match_items(cited):
                gaps.append(
                    f"match column '{column.title}' has no completed-match "
                    "evidence; reroute it to off_field/context or collect "
                    "a real result source"
                )
                continue
        if (
            column.specialist_group == "transfer_intel"
            and _transfer_story_count(request, column) == 0
        ):
            continue
        matches = _column_section_matches(report, column)
        if not matches:
            gaps.append(
                f"缺少 Leader 栏目《{column.title}》；必须写至少一个二级标题"
                "并引用其 evidence_ids。"
            )
            continue
        if column.specialist_group == "match_report":
            match_count = len(_match_story_keys(request, column))
            if match_count >= 2 and len(matches) < min(match_count, 4):
                gaps.append(
                    f"战报栏目《{column.title}》覆盖了 {match_count} 场比赛，"
                    "需要按每场比赛拆成独立二级标题，写清时间、地点、对阵、进球和走势。"
                )
            if _match_detail_required(
                request, column
            ) and not _matched_sections_have_match_detail(report, matches):
                gaps.append(
                    f"match column '{column.title}' cites evidence with score/minute/"
                    "event details, but the rendered section does not include a "
                    "scoreline, minute, goal/card/VAR detail."
                )
        if column.specialist_group == "transfer_intel":
            transfer_count = _transfer_story_count(request, column)
            if transfer_count >= 2 and len(matches) < min(transfer_count, 4):
                gaps.append(
                    f"转会栏目《{column.title}》覆盖了 {transfer_count} 条转会，"
                    "需要按每个转会拆成二级标题，写清当前球队、目标球队、阶段和金额/未知项。"
                )
    return gaps[:6]


class ReportService:
    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        max_output_tokens: int,
        flash_model: str | None = None,
        max_attempts: int = 2,
        youtube_api_key: str | None = None,
        youtube_channel_ids: tuple[str, ...] = (),
        media_enabled: bool = False,
    ) -> None:
        self._provider = provider
        self._model = model
        self._council_enabled = flash_model is not None
        self._flash_model = flash_model or model
        self._max_output_tokens = max_output_tokens
        self._max_attempts = max_attempts
        self._youtube_api_key = youtube_api_key
        self._youtube_channel_ids = youtube_channel_ids
        self._media_enabled = media_enabled

    async def generate(
        self,
        request: ReportRequest,
        max_attempts: int | None = None,
        skill_instructions: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ReportResponse:
        messages = build_messages(request, skill_instructions)
        council_results: list[LLMResult] = []
        desk_drafts: list[DeskDraft] = []
        statistical_baseline = build_statistical_baseline(request.match_context)
        sourced_external_predictions = extract_external_predictions(request.evidence)
        if request.report_type == ReportType.MATCH_PREDICTION:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "确定性统计基线如下。可以比较和解释，但不得修改其数值；"
                        "输出中的 statistical_baseline 请保持 null，Harness 会在"
                        "校验后注入原始计算结果。\n"
                        + (
                            statistical_baseline.model_dump_json()
                            if statistical_baseline
                            else "当前缺少每队至少三场结构化赛果，统计基线不可用。"
                        )
                        + "\n"
                        + PREDICTION_ANALYSIS_CONTRACT
                    ),
                }
            )
        if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
            desk_drafts, council_results = await self._run_daily_desks(request)
            desk_payload = _desk_draft_trace_payload(desk_drafts)
            _emit_progress(
                progress_callback,
                "desk_drafts_ready",
                75,
                {
                    **desk_payload,
                    "checkpoint": {
                        "name": "desk_drafts_ready",
                        "payload": {
                            "desk_drafts": [
                                draft.model_dump(mode="json")
                                for draft in desk_drafts
                            ],
                            **desk_payload,
                        },
                    },
                },
            )
            final_policy = stage_policy(
                "daily_final",
                configured_output_tokens=self._max_output_tokens,
                length=request.length.value,
            )
            messages = build_daily_final_messages(
                request,
                desk_drafts=desk_drafts,
                outline_json=_daily_editor_outline(request, desk_drafts),
                skill_instructions=skill_instructions,
                max_input_chars=final_policy.max_input_chars,
            )
        if request.report_type == ReportType.MATCH_PREDICTION and self._council_enabled:
            opinions, council_results = await self._run_prediction_council(request)
            _emit_progress(progress_callback, "prediction_council_ready", 72)
            if opinions:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"以下是 {len(opinions)} 个成功返回的独立分析席位意见。"
                            "你是终审席：必须审阅分歧，不得简单平均；仅能引用输入 "
                            "evidence_id，并输出最终报告。如果少于三个席位，必须在 "
                            "warnings 中说明预测委员会已降级。\n"
                            + "\n".join(item.model_dump_json() for item in opinions)
                        ),
                    }
                )
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "两个独立 Flash 分析席位暂时不可用。请仅依据证据包完成"
                            "保守预测，在 warnings 明确说明预测委员会已降级，并降低"
                            " confidence；不得补造外部观点。"
                        ),
                    }
                )
        last_result: LLMResult | None = None
        last_errors: list[str] = []
        attempt_limit = max_attempts or self._max_attempts
        total_round_limit = (
            14
            if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST
            else 5
            if request.report_type == ReportType.MATCH_PREDICTION
            else 2
        )
        attempt_limit = min(
            attempt_limit,
            self._max_attempts,
            max(1, total_round_limit - len(council_results)),
        )

        recovery_rounds = 0
        used_stable_final = False
        for attempt in range(1, attempt_limit + 1):
            _emit_progress(
                progress_callback,
                "editor_synthesis",
                82,
                {"attempt": attempt, "status": "started"},
            )
            if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
                final_policy = stage_policy(
                    "daily_final",
                    configured_output_tokens=self._max_output_tokens,
                    length=request.length.value,
                )
                thinking_enabled = final_policy.thinking_enabled
                max_output_tokens = final_policy.max_output_tokens
            elif request.report_type == ReportType.MATCH_PREDICTION:
                final_policy = stage_policy(
                    "prediction_judge",
                    configured_output_tokens=self._max_output_tokens,
                    length=request.length.value,
                )
                thinking_enabled = final_policy.thinking_enabled
                max_output_tokens = final_policy.max_output_tokens
            else:
                thinking_enabled = False
                max_output_tokens = _final_output_budget(
                    request, self._max_output_tokens
                )
            llm_request = LLMRequest(
                purpose=request.report_type.value,
                model=(
                    self._model
                    if request.report_type
                    in {
                        ReportType.MATCH_PREDICTION,
                        ReportType.DAILY_FOOTBALL_DIGEST,
                    }
                    else self._flash_model
                ),
                messages=messages,
                thinking_enabled=thinking_enabled,
                max_output_tokens=max_output_tokens,
                metadata={
                    "report_type": request.report_type.value,
                    "subject": request.subject,
                    "match_stage": (
                        request.match_stage.value if request.match_stage else None
                    ),
                    "evidence_ids": [item.id for item in request.evidence],
                    "input_chars": message_chars(messages),
                },
            )
            try:
                last_result = await self._provider.generate_json(llm_request)
            except LLMProviderError as exc:
                if not _is_recoverable_final_provider_error(exc):
                    raise
                _emit_progress(
                    progress_callback,
                    "editor_synthesis",
                    82,
                    {
                        "attempt": attempt,
                        "status": "provider_failed",
                        "provider_error_kind": exc.kind,
                        "provider_status_code": exc.status_code,
                        "request_id": exc.request_id,
                    },
                )
                recovery_rounds += 1
                used_stable_final = True
                stable_request = self._stable_final_request(llm_request, request, exc)
                try:
                    last_result = await self._provider.generate_json(stable_request)
                except LLMProviderError as stable_exc:
                    _emit_progress(
                        progress_callback,
                        "editor_synthesis",
                        82,
                        {
                            "attempt": attempt,
                            "status": "stable_provider_failed",
                            "provider_error_kind": stable_exc.kind,
                            "provider_status_code": stable_exc.status_code,
                            "request_id": stable_exc.request_id,
                        },
                    )
                    if (
                        request.report_type == ReportType.DAILY_FOOTBALL_DIGEST
                        and desk_drafts
                        and _is_recoverable_final_provider_error(stable_exc)
                    ):
                        recovery_rounds += 1
                        return await self._deterministic_daily_response(
                            request,
                            desk_drafts,
                            council_results,
                            attempts=attempt + len(council_results) + recovery_rounds,
                            progress_callback=progress_callback,
                        )
                    raise
            normalized_output = normalize_generated_output(last_result.output, request)
            try:
                repair_candidate = GeneratedReport.model_validate(normalized_output)
                _repair_report_scorelines(repair_candidate, request)
                normalized_output = repair_candidate.model_dump(mode="json")
            except ValidationError:
                pass
            try:
                report = validate_generated_report(normalized_output, request)
            except ReportValidationError as exc:
                last_errors = exc.errors
                attempt_payload = {
                    "attempt": attempt,
                    "status": "validation_failed",
                    "validation_errors": exc.errors[:8],
                    "attempt_summary": _attempt_summary(normalized_output),
                    "checkpoint": {
                        "name": f"editor_synthesis_attempt_{attempt}",
                        "payload": {
                            "attempt": attempt,
                            "status": "validation_failed",
                            "validation_errors": exc.errors[:12],
                            "normalized_output": normalized_output,
                        },
                    },
                }
                _emit_progress(
                    progress_callback, "editor_synthesis", 82, attempt_payload
                )
                if attempt < attempt_limit:
                    messages = append_revision_request(
                        messages,
                        normalized_output,
                        [
                            *exc.errors,
                            *_claim_revision_hints(
                                exc.errors, request, normalized_output
                            ),
                        ],
                    )
                    continue
                break

            _assign_section_categories(report, request)
            coverage_gaps = _daily_coverage_gaps(report, request)
            if coverage_gaps and attempt < attempt_limit:
                last_errors = coverage_gaps
                _emit_progress(
                    progress_callback,
                    "editor_synthesis",
                    82,
                    {
                        "attempt": attempt,
                        "status": "coverage_failed",
                        "coverage_gaps": coverage_gaps[:8],
                        "attempt_summary": _attempt_summary(normalized_output),
                        "checkpoint": {
                            "name": f"editor_synthesis_attempt_{attempt}",
                            "payload": {
                                "attempt": attempt,
                                "status": "coverage_failed",
                                "coverage_gaps": coverage_gaps[:12],
                                "normalized_output": normalized_output,
                            },
                        },
                    },
                )
                messages = append_revision_request(
                    messages, normalized_output, coverage_gaps
                )
                continue
            if coverage_gaps:
                last_errors = coverage_gaps
                if (
                    request.report_type == ReportType.DAILY_FOOTBALL_DIGEST
                    and desk_drafts
                ):
                    return await self._deterministic_daily_response(
                        request,
                        desk_drafts,
                        council_results,
                        attempts=attempt + len(council_results) + recovery_rounds,
                        progress_callback=progress_callback,
                        recovery_reasons=coverage_gaps,
                    )
                break

            _emit_progress(
                progress_callback,
                "editor_synthesis",
                82,
                {
                    "attempt": attempt,
                    "status": "accepted",
                    "category_counts": _section_category_counts(report.sections),
                    "attempt_summary": _attempt_summary(
                        report.model_dump(mode="json")
                    ),
                },
            )

            if used_stable_final:
                _append_report_warning(
                    report,
                    "高思考总编辑请求曾遇到模型连接异常；系统已使用稳定合稿模式恢复，"
                    "请发布前重点复核取舍和措辞。",
                )

            if report.prediction is not None:
                report.prediction.statistical_baseline = statistical_baseline
                known_external = {
                    (item.source_name.casefold(), tuple(item.evidence_ids))
                    for item in report.prediction.external_predictions
                }
                additions = [
                    item
                    for item in sourced_external_predictions
                    if (item.source_name.casefold(), tuple(item.evidence_ids))
                    not in known_external
                ]
                report.prediction.external_predictions = [
                    *report.prediction.external_predictions,
                    *additions,
                ][:6]
                _ensure_prediction_analysis(report)

            _append_coverage_warnings(
                report, request, statistical_baseline=statistical_baseline
            )
            _inject_deterministic_match_timeline(report, request)
            _inject_additional_match_timeline(report, request)
            _inject_deterministic_player_spotlights(report, request)
            _repair_report_scorelines(report, request)

            await self._attach_report_media(report, request, progress_callback)
            report.warnings = _clean_report_warnings(report.warnings)

            return ReportResponse(
                id=str(uuid4()),
                provider=last_result.provider,
                model=last_result.model,
                prompt_version=PROMPT_VERSION,
                data_cutoff=request.data_cutoff,
                generated_at=datetime.now(UTC),
                attempts=attempt + len(council_results) + recovery_rounds,
                usage=TokenUsage(
                    input_tokens=last_result.input_tokens
                    + sum(item.input_tokens for item in council_results),
                    output_tokens=last_result.output_tokens
                    + sum(item.output_tokens for item in council_results),
                ),
                report=report,
            )

        critical_response = self._deterministic_critical_response(
            request,
            council_results,
            last_result,
            attempts=attempt_limit + len(council_results) + recovery_rounds,
            progress_callback=progress_callback,
        )
        if critical_response:
            return critical_response

        if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST and desk_drafts:
            _emit_progress(
                progress_callback,
                "claim_repair",
                86,
                {
                    "reason": "final_report_validation_failed",
                    "validation_errors": last_errors[:8],
                },
            )
            return await self._deterministic_daily_response(
                request,
                desk_drafts,
                council_results,
                attempts=attempt_limit + len(council_results) + recovery_rounds,
                progress_callback=progress_callback,
                recovery_reasons=last_errors,
            )

        raise ReportGenerationError(
            "report validation failed after bounded retries: " + "; ".join(last_errors)
        )

    def _deterministic_critical_response(
        self,
        request: ReportRequest,
        council_results: list[LLMResult],
        last_result: LLMResult | None,
        *,
        attempts: int,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> ReportResponse | None:
        if request.report_type == ReportType.MATCH_PREDICTION:
            return None
        entities = _critical_entities_for_request(request)
        if not entities:
            return None
        _emit_progress(progress_callback, "deterministic_finalizer", 88)

        sections = []
        summaries = []
        for entity in entities[:3]:
            evidence_id = f"critical-{entity.id}"
            if evidence_id not in {item.id for item in request.evidence}:
                continue
            name = entity.preferred_name or entity.canonical_name
            sentence = _critical_entity_sentence(entity)
            summaries.append(sentence)
            sections.append(
                {
                    "heading": "官方确认",
                    "body": (
                        f"{sentence} 这类消息必须先交代官方确认的重大事实，"
                        "再做生涯、球队和赛事背景回溯；不能写成普通缺阵、轮休"
                        "或名单变化。"
                    ),
                    "evidence_ids": [evidence_id],
                }
            )
            sections.append(
                {
                    "heading": "写作处理",
                    "body": (
                        f"后续稿件可以称其为{name}，并围绕官方确认、利物浦与"
                        "葡萄牙队相关回忆、球迷悼念和赛程影响展开。若需要补充"
                        "更多背景，应继续回到原链接和其他批准来源核对。"
                    ),
                    "evidence_ids": [evidence_id],
                }
            )

        if not sections:
            return None
        report = validate_generated_report(
            GeneratedReport(
                title=f"{request.subject}｜官方事实回溯",
                executive_summary=" ".join(summaries),
                sections=sections[:8],
                warnings=[],
                prediction=None,
            ).model_dump(mode="json"),
            request,
        )
        _assign_section_categories(report, request)
        if request.prefetched_media_assets:
            report.enrichment.media_assets = _filter_prefetched_media_assets(
                request.prefetched_media_assets, report
            )
        _repair_report_scorelines(report, request)
        report.warnings = _clean_report_warnings(report.warnings)
        return ReportResponse(
            id=str(uuid4()),
            provider=last_result.provider if last_result else "harness",
            model=(
                "deterministic-critical-finalizer"
                if last_result is None
                else last_result.model
            ),
            prompt_version=PROMPT_VERSION,
            data_cutoff=request.data_cutoff,
            generated_at=datetime.now(UTC),
            attempts=min(8, max(1, attempts)),
            usage=TokenUsage(
                input_tokens=(last_result.input_tokens if last_result else 0)
                + sum(item.input_tokens for item in council_results),
                output_tokens=(last_result.output_tokens if last_result else 0)
                + sum(item.output_tokens for item in council_results),
            ),
            report=report,
        )

    async def _attach_report_media(
        self,
        report: GeneratedReport,
        request: ReportRequest,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> None:
        prefetched_media_assets = _filter_prefetched_media_assets(
            list(request.prefetched_media_assets), report
        )
        media_assets = prefetched_media_assets
        if self._media_enabled:
            _emit_progress(progress_callback, "licensed_media", 90)
            generated_media_assets = await collect_report_media(
                report,
                youtube_api_key=self._youtube_api_key,
                youtube_channel_ids=list(self._youtube_channel_ids),
            )
            media_assets = _merge_media_assets(
                prefetched_media_assets, generated_media_assets
            )
            media_assets = _filter_prefetched_media_assets(media_assets, report)
        if media_assets:
            report.enrichment.media_assets = media_assets

    def _stable_final_request(
        self,
        llm_request: LLMRequest,
        request: ReportRequest,
        exc: LLMProviderError,
    ) -> LLMRequest:
        stable_policy = stage_policy(
            "daily_stable_final"
            if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST
            else "prediction_judge",
            configured_output_tokens=self._max_output_tokens,
            length=request.length.value,
        )
        return LLMRequest(
            purpose=f"{request.report_type.value}:stable_final",
            model=llm_request.model,
            messages=[
                *llm_request.messages,
                {
                    "role": "user",
                    "content": (
                        "上一轮高思考总编辑请求未能稳定返回。现在进入稳定合稿模式："
                        "只输出一份较短但完整的 JSON 报告；优先保留已证实事实、"
                        "传闻标签、来源引用和主要结论；可减少人物卡、时间线和修辞，"
                        "但不得新增证据外事实。warnings 中说明已启用稳定合稿。"
                        f"上游错误类型：{exc.kind}。"
                    ),
                },
            ],
            thinking_enabled=False,
            max_output_tokens=stable_policy.max_output_tokens,
            metadata={
                **llm_request.metadata,
                "recovery": "stable_final",
                "provider_error_kind": exc.kind,
            },
        )

    async def _deterministic_daily_response(
        self,
        request: ReportRequest,
        desk_drafts: list[DeskDraft],
        council_results: list[LLMResult],
        *,
        attempts: int,
        progress_callback: Callable[[str, int], None] | None = None,
        recovery_reasons: list[str] | None = None,
    ) -> ReportResponse:
        _emit_progress(
            progress_callback,
            "deterministic_finalizer",
            88,
            {
                "reason": "bounded_final_recovery",
                "recovery_reasons": (recovery_reasons or [])[:8],
            },
        )
        sections, sanitizing_warnings = _safe_daily_sections(request, desk_drafts)
        _emit_progress(
            progress_callback,
            "deterministic_finalizer",
            88,
            {
                "status": "sections_prepared",
                "section_count": len(sections),
                "category_counts": _section_category_counts(sections),
            },
        )
        warnings = [
            "总编辑合稿未能稳定返回可校验 JSON；系统已根据完成的分桌草稿"
            "生成可编辑稿，请发布前重点复核结构和措辞。"
        ]
        if recovery_reasons:
            warnings.append("最终合稿触发声明校验修复；系统已改用证据保守合稿。")
        warnings.extend(sanitizing_warnings)
        for draft in desk_drafts:
            warnings.extend(draft.warnings)
        summary = "；".join(_shorten(draft.summary, 450) for draft in desk_drafts)
        if len(re.findall(r"[\u4e00-\u9fff]", summary)) < 10:
            summary = "系统已根据完成的分栏小组草稿生成保守版今日足球消息汇总。"
        try:
            report = validate_generated_report(
                GeneratedReport(
                    title=f"{request.subject}｜保守合稿版",
                    executive_summary=summary,
                    sections=sections,
                    warnings=list(dict.fromkeys(warnings))[:12],
                    prediction=None,
                ).model_dump(mode="json"),
                request,
            )
        except ReportValidationError as exc:
            fallback_sections, fallback_warnings = _safe_daily_sections(request, [])
            warnings.extend(fallback_warnings)
            if exc.errors:
                warnings.append("分桌草稿仍有不可发布 claim；系统已退回证据保守小节。")
            _emit_progress(
                progress_callback,
                "deterministic_finalizer",
                88,
                {
                    "status": "desk_sections_validation_failed",
                    "validation_errors": exc.errors[:8],
                },
            )
            try:
                report = validate_generated_report(
                    GeneratedReport(
                        title=f"{request.subject}｜证据保守版",
                        executive_summary=summary,
                        sections=fallback_sections,
                        warnings=list(dict.fromkeys(warnings))[:12],
                        prediction=None,
                    ).model_dump(mode="json"),
                    request,
                )
                _emit_progress(
                    progress_callback,
                    "deterministic_finalizer",
                    88,
                    {
                        "status": "fallback_sections_prepared",
                        "section_count": len(fallback_sections),
                        "category_counts": _section_category_counts(
                            fallback_sections
                        ),
                    },
                )
            except ReportValidationError as fallback_exc:
                warnings.append(
                    "证据保守小节仍未通过校验；系统已降级为最小证据摘要。"
                )
                _emit_progress(
                    progress_callback,
                    "deterministic_finalizer",
                    88,
                    {
                        "status": "fallback_sections_validation_failed",
                        "validation_errors": fallback_exc.errors[:8],
                    },
                )
                report = validate_generated_report(
                    GeneratedReport(
                        title=f"{request.subject}｜最小证据摘要",
                        executive_summary=summary,
                        sections=_minimal_daily_sections(request),
                        warnings=list(dict.fromkeys(warnings))[:12],
                        prediction=None,
                    ).model_dump(mode="json"),
                    request,
                )
        _assign_section_categories(report, request)
        _emit_progress(
            progress_callback,
            "deterministic_finalizer",
            88,
            {
                "status": "accepted",
                "section_count": len(report.sections),
                "category_counts": _section_category_counts(report.sections),
            },
        )
        remaining_gaps = _daily_coverage_gaps(report, request)
        if remaining_gaps:
            for gap in remaining_gaps[:3]:
                _append_report_warning(report, gap)
        report.enrichment.media_assets = _filter_prefetched_media_assets(
            request.prefetched_media_assets, report
        )
        _repair_report_scorelines(report, request)
        _inject_deterministic_match_timeline(report, request)
        _inject_additional_match_timeline(report, request)
        _inject_deterministic_player_spotlights(report, request)
        await self._attach_report_media(report, request, progress_callback)
        report.warnings = _clean_report_warnings(report.warnings)
        return ReportResponse(
            id=str(uuid4()),
            provider="harness",
            model="deterministic-daily-finalizer",
            prompt_version=PROMPT_VERSION,
            data_cutoff=request.data_cutoff,
            generated_at=datetime.now(UTC),
            attempts=attempts,
            usage=TokenUsage(
                input_tokens=sum(item.input_tokens for item in council_results),
                output_tokens=sum(item.output_tokens for item in council_results),
            ),
            report=report,
        )

    async def _run_daily_desks(
        self, request: ReportRequest
    ) -> tuple[list[DeskDraft], list[LLMResult]]:
        columns = request.editorial_plan or _fallback_daily_columns(request)
        brief_schema = json.dumps(DeskBrief.model_json_schema(), ensure_ascii=False)
        draft_schema = json.dumps(DeskDraft.model_json_schema(), ensure_ascii=False)

        async def research(
            column: EditorialColumnPlan,
        ) -> tuple[DeskBrief, LLMResult]:
            evidence, evidence_ids = _column_evidence_text(request, column)
            column_evidence = [
                item for item in request.evidence if item.id in set(evidence_ids)
            ]
            numeric_claim_ledger = build_numeric_claim_ledger(column_evidence)
            group_instruction = _specialist_instruction(column.specialist_group)
            instruction = " ".join(
                item
                for item in [group_instruction, column.instructions]
                if item.strip()
            )
            result = await self._provider.generate_json(
                LLMRequest(
                    purpose=f"daily_research:{column.specialist_group}",
                    model=self._flash_model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是今日球脉的专栏研究小组。只处理 Leader 分给"
                                "你的栏目，不要扩写其他栏目。只输出符合 JSON Schema "
                                f"的对象：{brief_schema}。只能引用输入 evidence_id。"
                                "unverified_lead 只能作为 rumor_items。每个小组最多"
                                "4 轮内部反思；本次只输出最终结构化简报。"
                                "必须先判断证据状态：已完赛、赛前、场外或转会。"
                                "赛前、抵达、开球安排、酒店接待和观赛安排不能写成战报。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Column: {column.title}\n"
                                f"Group: {column.specialist_group}\n"
                                f"Category: {column.category}\n"
                                f"Media targets: {column.media_targets}\n"
                                f"Enrichment targets: {column.enrichment_targets}\n"
                                "Coverage requirements: "
                                f"{column.coverage_requirements}\n"
                                f"Instruction: {instruction}\n证据：\n{evidence}"
                                "\n可用数字 claim ledger："
                                + json.dumps(numeric_claim_ledger, ensure_ascii=False)
                            ),
                        },
                    ],
                    thinking_enabled=True,
                    max_output_tokens=min(self._max_output_tokens, 4200),
                    metadata={
                        "report_type": request.report_type.value,
                        "subject": request.subject,
                        "evidence_ids": evidence_ids,
                        "desk": column.column_id,
                        "specialist_group": column.specialist_group,
                    },
                )
            )
            brief = DeskBrief.model_validate(
                {**result.output, "desk": column.column_id}
            )
            return brief, result

        researched = await asyncio.gather(
            *(research(column) for column in columns[:6]),
            return_exceptions=True,
        )
        briefs = [item for item in researched if not isinstance(item, BaseException)]
        results = [item[1] for item in briefs]
        if not briefs:
            return [], results

        async def write_desk(brief: DeskBrief) -> tuple[DeskDraft, LLMResult]:
            column = next(
                (item for item in columns if item.column_id == brief.desk),
                None,
            )
            column_instruction = (
                f"栏目标题：{column.title}；负责小组：{column.specialist_group}；"
                f"栏目类别：{column.category}；媒体目标：{column.media_targets}。"
                f"交付合同：{column.coverage_requirements}。"
                "数字只能来自研究简报和该栏目 claim ledger；没有 ledger 支持的"
                "比分、分钟、年份、金额、出场、进球数必须删除数字或放入 warnings。"
                "赛前/开球/抵达/酒店/敌意接待证据只能写成赛前或场外，不得写成已完赛战报。"
                "正文不得堆叠“关键事件待确认、暂未明朗、未知、待补充”等占位句；"
                "缺口放入 warnings 或研究简报 unknowns。"
                if column
                else ""
            )
            column_evidence_ids = (
                column.evidence_ids if column else brief.key_items[0].evidence_ids
            )
            column_evidence = [
                item for item in request.evidence if item.id in set(column_evidence_ids)
            ]
            numeric_claim_ledger = build_numeric_claim_ledger(column_evidence)
            result = await self._provider.generate_json(
                LLMRequest(
                    purpose=f"daily_desk_write:{brief.desk}",
                    model=self._model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是资深中文足球编辑。根据研究简报写一个栏目草稿，"
                                f"只输出符合 JSON Schema 的对象：{draft_schema}。"
                                "必须遵守 Leader 栏目合同，不要写其他栏目。"
                                "保留逐节 evidence_ids；传闻必须用‘传闻/未核实’措辞。"
                                "写得像给懂球但没时间刷消息的读者看的栏目：先说今天"
                                "的变化，再补人物背景、球队关联或比赛转折。事实、来源"
                                "观点和编辑判断要分开，但不要把段落写成可信度标签堆叠。"
                                "sections 是读者看到的二级标题。战报小组必须每场比赛"
                                "单独一个 section，转会小组必须每个重点转会单独一个"
                                " section。比赛段落按模板写：何时何地，谁和谁比赛，"
                                "谁在第几分钟用什么方式进球，比分如何变化，比赛过程"
                                "和晋级/淘汰影响；例如“第 72 分钟，XXX 接队友传中"
                                "头球破门，将比分改写为 2-1”。"
                                "证据没有写出的发布会、社交媒体、悼念方式、首发安排、"
                                "赛程日期、主帅/队长表态和直接引语，不要补写；"
                                "没有原文引语时只能转述，不得加引号。"
                                "如果引用的是赛前、抵达、开球时间、酒店接待或赛程安排，"
                                "只能写成赛前/场外栏目；不要写“今日赛场、展开较量、最终、击败”。"
                                "正文不要用“尚待确认/未知/待补充”扩写缺失信息。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": column_instruction
                            + "\n可用数字 claim ledger："
                            + json.dumps(numeric_claim_ledger, ensure_ascii=False)
                            + "\n研究简报："
                            + brief.model_dump_json(),
                        },
                    ],
                    thinking_enabled=True,
                    max_output_tokens=min(self._max_output_tokens, 5600),
                    metadata={
                        "report_type": request.report_type.value,
                        "subject": request.subject,
                        "evidence_ids": brief.key_items[0].evidence_ids
                        if brief.key_items
                        else [item.id for item in request.evidence[:1]],
                        "desk": brief.desk,
                    },
                )
            )
            draft = DeskDraft.model_validate({**result.output, "desk": brief.desk})
            return draft, result

        written = await asyncio.gather(
            *(write_desk(item[0]) for item in briefs), return_exceptions=True
        )
        successful = [item for item in written if not isinstance(item, BaseException)]
        results.extend(item[1] for item in successful)
        return [item[0] for item in successful], results

    async def _run_prediction_council(
        self, request: ReportRequest
    ) -> tuple[list[PredictionOpinion], list[LLMResult]]:
        evidence = "\n".join(
            f"[{item.id}] {item.title}: {item.summary}" for item in request.evidence
        )
        roles = {
            "form_analyst": "从实力、状态、阵容、休息和战术匹配分析支持性证据",
            "tactical_analyst": "从阵型、压迫方式、边路/中路对位和比赛节奏形成独立预测",
            "skeptic": "主动寻找样本偏差、伤停不确定性、对位风险和反方证据",
        }

        opinion_schema = json.dumps(
            PredictionOpinion.model_json_schema(), ensure_ascii=False
        )

        async def run_role(
            role: str, instruction: str
        ) -> tuple[PredictionOpinion, list[LLMResult]]:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are an independent pre-match football analyst. Output one "
                        "JSON object only. It must match this JSON Schema exactly: "
                        f"{opinion_schema}. key_claims must contain at least one "
                        "claim, and every evidence_ids value must come from the "
                        "supplied evidence. The three probabilities must sum to 1. "
                        "Do not add keys or prose."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Role: {role}. Task: {instruction}\n"
                        f"Match: {request.subject}\nEvidence:\n{evidence}"
                    ),
                },
            ]

            async def generate(current_messages: list[dict[str, str]]) -> LLMResult:
                return await self._provider.generate_json(
                    LLMRequest(
                        purpose=f"prediction_opinion:{role}",
                        model=self._flash_model,
                        messages=current_messages,
                        thinking_enabled=True,
                        max_output_tokens=min(self._max_output_tokens, 1800),
                        metadata={
                            "report_type": request.report_type.value,
                            "subject": request.subject,
                            "match_stage": request.match_stage.value
                            if request.match_stage
                            else None,
                            "evidence_ids": [item.id for item in request.evidence],
                            "opinion_role": role,
                        },
                    )
                )

            def validate(output: dict[str, object]) -> PredictionOpinion:
                opinion = PredictionOpinion.model_validate({**output, "role": role})
                total = opinion.home_win + opinion.draw + opinion.away_win
                if abs(total - 1.0) > 0.001:
                    raise ValueError("probabilities are not normalized")
                allowed = {item.id for item in request.evidence}
                referenced = {
                    evidence_id
                    for claim in opinion.key_claims
                    for evidence_id in claim.evidence_ids
                }
                if referenced - allowed:
                    raise ValueError("opinion cites unknown evidence")
                return opinion

            results = [await generate(messages)]
            try:
                opinion = validate(results[0].output)
            except (ValidationError, ValueError) as exc:
                repair_messages = [
                    *messages,
                    {
                        "role": "assistant",
                        "content": json.dumps(results[0].output, ensure_ascii=False),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Repair the JSON to match the schema exactly. "
                            "Do not add prose. "
                            f"Validation error: {exc}"
                        ),
                    },
                ]
                results.append(await generate(repair_messages))
                try:
                    opinion = validate(results[-1].output)
                except (ValidationError, ValueError) as retry_exc:
                    raise ReportGenerationError(
                        f"prediction opinion {role} failed its bounded repair"
                    ) from retry_exc
            return opinion, results

        completed = await asyncio.gather(
            *(run_role(role, instruction) for role, instruction in roles.items()),
            return_exceptions=True,
        )
        successful = [item for item in completed if not isinstance(item, BaseException)]
        if not successful:
            # The Pro judge can still produce a bounded report from the evidence
            # packet. This avoids one transient Flash seat taking down the user
            # request while keeping the missing review visible in the prompt.
            return [], []
        opinions = [item[0] for item in successful]
        results = [result for item in successful for result in item[1]]
        return opinions, results
