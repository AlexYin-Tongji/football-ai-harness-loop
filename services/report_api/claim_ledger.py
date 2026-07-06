from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from services.report_api.domain import Evidence

NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{1,4}(?:[.,]\d{1,3})?)(?:-\d{1,2})?"
    r"(?:\s*(?:分钟|分|%|英镑|欧元|美元|万|亿|million|bn|m|st|nd|rd|th))?"
)
DATE_RE = re.compile(
    r"\b(?P<year>20\d{2})[-/年](?P<month>\d{1,2})[-/月](?P<day>\d{1,2})"
)
SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
LAST_16_RE = re.compile(r"last[-\s]?16|16\s*强|十六强", re.I)
STORY_CLUSTER_RE = re.compile(r"事件簇\s+story-[^：:]+[:：][^。]*。?")
CLUSTER_TITLE_RE = re.compile(r"同簇标题[:：][^。]*。?")
SOURCE_EXCERPT_RE = re.compile(r"来源原摘[:：]\s*")


def shorten(value: str, limit: int) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def number_tokens(text: str) -> list[str]:
    tokens = []
    for match in NUMBER_RE.finditer(text):
        token = match.group(0).strip()
        if token:
            tokens.append(token)
    return tokens


def normalized_number(value: str) -> str:
    return re.sub(r"[^0-9]", "", value)


def normalized_number_variants(token: str) -> set[str]:
    variants = {normalized_number(token)}
    raw = token.strip()
    lowered = raw.casefold()
    score = re.fullmatch(r"(\d{1,2})\s*[-–]\s*(\d{1,2})", raw)
    if score:
        variants.add(f"{score.group(2)}{score.group(1)}")
    amount = re.search(r"\d{1,4}(?:[.,]\d{1,3})?", raw)
    if amount:
        try:
            value = Decimal(amount.group(0).replace(",", ""))
        except InvalidOperation:
            value = None
        if value is not None:
            if re.search(r"(?:million|m\b)", lowered):
                variants.add(str(int(value * 100)))
            if "万" in raw and value % 100 == 0:
                variants.add(str(int(value / 100)))
    variants.discard("")
    return variants


def meaningful_normalized_numbers(text: str) -> set[str]:
    numbers: set[str] = set()
    for token in number_tokens(text):
        numbers.update(
            variant for variant in normalized_number_variants(token) if len(variant) > 1
        )
    for match in DATE_RE.finditer(text):
        numbers.add(match.group("year"))
        numbers.add(f"{int(match.group('month')):02d}")
        numbers.add(f"{int(match.group('day')):02d}")
    numbers.discard("")
    return numbers


def evidence_source_text(evidence: list[Evidence], evidence_ids: list[str]) -> str:
    by_id = {item.id: item for item in evidence}
    return " ".join(
        f"{item.title} {item.summary}"
        for evidence_id in evidence_ids
        if (item := by_id.get(evidence_id)) is not None
    )


def supported_numbers_for_evidence(
    evidence: list[Evidence], evidence_ids: list[str]
) -> set[str]:
    return meaningful_normalized_numbers(evidence_source_text(evidence, evidence_ids))


def unsupported_numeric_tokens(
    text: str, evidence: list[Evidence], evidence_ids: list[str]
) -> list[str]:
    source_numbers = supported_numbers_for_evidence(evidence, evidence_ids)
    unsupported: list[str] = []
    for token in number_tokens(text):
        normalized = normalized_number(token)
        if normalized and len(normalized) > 1 and normalized not in source_numbers:
            unsupported.append(token)
    return list(dict.fromkeys(unsupported))


def _split_sentences(text: str) -> list[str]:
    return [piece.strip() for piece in SENTENCE_RE.split(text) if piece.strip()]


def _neutralize_token(sentence: str, token: str) -> str:
    normalized = normalized_number(token)
    if normalized == "16":
        sentence = LAST_16_RE.sub("淘汰赛阶段", sentence)
    if "-" in token or "–" in token:
        sentence = re.sub(
            re.escape(token).replace("\\–", "[–-]"),
            "具体比分待复核",
            sentence,
        )
    escaped = re.escape(token.strip())
    if escaped:
        sentence = re.sub(rf"(?<!\d){escaped}\s*年", "", sentence)
        sentence = re.sub(rf"(?<!\d){escaped}(?!\d)", "具体数字待复核", sentence)
    return sentence


def sanitize_text_against_evidence(
    text: str, evidence: list[Evidence], evidence_ids: list[str]
) -> tuple[str, list[str]]:
    """Remove or neutralize numeric claims not supported by the cited evidence."""

    source_numbers = supported_numbers_for_evidence(evidence, evidence_ids)
    changed_tokens: list[str] = []
    kept: list[str] = []
    pieces = _split_sentences(text)
    for sentence in pieces:
        repaired = sentence
        unsupported = []
        for token in number_tokens(sentence):
            normalized = normalized_number(token)
            if normalized and len(normalized) > 1 and normalized not in source_numbers:
                unsupported.append(token)
        if unsupported:
            for token in unsupported:
                repaired = _neutralize_token(repaired, token)
            remaining = [
                token
                for token in number_tokens(repaired)
                if (
                    normalized_number(token)
                    and len(normalized_number(token)) > 1
                    and normalized_number(token) not in source_numbers
                )
            ]
            changed_tokens.extend(unsupported)
            if remaining:
                continue
        if repaired.strip():
            kept.append(repaired.strip())
    if kept:
        return " ".join(kept), list(dict.fromkeys(changed_tokens))
    return (
        "该段只保留引用来源能够核实的事实；涉及具体数字的细节已转为发布前复核项。",
        list(dict.fromkeys(changed_tokens)),
    )


def clean_evidence_text(value: str) -> str:
    value = STORY_CLUSTER_RE.sub("", value)
    value = CLUSTER_TITLE_RE.sub("", value)
    value = SOURCE_EXCERPT_RE.sub("", value)
    value = value.replace("精简提炼：", "")
    value = value.replace("Continue reading...", "")
    value = value.replace("“", "").replace("”", "")
    value = value.replace('"', "")
    return shorten(value.strip(" 。"), 360)


def build_numeric_claim_ledger(
    evidence: list[Evidence], *, max_entries: int = 80
) -> list[dict[str, object]]:
    ledger: list[dict[str, object]] = []
    for item in evidence:
        source_text = f"{item.title}。{item.summary}"
        for sentence in _split_sentences(source_text):
            tokens = list(dict.fromkeys(number_tokens(sentence)))
            numbers = [token for token in tokens if len(normalized_number(token)) > 1]
            if not numbers:
                continue
            ledger.append(
                {
                    "claim": shorten(clean_evidence_text(sentence), 220),
                    "numbers": numbers[:8],
                    "evidence_ids": [item.id],
                    "source": item.source_name,
                    "status": item.verification_status,
                    "cluster": item.story_cluster_id,
                }
            )
            if len(ledger) >= max_entries:
                return ledger
    return ledger
