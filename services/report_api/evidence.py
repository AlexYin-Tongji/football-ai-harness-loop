from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Final
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from services.report_api.domain import ConsumerReportRequest, Evidence, ReportType

GUARDIAN_FOOTBALL_RSS: Final = "https://www.theguardian.com/football/rss"
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
_feed_cache: tuple[datetime, bytes] | None = None


class EvidenceCollectionError(RuntimeError):
    """Raised when no safe, current evidence can be collected."""


def _clean_text(value: str | None, limit: int = 1800) -> str:
    cleaned = html.unescape(TAG_RE.sub(" ", value or ""))
    return SPACE_RE.sub(" ", cleaned).strip()[:limit]


def _terms(request: ConsumerReportRequest) -> list[str]:
    subject_terms = [
        item.lower()
        for item in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", request.subject)
        if item.lower()
        not in {
            "report",
            "daily",
            "match",
            "football",
            "summer",
            "window",
            "today",
            "world",
            "cup",
            "fifa",
            "prediction",
        }
    ]
    defaults = {
        ReportType.WORLD_CUP_DAILY: ["world cup", "fifa"],
        ReportType.TRANSFER_DAILY: [
            "transfer",
            "signing",
            "signs",
            "target",
            "deal",
        ],
        ReportType.MATCH_PREDICTION: ["world cup", "team news", "injury"],
    }
    return list(dict.fromkeys([*subject_terms, *defaults[request.report_type]]))


def _is_relevant(
    request: ConsumerReportRequest, title: str, summary: str, terms: list[str]
) -> bool:
    title_text = title.lower()
    all_text = f"{title} {summary}".lower()
    if request.report_type == ReportType.WORLD_CUP_DAILY:
        return "world cup" in title_text or "fifa" in title_text
    if request.report_type == ReportType.TRANSFER_DAILY:
        return any(term in title_text for term in terms)
    team_terms = [
        term
        for term in terms
        if term
        not in {
            "world",
            "cup",
            "prediction",
            "team",
            "news",
            "injury",
        }
    ]
    return any(term in all_text for term in team_terms) if team_terms else False


async def collect_guardian_evidence(
    request: ConsumerReportRequest, *, max_items: int = 12
) -> list[Evidence]:
    global _feed_cache
    now = datetime.now(UTC)
    if _feed_cache and now - _feed_cache[0] < timedelta(minutes=15):
        content = _feed_cache[1]
    else:
        content = b""
        last_error: Exception | None = None
        timeout = httpx.Timeout(25.0, connect=10.0)
        for _attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                    headers={"User-Agent": "FootPulse/0.2 evidence-reader"},
                ) as client:
                    response = await client.get(GUARDIAN_FOOTBALL_RSS)
                    response.raise_for_status()
                    content = response.content
                    _feed_cache = (now, content)
                    break
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_error = exc
        if not content and _feed_cache and now - _feed_cache[0] < timedelta(hours=6):
            content = _feed_cache[1]
        if not content:
            raise EvidenceCollectionError(
                "新闻来源暂时无法连接，请稍后重试"
            ) from last_error

    if len(content) > 1_000_000:
        raise EvidenceCollectionError("新闻来源返回内容超出安全上限")
    return parse_guardian_feed(content, request, max_items=max_items)


def parse_guardian_feed(
    content: bytes,
    request: ConsumerReportRequest,
    *,
    max_items: int = 12,
    cutoff: datetime | None = None,
) -> list[Evidence]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise EvidenceCollectionError("新闻来源返回了无法识别的数据") from exc
    terms = _terms(request)
    cutoff = cutoff or datetime.now(UTC)
    earliest = cutoff - timedelta(days=4)
    evidence: list[Evidence] = []
    seen_urls: set[str] = set()

    for item in root.findall(".//item"):
        title = _clean_text(item.findtext("title"), 500)
        url = _clean_text(item.findtext("link"), 1000)
        summary = _clean_text(item.findtext("description"))
        raw_date = item.findtext("pubDate")
        if not title or not url or not summary or not raw_date:
            continue
        if (urlparse(url).hostname or "").lower() not in {
            "www.theguardian.com",
            "theguardian.com",
        }:
            continue
        try:
            published_at = parsedate_to_datetime(raw_date).astimezone(UTC)
        except (TypeError, ValueError):
            continue
        if not _is_relevant(request, title, summary, terms):
            continue
        if not earliest <= published_at <= cutoff or url in seen_urls:
            continue
        seen_urls.add(url)
        evidence.append(
            Evidence(
                id="guardian-" + hashlib.sha256(url.encode()).hexdigest()[:12],
                title=title,
                url=url,
                published_at=published_at,
                source_name="The Guardian Football",
                summary=summary,
            )
        )
        if len(evidence) >= max_items:
            break

    if len(evidence) < 2:
        raise EvidenceCollectionError(
            "没有找到足够的近期相关资料，请写明球队英文名或更具体的主题"
        )
    return evidence
