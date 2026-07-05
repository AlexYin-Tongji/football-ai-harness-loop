from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from services.report_api.domain import Evidence, ReportSection

MatchEvidenceState = Literal["completed_match", "upcoming_match", "mixed", "unknown"]


SCORELINE_RE = re.compile(r"(?<!\d)\d{1,2}\s*[-\u2013\u2014]\s*\d{1,2}(?!\d)")
COMPLETED_RE = re.compile(
    r"\b(?:beat|beats|beaten|defeat|defeats|defeated|won|win|victory|loss|lost|"
    r"scored|scores|goal|goals|penalty|red card|full[- ]time|match report|"
    r"stroll(?:s|ed)? into|sent .* into|advanced?|qualified?|knocked out|"
    r"cruise(?:s|d)? past|edge(?:s|d)? past|survive(?:s|d)?|overcame|"
    r"overcome)\b|"
    r"\u51fb\u8d25|\u6218\u80dc|\u53d6\u80dc|\u83b7\u80dc|\u8fdb\u7403|"
    r"\u6bd4\u5206|\u7ec8\u573a|\u664b\u7ea7|\u6dd8\u6c70|\u51fa\u5c40",
    re.I,
)
UPCOMING_TITLE_RE = re.compile(
    r"\b(?:preview|live stream|team news|tickets?|fixture|scheduled|schedule|"
    r"kick[- ]?off|all[- ]?nighter|arrival|arrive|welcome|hotel|ahead of|"
    r"before|will face|will meet|set to|showdown|survival is key)\b|"
    r"\u524d\u77bb|\u5c06\u4e8e|\u5373\u5c06|\u5f00\u7403|\u8d5b\u524d|"
    r"\u5907\u6218|\u62b5\u8fbe|\u9152\u5e97|\u63a5\u5f85|\u8d5b\u7a0b",
    re.I,
)
UPCOMING_BODY_RE = re.compile(
    r"\b(?:preview|fixture|scheduled|schedule|kick[- ]?off|ahead of|before|"
    r"will face|will meet|set to|prepares?|arrival|arrive|welcome|hotel|"
    r"team news|line[- ]?ups?|tickets?|hostile reception|all[- ]?nighter)\b|"
    r"\u524d\u77bb|\u5c06\u4e8e|\u5373\u5c06|\u5f00\u7403|\u8d5b\u524d|"
    r"\u5907\u6218|\u62b5\u8fbe|\u9152\u5e97|\u63a5\u5f85|\u8d5b\u7a0b",
    re.I,
)
COMPLETED_COPY_RE = re.compile(
    r"\b(?:today's? (?:match|fixture)|today .* (?:face|play)|final(?:ly)?|"
    r"finished|ended|beat|defeated|won|victory)\b|"
    r"\u4eca\u65e5(?:\u4e16\u754c\u676f)?\u8d5b\u573a|"
    r"\u4eca\u65e5.*(?:\u6311\u6218|\u5bf9\u9635)|"
    r"\u5c55\u5f00\u8f83\u91cf|\u6700\u7ec8|"
    r"\u4ee5\s*\d{1,2}\s*[-:\u2013\u2014]\s*\d{1,2}\s*(?:\u51fb\u8d25|\u6218\u80dc)",
    re.I,
)
FUTURE_COPY_RE = re.compile(
    r"\b(?:will|is set to|scheduled|upcoming|ahead of|before|kick[- ]?off)\b|"
    r"\u5c06\u4e8e|\u5373\u5c06|\u8d5b\u524d|\u5907\u6218|\u5f00\u7403\u65f6\u95f4",
    re.I,
)
PLACEHOLDER_BAD_PHRASES_RE = re.compile(
    r"\u5173\u952e\u4e8b\u4ef6\u5c1a\u5f85\u786e\u8ba4|"
    r"\u6bd4\u8d5b\u7ec6\u8282\u6682\u672a\u660e\u6717|"
    r"\u6bd4\u8d5b\u8fdb\u7a0b\u4f9d\u7136\u5b58\u7591|"
    r"\u6682\u65e0\u5173\u4e8e.*\u8be6\u7ec6\u62a5\u9053|"
    r"\u5c06\u5728\u83b7\u53d6\u66f4\u591a\u4fe1\u606f\u540e|"
    r"\u8ba1\u5212\u914d\u53d1|"
    r"\u9700\u540e\u7eed\u8865\u91c7|"
    r"key events? (?:remain|are) (?:unclear|unknown)|"
    r"details? (?:remain|are) (?:unclear|unknown)",
    re.I,
)
PLACEHOLDER_TERM_RE = re.compile(
    r"\u672a\u77e5|\u5c1a\u5f85\u786e\u8ba4|\u6682\u672a\u660e\u6717|"
    r"\u672a\u6709\u8be6\u7ec6|\u672a\u89c1\u53ef\u9760|\u5b58\u7591|"
    r"\u5f85\u8865\u5145|\u8865\u91c7|unknown|unclear|unconfirmed",
    re.I,
)
TRANSFER_EVIDENCE_RE = re.compile(
    r"\b(?:transfer|signing|signs|signed|bid|fee|medical|joins?|loan)\b|"
    r"\bagree(?:d|s)?\s+(?:a\s+)?(?:£?\d+(?:\.\d+)?m?\s+)?deal\s+for\b|"
    r"£\d+|\b\d+(?:\.\d+)?m\b|转会|签下|报价|英镑|体检|租借",
    re.I,
)


def evidence_text(item: Evidence) -> str:
    return f"{item.title} {item.summary}"


def is_upcoming_match_evidence(item: Evidence) -> bool:
    title = item.title
    text = evidence_text(item)
    if UPCOMING_TITLE_RE.search(title):
        return True
    return bool(UPCOMING_BODY_RE.search(text) and not SCORELINE_RE.search(title))


def is_completed_match_evidence(item: Evidence) -> bool:
    text = evidence_text(item)
    if SCORELINE_RE.search(item.title) or COMPLETED_RE.search(item.title):
        return True
    if is_upcoming_match_evidence(item) and not SCORELINE_RE.search(item.title):
        return False
    return bool(SCORELINE_RE.search(text) or COMPLETED_RE.search(text))


def match_evidence_state(items: Iterable[Evidence]) -> MatchEvidenceState:
    has_completed = False
    has_upcoming = False
    for item in items:
        has_completed = has_completed or is_completed_match_evidence(item)
        has_upcoming = has_upcoming or is_upcoming_match_evidence(item)
    if has_completed and has_upcoming:
        return "mixed"
    if has_completed:
        return "completed_match"
    if has_upcoming:
        return "upcoming_match"
    return "unknown"


def completed_match_items(items: Iterable[Evidence]) -> list[Evidence]:
    return [item for item in items if is_completed_match_evidence(item)]


def evidence_is_match_like(item: Evidence) -> bool:
    text = evidence_text(item)
    return bool(
        is_completed_match_evidence(item)
        or is_upcoming_match_evidence(item)
        or re.search(r"\b(?:world cup|last 16|knockout|match|fixture)\b", text, re.I)
    )


def is_transfer_evidence(item: Evidence) -> bool:
    return bool(TRANSFER_EVIDENCE_RE.search(evidence_text(item)))


def has_completed_match_claim(text: str) -> bool:
    return bool(COMPLETED_COPY_RE.search(text) or SCORELINE_RE.search(text))


def has_future_match_framing(text: str) -> bool:
    return bool(FUTURE_COPY_RE.search(text))


def placeholder_copy_errors(section: ReportSection) -> list[str]:
    text = f"{section.heading} {section.body}"
    errors: list[str] = []
    if PLACEHOLDER_BAD_PHRASES_RE.search(text):
        errors.append(
            f"section '{section.heading}' uses placeholder match-report copy "
            "instead of a substantive evidence-backed delivery"
        )
    placeholder_terms = PLACEHOLDER_TERM_RE.findall(text)
    if section.category != "transfer" and len(placeholder_terms) >= 3:
        errors.append(
            f"section '{section.heading}' is placeholder-heavy; move unknowns "
            "to warnings and keep the body evidence-backed"
        )
    return errors[:2]


def match_state_errors(section: ReportSection, cited: list[Evidence]) -> list[str]:
    if not cited:
        return []
    section_text = f"{section.heading} {section.body}"
    state = match_evidence_state(cited)
    if state == "upcoming_match" and has_completed_match_claim(section_text):
        return [
            f"section '{section.heading}' writes upcoming fixture evidence "
            "as a completed/today match"
        ]
    if section.category == "match" and state == "upcoming_match":
        return [
            f"section '{section.heading}' is categorized as match report "
            "but only cites upcoming fixture evidence"
        ]
    return []
