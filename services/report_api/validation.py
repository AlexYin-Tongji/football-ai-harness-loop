from __future__ import annotations

import copy
import re

from pydantic import ValidationError

from services.report_api.claim_ledger import supported_numbers_for_evidence
from services.report_api.critical_entities import load_critical_entities
from services.report_api.domain import (
    GeneratedReport,
    MatchStage,
    ReportRequest,
    ReportType,
)
from services.report_api.evidence_state import (
    is_transfer_evidence,
    match_state_errors,
    placeholder_copy_errors,
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
    evidence_by_id = {item.id: item for item in request.evidence}
    _normalize_section_categories(normalized)
    _repair_critical_entity_status(normalized, request)
    _prune_unsupported_enrichment(normalized, evidence_by_id)
    if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
        _neutralize_unsupported_direct_quotes(normalized, request)
    prediction = normalized.get("prediction")
    if not isinstance(prediction, dict):
        return normalized

    externals = prediction.get("external_predictions")
    if isinstance(externals, list):
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


def _source_text(evidence_ids: object, evidence_by_id: dict[str, object]) -> str:
    if not isinstance(evidence_ids, list):
        return ""
    return " ".join(
        f"{item.title} {item.summary}"
        for item_id in evidence_ids
        if isinstance(item_id, str)
        for item in [evidence_by_id.get(item_id)]
        if item is not None
    )


def _add_warning(normalized: dict[str, object], warning: str) -> None:
    warnings = normalized.setdefault("warnings", [])
    if not isinstance(warnings, list):
        normalized["warnings"] = [warning]
        return
    if warning in warnings:
        return
    if len(warnings) < 12:
        warnings.append(warning)
    elif warnings:
        warnings[-1] = warning


DEATH_TERMS_RE = re.compile(
    r"去世|逝世|离世|身亡|遇难|丧生|死亡|tragic passing|passed away|died|killed",
    re.I,
)
INJURY_ABSENCE_RE = re.compile(
    r"因伤|伤病|受伤|伤缺|缺席(?:本届|剩余|余下|接下来)?|injur(?:y|ed)|ruled out",
    re.I,
)
MISLEADING_DEATH_TIMING_RE = re.compile(
    r"赛前|近日|日前|刚刚|今日|今天|本周|recent(?:ly)?|before the match",
    re.I,
)
MINUTE_RE = re.compile(r"^(?:\d{1,3}(?:\+\d{1,2})?|赛前|半场|终场)$")
SCORE_RE = re.compile(r"^\d{1,2}-\d{1,2}$")
MATCH_EVENT_TYPES = {
    "goal",
    "own_goal",
    "penalty",
    "card",
    "substitution",
    "key_moment",
}
DIRECT_QUOTE_RE = re.compile(r"[“\"]([^”\"]{4,160})[”\"]")
SECTION_CATEGORY_MAP = {
    "match": "match",
    "matches": "match",
    "match_news": "match",
    "赛场": "match",
    "赛事": "match",
    "比赛": "match",
    "战报": "match",
    "transfer": "transfer",
    "transfers": "transfer",
    "transfer_market": "transfer",
    "转会": "transfer",
    "引援": "transfer",
    "off_field": "off_field",
    "off-field": "off_field",
    "schedule": "off_field",
    "场外": "off_field",
    "赛程": "off_field",
    "转播": "off_field",
    "context": "context",
    "background": "context",
    "背景": "context",
    "脉络": "context",
}


def _normalize_section_categories(normalized: dict[str, object]) -> None:
    sections = normalized.get("sections")
    if not isinstance(sections, list):
        return
    for section in sections:
        if not isinstance(section, dict):
            continue
        category = section.get("category")
        if category is None:
            continue
        key = str(category).strip().casefold()
        section["category"] = SECTION_CATEGORY_MAP.get(key)


def _contains_alias(text: str, aliases: list[str]) -> bool:
    folded = text.casefold()
    return any(
        alias in text
        if re.search(r"[\u4e00-\u9fff]", alias)
        else alias.casefold() in folded
        for alias in aliases
    )


def _sentences_with_alias(text: str, aliases: list[str]) -> list[str]:
    pieces = re.split(r"(?<=[。！？.!?])\s*", text)
    return [piece for piece in pieces if _contains_alias(piece, aliases)]


def _critical_entity_sentence(entity: object) -> str:
    report_summary = getattr(entity, "report_summary", None)
    if isinstance(report_summary, str) and report_summary.strip():
        return report_summary.strip()
    preferred_name = getattr(entity, "preferred_name", None)
    canonical_name = getattr(entity, "canonical_name", "")
    name = preferred_name or canonical_name
    if name != canonical_name:
        name = f"{canonical_name}（{name}）"
    source_name = getattr(entity, "source_name", "官方来源")
    event_date = getattr(entity, "event_date", None)
    date_text = event_date.date().isoformat() if event_date else "已"
    return f"{source_name}确认，{name}已于{date_text}去世。"


def _remove_bad_critical_sentences(text: str, aliases: list[str]) -> str:
    pieces = re.split(r"(?<=[。！？.!?])\s*", text)
    kept = [
        piece
        for piece in pieces
        if piece.strip()
        and not (
            _contains_alias(piece, aliases)
            and (
                INJURY_ABSENCE_RE.search(piece)
                or (
                    DEATH_TERMS_RE.search(piece)
                    and MISLEADING_DEATH_TIMING_RE.search(piece)
                )
            )
        )
    ]
    return " ".join(kept).strip()


def _repair_critical_text(text: str, aliases: list[str], sentence: str) -> str:
    repaired = _remove_bad_critical_sentences(text, aliases)
    if not repaired:
        return sentence
    alias_context = " ".join(_sentences_with_alias(repaired, aliases))
    if not DEATH_TERMS_RE.search(alias_context):
        return f"{repaired.rstrip()} {sentence}"
    return repaired


def _repair_critical_entity_status(
    normalized: dict[str, object], request: ReportRequest
) -> None:
    sections = normalized.get("sections")
    if not isinstance(sections, list):
        return
    registry = load_critical_entities()
    evidence_text = " ".join(
        f"{item.id} {item.title} {item.summary} {item.source_name}"
        for item in request.evidence
    )
    allowed_ids = {item.id for item in request.evidence}

    for entity in registry.entities:
        if entity.critical_status != "deceased":
            continue
        aliases = [entity.canonical_name, *entity.aliases]
        critical_id = f"critical-{entity.id}"
        has_source_context = critical_id in allowed_ids or _contains_alias(
            evidence_text, aliases
        )
        if not has_source_context:
            continue
        sentence = _critical_entity_sentence(entity)
        summary = normalized.get("executive_summary")
        if isinstance(summary, str) and _contains_alias(summary, aliases):
            normalized["executive_summary"] = _repair_critical_text(
                summary, aliases, sentence
            )

        mentioned = False
        for section in sections:
            if not isinstance(section, dict):
                continue
            evidence_ids = section.get("evidence_ids")
            section_text = " ".join(
                str(section.get(key) or "") for key in ("heading", "body")
            )
            section_mentions = _contains_alias(section_text, aliases)
            section_cites_critical = (
                isinstance(evidence_ids, list) and critical_id in evidence_ids
            )
            if not section_mentions and not section_cites_critical:
                continue
            mentioned = True
            body = str(section.get("body") or "")
            section["body"] = _repair_critical_text(body, aliases, sentence)
            if critical_id in allowed_ids:
                if not isinstance(evidence_ids, list):
                    evidence_ids = []
                if critical_id not in evidence_ids:
                    evidence_ids.append(critical_id)
                section["evidence_ids"] = evidence_ids

        if mentioned:
            continue
        if critical_id not in allowed_ids:
            continue
        if isinstance(summary, str):
            normalized["executive_summary"] = f"{summary.rstrip()} {sentence}".strip()
        sections.append(
            {
                "heading": f"{entity.preferred_name or entity.canonical_name}官方事实",
                "body": (
                    f"{sentence} 相关内容只能按官方确认的重大人物状态处理，"
                    "不得写成普通伤缺、轮休或名单变化。"
                ),
                "evidence_ids": [critical_id],
                "category": "context",
            }
        )


def _critical_status_errors(visible_text: str, request: ReportRequest) -> list[str]:
    registry = load_critical_entities()
    evidence_text = " ".join(
        f"{item.id} {item.title} {item.summary}" for item in request.evidence
    )
    errors: list[str] = []
    for entity in registry.entities:
        if entity.critical_status != "deceased":
            continue
        aliases = [entity.canonical_name, *entity.aliases]
        if not any(alias.casefold() in evidence_text.casefold() for alias in aliases):
            continue
        mentioned_aliases = [
            alias
            for alias in aliases
            if (
                alias in visible_text
                if re.search(r"[\u4e00-\u9fff]", alias)
                else alias.casefold() in visible_text.casefold()
            )
        ]
        if not mentioned_aliases and any(
            item.id == f"critical-{entity.id}" for item in request.evidence
        ):
            errors.append(
                f"{entity.canonical_name} critical status evidence is not reflected"
            )
            continue
        alias_context = " ".join(_sentences_with_alias(visible_text, aliases))
        if mentioned_aliases and not DEATH_TERMS_RE.search(alias_context):
            errors.append(
                f"{entity.canonical_name} must be described as deceased when cited"
            )
        if mentioned_aliases and INJURY_ABSENCE_RE.search(alias_context):
            errors.append(
                f"{entity.canonical_name} cannot be described as injury absence; "
                "official evidence says deceased"
            )
    return errors


def _prune_unsupported_enrichment(
    normalized: dict[str, object], evidence_by_id: dict[str, object]
) -> None:
    enrichment = normalized.get("enrichment")
    if not isinstance(enrichment, dict):
        if enrichment is not None:
            normalized["enrichment"] = {}
        return

    enrichment["media_assets"] = []
    allowed_ids = set(evidence_by_id)

    pruned_metrics = 0
    spotlights = enrichment.get("player_spotlights")
    if isinstance(spotlights, list):
        kept_spotlights = []
        for spotlight in spotlights:
            if not isinstance(spotlight, dict):
                continue
            evidence_ids = spotlight.get("evidence_ids")
            supported_ids = (
                [
                    item
                    for item in evidence_ids
                    if isinstance(item, str) and item in allowed_ids
                ]
                if isinstance(evidence_ids, list)
                else []
            )
            if not (
                isinstance(spotlight.get("name"), str)
                and spotlight["name"].strip()
                and isinstance(spotlight.get("narrative"), str)
                and spotlight["narrative"].strip()
                and supported_ids
            ):
                continue
            spotlight["evidence_ids"] = supported_ids
            related_clubs = spotlight.get("related_clubs")
            spotlight["related_clubs"] = (
                [item for item in related_clubs if isinstance(item, str)][:4]
                if isinstance(related_clubs, list)
                else []
            )
            if not isinstance(spotlight.get("media_search_name"), str):
                spotlight["media_search_name"] = None
            if not isinstance(spotlight.get("position"), str):
                spotlight["position"] = None
            metrics = spotlight.get("metrics")
            if not isinstance(metrics, list):
                spotlight["metrics"] = []
                kept_spotlights.append(spotlight)
                continue
            source_text = _source_text(spotlight.get("evidence_ids"), evidence_by_id)
            kept_metrics = []
            for metric in metrics:
                if not isinstance(metric, dict):
                    continue
                if not isinstance(metric.get("label"), str) or not isinstance(
                    metric.get("value"), str
                ):
                    continue
                value = str(metric.get("value") or "")
                if value and value.casefold() in source_text.casefold():
                    kept_metrics.append(metric)
                else:
                    pruned_metrics += 1
            spotlight["metrics"] = kept_metrics
            kept_spotlights.append(spotlight)
        enrichment["player_spotlights"] = kept_spotlights
    elif spotlights is not None:
        enrichment["player_spotlights"] = []
    if pruned_metrics:
        _add_warning(
            normalized,
            "部分人物卡数据未能在引用资料中定位，系统已移除这些可选指标。",
        )

    pruned_events = 0
    events = enrichment.get("match_timeline")
    if isinstance(events, list):
        kept_events = []
        for event in events:
            if not isinstance(event, dict):
                continue
            minute = event.get("minute")
            event_type = event.get("event_type")
            description = event.get("description")
            evidence_ids = event.get("evidence_ids")
            supported_ids = (
                [
                    item
                    for item in evidence_ids
                    if isinstance(item, str) and item in allowed_ids
                ]
                if isinstance(evidence_ids, list)
                else []
            )
            if not (
                isinstance(minute, str)
                and MINUTE_RE.match(minute)
                and event_type in MATCH_EVENT_TYPES
                and isinstance(description, str)
                and description.strip()
                and supported_ids
            ):
                pruned_events += 1
                continue
            event["evidence_ids"] = supported_ids
            if not isinstance(event.get("player"), str):
                event["player"] = None
            if not isinstance(event.get("team"), str):
                event["team"] = None
            score_after = event.get("score_after")
            if not (isinstance(score_after, str) and SCORE_RE.match(score_after)):
                event["score_after"] = None
            minute_number = minute.split("+", 1)[0]
            source_text = _source_text(event.get("evidence_ids"), evidence_by_id)
            if not minute_number.isdigit() or re.search(
                rf"(?<!\d){re.escape(minute_number)}(?:st|nd|rd|th|\s*分钟|'|’)?",
                source_text,
                re.I,
            ):
                kept_events.append(event)
            else:
                pruned_events += 1
        enrichment["match_timeline"] = kept_events
    elif events is not None:
        enrichment["match_timeline"] = []
    if pruned_events:
        _add_warning(
            normalized,
            "部分比赛时间线分钟未能在引用资料中定位，系统已移除这些可选事件。",
        )


def _unsupported_direct_quotes(section_body: str, source_text: str) -> list[str]:
    errors = []
    folded_source = source_text.casefold()
    for match in DIRECT_QUOTE_RE.finditer(section_body):
        quote = match.group(1).strip()
        if quote and quote.casefold() not in folded_source:
            errors.append(
                f"direct quote is not present in cited evidence: {quote[:60]}"
            )
    return errors


def _neutralize_unsupported_direct_quotes(
    normalized: dict[str, object], request: ReportRequest
) -> None:
    sections = normalized.get("sections")
    if not isinstance(sections, list):
        return
    by_id = {item.id: item for item in request.evidence}
    changed = False
    for section in sections:
        if not isinstance(section, dict):
            continue
        body = section.get("body")
        evidence_ids = section.get("evidence_ids")
        if not isinstance(body, str) or not isinstance(evidence_ids, list):
            continue
        source_text = " ".join(
            f"{item.title} {item.summary}"
            for evidence_id in evidence_ids
            if isinstance(evidence_id, str)
            if (item := by_id.get(evidence_id)) is not None
        )
        folded_source = source_text.casefold()

        def replace(match: re.Match[str], folded_source: str = folded_source) -> str:
            nonlocal changed
            quote = match.group(1).strip()
            if quote and quote.casefold() not in folded_source:
                changed = True
                return quote
            return match.group(0)

        section["body"] = DIRECT_QUOTE_RE.sub(replace, body)
    if changed:
        _add_warning(
            normalized,
            "部分未能在来源中精确定位的引号内容已改为转述。",
        )


FACTUAL_TRIGGER_RE = re.compile(
    r"确认|报道|称|宣布|击败|战胜|淘汰|晋级|进球|破门|扳平|反超|点球|VAR|"
    r"转会|签下|签约|报价|协议|体检|合同|伤停|禁赛|出场|首发|助攻|"
    r"beat|defeat|score|scored|goal|transfer|sign|signed|bid|agreement|medical",
    re.I,
)
RUMOR_LABEL_RE = re.compile(r"传闻|据报道|未核实|线索|尚未确认|据.*称|reported", re.I)
ASSERTIVE_RUMOR_RE = re.compile(
    r"已经|已|确认|完成|官宣|签下|签约|加盟|达成|敲定|will join|has signed",
    re.I,
)
NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{1,4}(?:[.,]\d{1,3})?)(?:-\d{1,2})?"
    r"(?:\s*(?:分钟|分|%|英镑|欧元|美元|万|亿|million|bn|m|st|nd|rd|th))?"
)
TIME_CLAIM_RE = re.compile(
    r"第\s*\d{1,3}\s*分钟|\d{1,3}(?:st|nd|rd|th)[-\s]?minute",
    re.I,
)
SCORE_CLAIM_RE = re.compile(r"(?<![\d:-])\d{1,2}\s*[-–]\s*\d{1,2}(?![\d:-])")


def _split_claim_sentences(text: str) -> list[str]:
    pieces = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
    return [piece.strip() for piece in pieces if len(piece.strip()) >= 8]


def _number_tokens(text: str) -> list[str]:
    tokens = []
    for match in NUMBER_RE.finditer(text):
        token = match.group(0).strip()
        if token:
            tokens.append(token)
    return tokens


def _normalized_number(value: str) -> str:
    return re.sub(r"[^0-9]", "", value)


def _claim_integrity_errors(section: object, request: ReportRequest) -> list[str]:
    errors: list[str] = []
    evidence_by_id = {item.id: item for item in request.evidence}
    section_ids = [str(item) for item in getattr(section, "evidence_ids", [])]
    if section_ids and all(item.startswith("critical-") for item in section_ids):
        return []
    cited = [
        item
        for evidence_id in section_ids
        if (item := evidence_by_id.get(evidence_id)) is not None
    ]
    source_text = " ".join(f"{item.title} {item.summary}" for item in cited)
    source_numbers = supported_numbers_for_evidence(request.evidence, section_ids)
    discovery_cited = any(
        item.verification_status == "unverified_lead" for item in cited
    )

    for sentence in _split_claim_sentences(getattr(section, "body", "")):
        factual = bool(
            FACTUAL_TRIGGER_RE.search(sentence)
            or TIME_CLAIM_RE.search(sentence)
            or SCORE_CLAIM_RE.search(sentence)
            or _number_tokens(sentence)
        )
        if not factual:
            continue
        if (
            discovery_cited
            and ASSERTIVE_RUMOR_RE.search(sentence)
            and not RUMOR_LABEL_RE.search(sentence)
        ):
            errors.append(
                f"claim in section '{section.heading}' upgrades discovery lead "
                "without a per-claim rumor label"
            )
        for token in _number_tokens(sentence):
            normalized = _normalized_number(token)
            if not normalized or len(normalized) <= 1:
                continue
            if normalized not in source_numbers:
                errors.append(
                    f"numeric claim is not present in cited evidence "
                    f"for section '{section.heading}': {token}"
                )
        if TIME_CLAIM_RE.search(sentence) and not TIME_CLAIM_RE.search(source_text):
            errors.append(
                "minute-level claim lacks minute evidence in section "
                f"'{section.heading}'"
            )
        score = SCORE_CLAIM_RE.search(sentence)
        if score and _normalized_number(score.group(0)) not in source_numbers:
            errors.append(
                f"scoreline claim is not present in cited evidence "
                f"for section '{section.heading}': {score.group(0)}"
            )
    return errors[:8]


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
    errors.extend(_critical_status_errors(visible_text, request))
    if any(evidence_id in visible_text for evidence_id in allowed_ids):
        errors.append("visible report text must not expose internal evidence IDs")

    for section in report.sections:
        referenced_ids.update(section.evidence_ids)
        cited = [item for item in request.evidence if item.id in section.evidence_ids]
        section_source_text = " ".join(f"{item.title} {item.summary}" for item in cited)
        errors.extend(_unsupported_direct_quotes(section.body, section_source_text))
        errors.extend(_claim_integrity_errors(section, request))
        if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
            errors.extend(placeholder_copy_errors(section))
            errors.extend(match_state_errors(section, cited))
            if section.category == "transfer" and not any(
                is_transfer_evidence(item) for item in cited
            ):
                errors.append(
                    f"section '{section.heading}' is categorized as transfer "
                    "but cites no transfer evidence"
                )
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
                *prediction.analysis_process,
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
