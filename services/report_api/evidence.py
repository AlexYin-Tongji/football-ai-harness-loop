from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Final
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from services.mcp_servers.common import load_publisher_registry
from services.report_api.domain import ConsumerReportRequest, Evidence, ReportType

GUARDIAN_FOOTBALL_RSS: Final = "https://www.theguardian.com/football/rss"
BBC_FOOTBALL_RSS: Final = "https://feeds.bbci.co.uk/sport/football/rss.xml"
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
_feed_cache: tuple[datetime, bytes] | None = None
_bbc_feed_cache: tuple[datetime, bytes] | None = None
_gdelt_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
CLUSTER_STOP_WORDS = {
    "a",
    "an",
    "and",
    "at",
    "for",
    "football",
    "from",
    "in",
    "of",
    "on",
    "the",
    "to",
    "transfer",
    "world",
    "cup",
}


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
        ReportType.DAILY_FOOTBALL_DIGEST: [
            "world cup",
            "football",
            "transfer",
            "signing",
        ],
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
    if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
        return any(term in all_text for term in terms)
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
                source_id="guardian-football-rss",
                trust_tier="S1",
                evidence_kind="verified",
                verification_status="publisher_report",
                source_independence_key="guardian-football",
            )
        )
        if len(evidence) >= max_items:
            break

    if len(evidence) < 2:
        raise EvidenceCollectionError(
            "没有找到足够的近期相关资料，请写明球队英文名或更具体的主题"
        )
    return evidence


async def collect_bbc_evidence(
    request: ConsumerReportRequest, *, max_items: int = 12
) -> list[Evidence]:
    global _bbc_feed_cache
    now = datetime.now(UTC)
    if _bbc_feed_cache and now - _bbc_feed_cache[0] < timedelta(minutes=15):
        content = _bbc_feed_cache[1]
    else:
        content = b""
        timeout = httpx.Timeout(25.0, connect=10.0)
        for _attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                    headers={"User-Agent": "FootPulse/0.3 evidence-reader"},
                ) as client:
                    response = await client.get(BBC_FOOTBALL_RSS)
                    response.raise_for_status()
                    content = response.content
                    _bbc_feed_cache = (now, content)
                    break
            except (httpx.HTTPError, httpx.TimeoutException):
                continue
        if (
            not content
            and _bbc_feed_cache
            and now - _bbc_feed_cache[0] < timedelta(hours=6)
        ):
            content = _bbc_feed_cache[1]
        if not content:
            raise EvidenceCollectionError("BBC Football RSS 暂时无法连接")
    if len(content) > 1_000_000:
        raise EvidenceCollectionError("BBC Football RSS 返回内容超出安全上限")
    return parse_bbc_feed(content, request, max_items=max_items, cutoff=now)


def parse_bbc_feed(
    content: bytes,
    request: ConsumerReportRequest,
    *,
    max_items: int = 12,
    cutoff: datetime | None = None,
) -> list[Evidence]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise EvidenceCollectionError("BBC Football RSS 数据无法识别") from exc
    cutoff = cutoff or datetime.now(UTC)
    earliest = cutoff - timedelta(days=7)
    terms = _terms(request)
    evidence: list[Evidence] = []
    seen_urls: set[str] = set()
    for item in root.findall(".//item"):
        title = _clean_text(item.findtext("title"), 500)
        url = _clean_text(item.findtext("link"), 1000)
        summary = _clean_text(item.findtext("description")) or title
        raw_date = item.findtext("pubDate")
        hostname = (urlparse(url).hostname or "").lower()
        if not title or not url or not raw_date:
            continue
        if not (
            hostname == "bbc.com"
            or hostname.endswith(".bbc.com")
            or hostname == "bbc.co.uk"
            or hostname.endswith(".bbc.co.uk")
        ):
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
                id="bbc-" + hashlib.sha256(url.encode()).hexdigest()[:12],
                title=title,
                url=url,
                published_at=published_at,
                source_name="BBC Sport Football",
                summary=summary,
                source_id="bbc-football-rss",
                trust_tier="S1",
                evidence_kind="verified",
                verification_status="publisher_report",
                source_independence_key="bbc-sport",
            )
        )
        if len(evidence) >= max_items:
            break
    if not evidence:
        raise EvidenceCollectionError("BBC Football RSS 没有近期相关资料")
    return evidence


def _gdelt_query(request: ConsumerReportRequest) -> str:
    subject = re.sub(r"[^A-Za-z0-9 '\-]", " ", request.subject)
    subject = SPACE_RE.sub(" ", subject).strip()[:180]
    suffix = {
        ReportType.DAILY_FOOTBALL_DIGEST: (
            '(football OR soccer) ("World Cup" OR transfer OR signing OR injury)'
        ),
        ReportType.TRANSFER_DAILY: "(transfer OR signing OR bid OR deal OR target)",
        ReportType.WORLD_CUP_DAILY: '"World Cup" football',
        ReportType.MATCH_PREDICTION: "(preview OR prediction OR injury OR lineup)",
    }[request.report_type]
    return f"{subject} {suffix}".strip()


def _publisher_map() -> dict[str, dict[str, Any]]:
    return {
        item["domain"].lower(): item
        for item in load_publisher_registry()["publishers"]
        if item.get("access") != "metadata_only_subscription_required"
    }


def _publisher_for_domain(
    domain: str, publishers: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    normalized = domain.lower().removeprefix("www.")
    for approved_domain, publisher in publishers.items():
        approved = approved_domain.removeprefix("www.")
        if normalized == approved or normalized.endswith(f".{approved}"):
            return publisher
    return None


def parse_gdelt_articles(
    payload: dict[str, Any],
    request: ConsumerReportRequest,
    *,
    max_items: int = 20,
    cutoff: datetime | None = None,
) -> list[Evidence]:
    cutoff = cutoff or datetime.now(UTC)
    lookback_days = 7 if request.report_type == ReportType.TRANSFER_DAILY else 4
    earliest = cutoff - timedelta(days=lookback_days)
    publishers = _publisher_map()
    evidence: list[Evidence] = []
    seen_urls: set[str] = set()
    per_publisher: dict[str, int] = {}

    for item in payload.get("articles", []):
        title = _clean_text(item.get("title"), 500)
        url = _clean_text(item.get("url"), 1000)
        domain = _clean_text(item.get("domain"), 200).lower()
        publisher = _publisher_for_domain(domain, publishers)
        if not title or not url or not publisher:
            continue
        hostname = (urlparse(url).hostname or "").lower()
        if _publisher_for_domain(hostname, publishers) is None or url in seen_urls:
            continue
        raw_date = str(item.get("seendate") or "")
        try:
            published_at = datetime.strptime(raw_date, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=UTC
            )
        except ValueError:
            continue
        if not earliest <= published_at <= cutoff:
            continue
        publisher_id = str(publisher["id"])
        if per_publisher.get(publisher_id, 0) >= 3:
            continue
        seen_urls.add(url)
        per_publisher[publisher_id] = per_publisher.get(publisher_id, 0) + 1
        evidence.append(
            Evidence(
                id="gdelt-" + hashlib.sha256(url.encode()).hexdigest()[:12],
                title=title,
                url=url,
                published_at=published_at,
                source_name=publisher_id.replace("-", " ").title(),
                summary=(
                    f"发现线索：{title}。当前只取得发布元数据，尚未完成正文级核验；"
                    "可作为传闻或进一步调查入口，不得单独写成已确认事实。"
                ),
                source_id=publisher_id,
                trust_tier=str(publisher["tier"]),
                evidence_kind="discovery",
                verification_status="unverified_lead",
                source_independence_key=publisher_id,
            )
        )
        if len(evidence) >= max_items:
            break
    return evidence


async def collect_gdelt_evidence(
    request: ConsumerReportRequest, *, max_items: int = 20
) -> list[Evidence]:
    query = _gdelt_query(request)
    now = datetime.now(UTC)
    cached = _gdelt_cache.get(query)
    if cached and now - cached[0] < timedelta(minutes=15):
        payload = cached[1]
    else:
        timeout = httpx.Timeout(20.0, connect=7.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "FootPulse/0.3 metadata-discovery"},
        ) as client:
            response = await client.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params={
                    "query": query,
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": min(max_items * 3, 50),
                    "timespan": "7d",
                    "sort": "datedesc",
                },
            )
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise EvidenceCollectionError("新闻发现结果超过安全上限")
            payload = json.loads(response.content)
            _gdelt_cache[query] = (now, payload)
    return parse_gdelt_articles(payload, request, max_items=max_items, cutoff=now)


async def collect_research_evidence(
    request: ConsumerReportRequest, *, max_items: int = 24
) -> list[Evidence]:
    """Collect verified publisher items plus clearly labelled discovery leads."""
    import asyncio

    rss_limit = max(8, min(15, max_items))
    guardian_task = collect_guardian_evidence(request, max_items=rss_limit)
    bbc_task = collect_bbc_evidence(request, max_items=rss_limit)
    gdelt_task = collect_gdelt_evidence(
        request, max_items=max(4, max_items - min(12, max_items // 2))
    )
    results = await asyncio.gather(
        guardian_task, bbc_task, gdelt_task, return_exceptions=True
    )
    combined: list[Evidence] = []
    errors: list[Exception] = []
    for result in results:
        if isinstance(result, BaseException):
            if isinstance(result, Exception):
                errors.append(result)
            continue
        combined.extend(result)

    unique: list[Evidence] = []
    cluster_tokens: list[tuple[str, set[str]]] = []
    seen: set[str] = set()
    subject_terms = [
        term
        for term in _terms(request)
        if term
        not in {
            "world cup",
            "fifa",
            "football",
            "team news",
            "injury",
            "transfer",
            "signing",
            "signs",
            "target",
            "deal",
        }
    ]

    def relevance(item: Evidence) -> tuple[int, datetime]:
        text = f"{item.title} {item.summary}".casefold()
        score = sum(4 for term in subject_terms if term in text)
        if request.report_type == ReportType.MATCH_PREDICTION and re.search(
            r"opta|probability|prediction|preview|tactics|lineup|injury", text
        ):
            score += 3
        if request.report_type in {
            ReportType.TRANSFER_DAILY,
            ReportType.DAILY_FOOTBALL_DIGEST,
        } and re.search(
            r"transfer|signing|signs|bid|deal|target|medical|agreement", text
        ):
            score += 2
        return score, item.published_at

    for item in sorted(combined, key=relevance, reverse=True):
        canonical = str(item.url).split("#", 1)[0]
        if canonical in seen:
            continue
        seen.add(canonical)
        tokens = {
            token
            for token in re.findall(r"[a-z0-9]{2,}", item.title.casefold())
            if token not in CLUSTER_STOP_WORDS
        }
        cluster_id = None
        for known_id, known_tokens in cluster_tokens:
            union = tokens | known_tokens
            similarity = len(tokens & known_tokens) / len(union) if union else 0
            if similarity >= 0.55:
                cluster_id = known_id
                break
        if cluster_id is None:
            signature = " ".join(sorted(tokens)) or canonical
            cluster_id = "story-" + hashlib.sha256(signature.encode()).hexdigest()[:12]
            cluster_tokens.append((cluster_id, tokens))
        item.story_cluster_id = cluster_id
        unique.append(item)
        if len(unique) >= max_items:
            break
    if len(unique) < 2:
        raise EvidenceCollectionError(
            "没有找到足够的近期资料，请写明球队英文名或更具体的主题"
        ) from (errors[0] if errors else None)
    return unique
