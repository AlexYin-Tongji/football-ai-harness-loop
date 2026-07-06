from __future__ import annotations

import hashlib
import html
import json
import os
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Final
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from services.mcp_servers.common import load_publisher_registry
from services.report_api.critical_entities import matching_critical_entities
from services.report_api.domain import ConsumerReportRequest, Evidence, ReportType
from services.report_api.time_scope import scope_for_request, window_query_dates

GUARDIAN_FOOTBALL_RSS: Final = "https://www.theguardian.com/football/rss"
GUARDIAN_CONTENT_API: Final = "https://content.guardianapis.com/search"
BBC_FOOTBALL_RSS: Final = "https://feeds.bbci.co.uk/sport/football/rss.xml"
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
_feed_cache: tuple[datetime, bytes] | None = None
_guardian_api_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
_bbc_feed_cache: tuple[datetime, bytes] | None = None
_gdelt_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
_newsapi_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
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
    "news",
    "latest",
    "linked",
    "club",
    "man",
}
TRANSFER_STAGE_RE = re.compile(
    r"\b(bid|offer|talks?|agreement|agreed|medical|sign(?:s|ing|ed)?|"
    r"target|interest|loan|clause|release|reject(?:s|ed)?|rumou?rs?)\b",
    re.I,
)
TRANSFER_STAGE_WORDS = {
    "bid",
    "offer",
    "talk",
    "talks",
    "agreement",
    "agreed",
    "medical",
    "sign",
    "signs",
    "signing",
    "signed",
    "target",
    "interest",
    "loan",
    "clause",
    "release",
    "reject",
    "rejects",
    "rejected",
    "rumour",
    "rumours",
    "rumor",
    "rumors",
}


class EvidenceCollectionError(RuntimeError):
    """Raised when no safe, current evidence can be collected."""


def _collection_window(
    request: ConsumerReportRequest,
    *,
    cutoff: datetime | None,
    start_at: datetime | None = None,
    lookback_days: int,
) -> tuple[datetime, datetime]:
    if cutoff is None:
        scope = scope_for_request(request)
        return scope.window_start_utc, scope.data_cutoff_utc
    end = cutoff.astimezone(UTC)
    start = (
        start_at.astimezone(UTC)
        if start_at is not None
        else end - timedelta(days=lookback_days)
    )
    return start, end


FOCUS_TERM_ALIASES: dict[str, tuple[str, ...]] = {
    "热刺": ("spurs", "tottenham", "tottenham hotspur"),
    "托特纳姆": ("spurs", "tottenham", "tottenham hotspur"),
    "曼联": ("man utd", "manchester united"),
    "阿森纳": ("arsenal",),
    "纽卡": ("newcastle", "newcastle united"),
    "西汉姆": ("west ham", "west ham united"),
    "巴萨": ("barcelona", "barca"),
    "皇马": ("real madrid",),
    "世界杯": ("world cup", "fifa"),
    "转会": ("transfer", "sign", "signed", "signing", "deal"),
    "战报": ("match report", "highlights", "official highlights"),
    "战报图": ("match report", "highlights", "official highlights"),
    "进球": ("goal", "scored", "scores"),
}
CLUB_SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "Tottenham": ("spurs", "tottenham", "tottenham hotspur", "热刺", "托特纳姆"),
    "Manchester United": ("man utd", "manchester united", "曼联"),
    "Arsenal": ("arsenal", "阿森纳"),
    "Newcastle": ("newcastle", "newcastle united", "纽卡"),
    "West Ham": ("west ham", "west ham united", "西汉姆"),
    "Barcelona": ("barcelona", "barca", "巴萨"),
    "Real Madrid": ("real madrid", "皇马"),
}


def _clean_text(value: str | None, limit: int = 1800) -> str:
    cleaned = html.unescape(TAG_RE.sub(" ", value or ""))
    return SPACE_RE.sub(" ", cleaned).strip()[:limit]


def _alias_terms(searchable_text: str) -> list[str]:
    aliases: list[str] = []
    folded = searchable_text.casefold()
    for marker, terms in FOCUS_TERM_ALIASES.items():
        if marker.casefold() in folded:
            aliases.extend(terms)
    return aliases


def _terms(request: ConsumerReportRequest) -> list[str]:
    searchable_text = " ".join([request.subject, *request.focus])
    subject_terms = [
        item.lower()
        for item in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", searchable_text)
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
    alias_terms = _alias_terms(searchable_text)
    defaults = {
        ReportType.DAILY_FOOTBALL_DIGEST: [
            "world cup",
            "football",
            "transfer",
            "sign",
            "signed",
            "signing",
            "club record",
        ],
        ReportType.WORLD_CUP_DAILY: ["world cup", "fifa"],
        ReportType.TRANSFER_DAILY: [
            "transfer",
            "sign",
            "signed",
            "signing",
            "signs",
            "target",
            "deal",
        ],
        ReportType.MATCH_PREDICTION: ["world cup", "team news", "injury"],
    }
    return list(
        dict.fromkeys([*subject_terms, *alias_terms, *defaults[request.report_type]])
    )


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
    scope = scope_for_request(request)
    return parse_guardian_feed(
        content,
        request,
        max_items=max_items,
        cutoff=scope.data_cutoff_utc,
        start_at=scope.window_start_utc,
    )


def parse_guardian_feed(
    content: bytes,
    request: ConsumerReportRequest,
    *,
    max_items: int = 12,
    cutoff: datetime | None = None,
    start_at: datetime | None = None,
) -> list[Evidence]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise EvidenceCollectionError("新闻来源返回了无法识别的数据") from exc
    terms = _terms(request)
    earliest, cutoff = _collection_window(
        request, cutoff=cutoff, start_at=start_at, lookback_days=4
    )
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
        if not earliest <= published_at < cutoff or url in seen_urls:
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

    if not evidence:
        raise EvidenceCollectionError(
            "没有找到足够的近期相关资料，请写明球队英文名或更具体的主题"
        )
    return evidence


def _guardian_api_key() -> str:
    return (
        os.getenv("GUARDIAN_OPEN_PLATFORM_API_KEY")
        or os.getenv("GUARDIAN_API_KEY")
        or "test"
    )


def _guardian_search_query(request: ConsumerReportRequest) -> tuple[str, str]:
    searchable = " ".join([request.subject, *request.focus])
    folded = searchable.casefold()
    has_transfer_intent = bool(
        re.search(r"transfer|sign(?:s|ing|ed)?|deal|bid|offer|agreement|转会", folded)
    )
    for canonical, aliases in CLUB_SEARCH_ALIASES.items():
        if any(alias.casefold() in folded for alias in aliases):
            if has_transfer_intent:
                return f'"{canonical}" "transfer"', "relevance"
            return canonical, "relevance"
    query = SPACE_RE.sub(" ", re.sub(r"[^A-Za-z0-9 '\-]", " ", request.subject))
    return query.strip()[:180], "newest"


def parse_guardian_search_payload(
    payload: dict[str, Any],
    request: ConsumerReportRequest,
    *,
    max_items: int = 12,
    cutoff: datetime | None = None,
    start_at: datetime | None = None,
) -> list[Evidence]:
    earliest, cutoff = _collection_window(
        request, cutoff=cutoff, start_at=start_at, lookback_days=7
    )
    evidence: list[Evidence] = []
    seen_urls: set[str] = set()
    results = payload.get("response", {}).get("results", [])
    for item in results:
        title = _clean_text(item.get("webTitle"), 500)
        url = _clean_text(item.get("webUrl"), 1000)
        if not title or not url:
            continue
        hostname = (urlparse(url).hostname or "").lower()
        if hostname not in {"www.theguardian.com", "theguardian.com"}:
            continue
        fields = item.get("fields") or {}
        summary = (
            _clean_text(fields.get("trailText"), 900)
            or _clean_text(fields.get("standfirst"), 900)
            or title
        )
        raw_date = str(item.get("webPublicationDate") or "")
        try:
            published_at = datetime.fromisoformat(
                raw_date.replace("Z", "+00:00")
            ).astimezone(UTC)
        except ValueError:
            continue
        if not earliest <= published_at < cutoff or url in seen_urls:
            continue
        seen_urls.add(url)
        evidence.append(
            Evidence(
                id="guardian-api-" + hashlib.sha256(url.encode()).hexdigest()[:12],
                title=title,
                url=url,
                published_at=published_at,
                source_name="The Guardian Football",
                summary=summary,
                source_id="guardian-open-platform",
                trust_tier="S1",
                evidence_kind="verified",
                verification_status="publisher_report",
                source_independence_key="guardian-football",
            )
        )
        if len(evidence) >= max_items:
            break
    return evidence


async def collect_guardian_search_evidence(
    request: ConsumerReportRequest, *, max_items: int = 12
) -> list[Evidence]:
    now = datetime.now(UTC)
    scope = scope_for_request(request)
    query, order_by = _guardian_search_query(request)
    if not query:
        return []
    cache_key = json.dumps(
        {
            "query": query,
            "date": request.report_date.isoformat(),
            "window_start": scope.window_start_utc.isoformat(),
            "cutoff": scope.data_cutoff_utc.isoformat(),
            "max_items": max_items,
        },
        sort_keys=True,
    )
    cached = _guardian_api_cache.get(cache_key)
    if cached and now - cached[0] < timedelta(minutes=15):
        payload = cached[1]
    else:
        from_date, to_date = window_query_dates(scope)
        params = {
            "api-key": _guardian_api_key(),
            "section": "football",
            "q": query,
            "order-by": order_by,
            "page-size": str(max(1, min(max_items, 20))),
            "show-fields": "trailText,standfirst",
            "from-date": from_date,
            "to-date": to_date,
        }
        timeout = httpx.Timeout(20.0, connect=8.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "FootPulse/0.3 guardian-open-platform"},
        ) as client:
            response = await client.get(GUARDIAN_CONTENT_API, params=params)
            response.raise_for_status()
            payload = response.json()
        _guardian_api_cache[cache_key] = (now, payload)
    return parse_guardian_search_payload(
        payload,
        request,
        max_items=max_items,
        cutoff=scope.data_cutoff_utc,
        start_at=scope.window_start_utc,
    )


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
    scope = scope_for_request(request)
    return parse_bbc_feed(
        content,
        request,
        max_items=max_items,
        cutoff=scope.data_cutoff_utc,
        start_at=scope.window_start_utc,
    )


def parse_bbc_feed(
    content: bytes,
    request: ConsumerReportRequest,
    *,
    max_items: int = 12,
    cutoff: datetime | None = None,
    start_at: datetime | None = None,
) -> list[Evidence]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise EvidenceCollectionError("BBC Football RSS 数据无法识别") from exc
    earliest, cutoff = _collection_window(
        request, cutoff=cutoff, start_at=start_at, lookback_days=7
    )
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
        if not earliest <= published_at < cutoff or url in seen_urls:
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


def _approved_newsapi_domains() -> str:
    registry = load_publisher_registry()
    domains: list[str] = []
    for publisher in registry["publishers"]:
        access = str(publisher.get("access") or "")
        if access in {"official_verification", "metadata_only_subscription_required"}:
            continue
        domain = str(publisher.get("domain") or "").lower()
        if domain:
            domains.append(domain)
        if len(domains) >= 20:
            break
    return ",".join(dict.fromkeys(domains))


def parse_gdelt_articles(
    payload: dict[str, Any],
    request: ConsumerReportRequest,
    *,
    max_items: int = 20,
    cutoff: datetime | None = None,
    start_at: datetime | None = None,
) -> list[Evidence]:
    lookback_days = 7 if request.report_type == ReportType.TRANSFER_DAILY else 4
    earliest, cutoff = _collection_window(
        request, cutoff=cutoff, start_at=start_at, lookback_days=lookback_days
    )
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
        if not earliest <= published_at < cutoff:
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
    scope = scope_for_request(request)
    cache_key = json.dumps(
        {
            "query": query,
            "window_start": scope.window_start_utc.isoformat(),
            "cutoff": scope.data_cutoff_utc.isoformat(),
        },
        sort_keys=True,
    )
    cached = _gdelt_cache.get(cache_key)
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
                    "startdatetime": scope.window_start_utc.strftime("%Y%m%d%H%M%S"),
                    "enddatetime": scope.data_cutoff_utc.strftime("%Y%m%d%H%M%S"),
                    "sort": "datedesc",
                },
            )
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise EvidenceCollectionError("新闻发现结果超过安全上限")
            payload = json.loads(response.content)
            _gdelt_cache[cache_key] = (now, payload)
    return parse_gdelt_articles(
        payload,
        request,
        max_items=max_items,
        cutoff=scope.data_cutoff_utc,
        start_at=scope.window_start_utc,
    )


def _newsapi_query(request: ConsumerReportRequest) -> str:
    subject = re.sub(r"[^A-Za-z0-9 '\-]", " ", request.subject)
    subject = SPACE_RE.sub(" ", subject).strip()[:140]
    suffix = {
        ReportType.DAILY_FOOTBALL_DIGEST: (
            '(football OR soccer) AND ("World Cup" OR transfer OR signing)'
        ),
        ReportType.TRANSFER_DAILY: "(transfer OR signing OR bid OR talks OR deal)",
        ReportType.WORLD_CUP_DAILY: '"World Cup" AND football',
        ReportType.MATCH_PREDICTION: "(preview OR prediction OR lineup OR injury)",
    }[request.report_type]
    return f"{subject} {suffix}".strip()


def parse_newsapi_articles(
    payload: dict[str, Any],
    request: ConsumerReportRequest,
    *,
    max_items: int = 20,
    cutoff: datetime | None = None,
    start_at: datetime | None = None,
) -> list[Evidence]:
    earliest, cutoff = _collection_window(
        request,
        cutoff=cutoff,
        start_at=start_at,
        lookback_days=10 if request.report_type == ReportType.TRANSFER_DAILY else 5,
    )
    publishers = _publisher_map()
    evidence: list[Evidence] = []
    seen_urls: set[str] = set()
    per_publisher: dict[str, int] = {}

    for item in payload.get("articles", []):
        title = _clean_text(item.get("title"), 500)
        url = _clean_text(item.get("url"), 1000)
        description = _clean_text(item.get("description"), 1200)
        if not title or not url:
            continue
        hostname = (urlparse(url).hostname or "").lower()
        publisher = _publisher_for_domain(hostname, publishers)
        if publisher is None or url in seen_urls:
            continue
        try:
            published_at = datetime.fromisoformat(
                str(item.get("publishedAt") or "").replace("Z", "+00:00")
            ).astimezone(UTC)
        except ValueError:
            continue
        if not earliest <= published_at < cutoff:
            continue
        publisher_id = str(publisher["id"])
        if per_publisher.get(publisher_id, 0) >= 4:
            continue
        seen_urls.add(url)
        per_publisher[publisher_id] = per_publisher.get(publisher_id, 0) + 1
        evidence.append(
            Evidence(
                id="newsapi-" + hashlib.sha256(url.encode()).hexdigest()[:12],
                title=title,
                url=url,
                published_at=published_at,
                source_name=str((item.get("source") or {}).get("name") or publisher_id),
                summary=(
                    f"NewsAPI 线索：{description or title}。当前只使用标题、链接、"
                    "发布时间和短描述，需在报告中保持线索/传闻标签。"
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


async def collect_newsapi_evidence(
    request: ConsumerReportRequest, *, max_items: int = 20
) -> list[Evidence]:
    api_key = os.getenv("NEWS_API_KEY")
    if not api_key:
        return []
    query = _newsapi_query(request)
    now = datetime.now(UTC)
    scope = scope_for_request(request)
    approved_domains = _approved_newsapi_domains()
    cache_key = json.dumps(
        {
            "query": query,
            "domains": approved_domains,
            "window_start": scope.window_start_utc.isoformat(),
            "cutoff": scope.data_cutoff_utc.isoformat(),
        },
        sort_keys=True,
    )
    cached = _newsapi_cache.get(cache_key)
    if cached and now - cached[0] < timedelta(minutes=15):
        payload = cached[1]
    else:
        timeout = httpx.Timeout(18.0, connect=7.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={
                "User-Agent": "FootPulse/0.4 newsapi-discovery",
                "X-Api-Key": api_key,
            },
        ) as client:
            response = await client.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": query,
                    "searchIn": "title,description",
                    "language": "en",
                    "domains": approved_domains,
                    "from": scope.window_start_utc.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "to": scope.data_cutoff_utc.isoformat().replace("+00:00", "Z"),
                    "pageSize": min(max(max_items * 2, 10), 50),
                    "sortBy": "publishedAt",
                },
            )
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise EvidenceCollectionError("NewsAPI 发现结果超过安全上限")
            payload = response.json()
            _newsapi_cache[cache_key] = (now, payload)
    return parse_newsapi_articles(
        payload,
        request,
        max_items=max_items,
        cutoff=scope.data_cutoff_utc,
        start_at=scope.window_start_utc,
    )


def _story_tokens(item: Evidence) -> set[str]:
    text = f"{item.title} {item.summary}".casefold()
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]{2,}", text)
        if token not in CLUSTER_STOP_WORDS
    }
    stages = {match.group(1).casefold() for match in TRANSFER_STAGE_RE.finditer(text)}
    return tokens | stages


def _story_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    overlap = left & right
    return len(overlap) / len(union)


def _same_story_cluster(tokens: set[str], known_tokens: set[str]) -> bool:
    overlap = tokens & known_tokens
    if _story_similarity(tokens, known_tokens) >= 0.42:
        return True
    shared_entities = overlap - TRANSFER_STAGE_WORDS
    shared_stage = overlap & TRANSFER_STAGE_WORDS
    return len(shared_entities) >= 2 and bool(shared_stage)


def _cluster_evidence(unique: list[Evidence]) -> list[Evidence]:
    cluster_tokens: list[tuple[str, set[str]]] = []
    for item in unique:
        tokens = _story_tokens(item)
        cluster_id = None
        for known_id, known_tokens in cluster_tokens:
            if _same_story_cluster(tokens, known_tokens):
                cluster_id = known_id
                known_tokens.update(tokens)
                break
        if cluster_id is None:
            signature = " ".join(sorted(tokens)) or str(item.url)
            cluster_id = "story-" + hashlib.sha256(signature.encode()).hexdigest()[:12]
            cluster_tokens.append((cluster_id, set(tokens)))
        item.story_cluster_id = cluster_id
    return unique


def _annotate_story_clusters(items: list[Evidence]) -> None:
    clusters: dict[str, list[Evidence]] = {}
    for item in items:
        if item.story_cluster_id:
            clusters.setdefault(item.story_cluster_id, []).append(item)
    for cluster_id, members in clusters.items():
        source_count = len(
            {member.source_independence_key or member.source_id for member in members}
        )
        lead_titles = [member.title for member in members[:3]]
        stage_hits = sorted(
            {
                match.group(1).casefold()
                for member in members
                for match in TRANSFER_STAGE_RE.finditer(
                    f"{member.title} {member.summary}"
                )
            }
        )
        stage = "、".join(stage_hits[:4]) if stage_hits else "待编辑判断"
        cluster_note = (
            f"事件簇 {cluster_id}：{len(members)} 条线索，"
            f"{source_count} 个独立来源，阶段关键词：{stage}。"
            f"同簇标题：{'；'.join(lead_titles)}。"
        )
        for member in members:
            if cluster_note not in member.summary:
                member.summary = _clean_text(f"{cluster_note}{member.summary}", 4000)


def _daily_item_category(item: Evidence) -> str:
    text = f"{item.title} {item.summary}".casefold()
    if re.search(r"transfer|signing|signs|bid|deal|target|medical|agreement", text):
        return "transfer"
    if re.search(
        r"goal|scored|beat|beats|defeat|defeats|fixture|result|knockout|var|"
        r"world cup|match|preview|lineup|highlights?",
        text,
    ):
        return "match"
    if re.search(r"coach|manager|tactics|viewership|fans|pubs|city|broadcast", text):
        return "context"
    return "other"


def _balanced_daily_selection(
    ranked: list[Evidence], *, max_items: int
) -> list[Evidence]:
    if max_items < 4:
        return ranked[:max_items]
    quotas = {
        "transfer": min(3, max_items),
        "match": min(3, max_items),
        "context": 1,
    }
    selected: list[Evidence] = []
    selected_ids: set[str] = set()

    def add_category(category: str, limit: int) -> None:
        for item in ranked:
            if len(selected) >= max_items or limit <= 0:
                return
            if item.id in selected_ids or _daily_item_category(item) != category:
                continue
            selected.append(item)
            selected_ids.add(item.id)
            limit -= 1

    for category, limit in quotas.items():
        add_category(category, limit)
    for item in ranked:
        if len(selected) >= max_items:
            break
        if item.id in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(item.id)
    return selected


def finish_evidence_selection(
    request: ConsumerReportRequest, combined: list[Evidence], *, max_items: int
) -> list[Evidence]:
    ranked: list[Evidence] = []
    seen: set[str] = set()
    critical_entities = matching_critical_entities(
        " ".join([request.subject, *request.focus])
    )
    critical_subject_mode = bool(critical_entities)
    if critical_subject_mode:
        critical_aliases = [
            alias
            for entity in critical_entities
            for alias in [entity.canonical_name, *entity.aliases]
        ]
        focused = [
            item
            for item in combined
            if item.id.startswith("critical-")
            or _contains_any_alias(f"{item.title} {item.summary}", critical_aliases)
        ]
        if focused:
            combined = focused
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
        if item.id.startswith("critical-"):
            score += 20 if critical_subject_mode else 1
        if item.verification_status != "unverified_lead":
            score += 3
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
        ranked.append(item)
    if request.report_type == ReportType.DAILY_FOOTBALL_DIGEST:
        unique = _balanced_daily_selection(ranked, max_items=max_items)
    else:
        unique = ranked[:max_items]
    _cluster_evidence(unique)
    _annotate_story_clusters(unique)
    return unique


def _contains_any_alias(text: str, aliases: list[str]) -> bool:
    folded = text.casefold()
    for alias in aliases:
        if re.search(r"[\u4e00-\u9fff]", alias):
            if alias in text:
                return True
            continue
        alias_folded = alias.casefold()
        if re.search(rf"(?<![a-z0-9]){re.escape(alias_folded)}(?![a-z0-9])", folded):
            return True
    return False


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
    newsapi_task = collect_newsapi_evidence(
        request, max_items=max(4, max_items - min(12, max_items // 2))
    )
    results = await asyncio.gather(
        guardian_task, bbc_task, gdelt_task, newsapi_task, return_exceptions=True
    )
    combined: list[Evidence] = []
    errors: list[Exception] = []
    for result in results:
        if isinstance(result, BaseException):
            if isinstance(result, Exception):
                errors.append(result)
            continue
        combined.extend(result)

    unique = finish_evidence_selection(request, combined, max_items=max_items)
    if len(unique) < 2:
        raise EvidenceCollectionError(
            "没有找到足够的近期资料，请写明球队英文名或更具体的主题"
        ) from (errors[0] if errors else None)
    return unique
