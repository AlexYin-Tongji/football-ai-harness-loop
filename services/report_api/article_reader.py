from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from services.mcp_servers.common import load_publisher_registry

BLOCK_RE = re.compile(
    r"(?is)<(script|style|noscript|svg|header|footer|nav|aside)\b[^>]*>.*?</\1>"
)
ARTICLE_RE = re.compile(r"(?is)<article\b[^>]*>(.*?)</article>")
P_RE = re.compile(r"(?is)<p\b[^>]*>(.*?)</p>")
TAG_RE = re.compile(r"(?s)<[^>]+>")
SPACE_RE = re.compile(r"\s+")
JSON_LD_RE = re.compile(
    r"(?is)<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>"
)
META_RE = re.compile(
    r"(?is)<meta[^>]+(?:name|property)=[\"'](?:description|og:description)"
    r"[\"'][^>]+content=[\"'](.*?)[\"'][^>]*>"
)
MAX_HTML_BYTES = 2_000_000
ARTICLE_CACHE_TTL = timedelta(minutes=30)
_article_cache: dict[str, tuple[datetime, ArticleExcerpt | None]] = {}


@dataclass(frozen=True)
class ArticleExcerpt:
    url: str
    text: str
    chars_read: int


def _clean_text(value: str | None, limit: int = 6000) -> str:
    cleaned = html.unescape(TAG_RE.sub(" ", value or ""))
    return SPACE_RE.sub(" ", cleaned).strip()[:limit]


def _approved_domains() -> set[str]:
    return {
        str(item.get("domain") or "").lower().removeprefix("www.")
        for item in load_publisher_registry()["publishers"]
        if item.get("access") != "metadata_only_subscription_required"
    }


def _is_approved_url(value: str) -> bool:
    host = (urlparse(value).hostname or "").lower().removeprefix("www.")
    if not host:
        return False
    return any(
        host == domain or host.endswith(f".{domain}") for domain in _approved_domains()
    )


def _flatten_json_ld(value: object) -> list[str]:
    snippets: list[str] = []
    if isinstance(value, dict):
        for key in ("articleBody", "description", "headline"):
            text = value.get(key)
            if isinstance(text, str):
                snippets.append(text)
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                snippets.extend(_flatten_json_ld(item))
    elif isinstance(value, list):
        for item in value:
            snippets.extend(_flatten_json_ld(item))
    return snippets


def _json_ld_text(document: str) -> str:
    snippets: list[str] = []
    for match in JSON_LD_RE.finditer(document):
        raw = html.unescape(match.group(1)).strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        snippets.extend(_flatten_json_ld(payload))
    return _clean_text(" ".join(snippets))


def extract_article_text(document: str, *, max_chars: int = 6000) -> str:
    """Extract a bounded article excerpt for summarization, not archival storage."""
    json_ld = _json_ld_text(document)
    if len(json_ld) >= 220:
        return json_ld[:max_chars]

    stripped = BLOCK_RE.sub(" ", document)
    article_blocks = ARTICLE_RE.findall(stripped)
    paragraphs: list[str] = []
    for block in article_blocks or [stripped]:
        paragraphs.extend(_clean_text(match, 1200) for match in P_RE.findall(block))
        if len(" ".join(paragraphs)) >= max_chars:
            break

    meta = [_clean_text(match, 500) for match in META_RE.findall(document)]
    text = _clean_text(" ".join([*meta[:2], *paragraphs]), max_chars)
    return text


async def read_article_excerpt(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    max_chars: int = 6000,
) -> ArticleExcerpt | None:
    if not _is_approved_url(url):
        return None

    now = datetime.now(UTC)
    cached = _article_cache.get(url)
    if cached and now - cached[0] < ARTICLE_CACHE_TTL:
        return cached[1]

    timeout = httpx.Timeout(12.0, connect=5.0)
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=3,
            transport=transport,
            headers={"User-Agent": "FootPulse/0.6 article-excerpt-reader"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException):
        _article_cache[url] = (now, None)
        return None

    if not _is_approved_url(str(response.url)):
        _article_cache[url] = (now, None)
        return None
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and content_type:
        _article_cache[url] = (now, None)
        return None
    document = response.content[:MAX_HTML_BYTES].decode(
        response.encoding or "utf-8", errors="replace"
    )
    text = extract_article_text(document, max_chars=max_chars)
    excerpt = (
        ArticleExcerpt(str(response.url), text, len(text)) if len(text) >= 80 else None
    )
    _article_cache[url] = (now, excerpt)
    if len(_article_cache) > 80:
        for key in list(_article_cache)[:20]:
            _article_cache.pop(key, None)
    return excerpt
