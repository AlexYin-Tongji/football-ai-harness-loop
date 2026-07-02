from __future__ import annotations

import asyncio
import html
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from services.report_api.domain import GeneratedReport, MediaAsset

TAG_RE = re.compile(r"<[^>]+>")
APPROVED_COMMONS_LICENSES = (
    "cc by",
    "cc0",
    "public domain",
)
NAME_STOP_WORDS = {"jr", "sr", "ii", "iii", "iv", "de", "da", "dos", "van", "von"}


def _plain(value: str | None, limit: int = 500) -> str:
    return html.unescape(TAG_RE.sub(" ", value or "")).strip()[:limit]


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _name_tokens(name: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9'-]{2,}", _fold(name))
        if token not in NAME_STOP_WORDS
    ]


def _metadata_text(title: str, metadata: dict[str, Any]) -> str:
    fields = [title]
    for key in (
        "ObjectName",
        "ImageDescription",
        "Categories",
        "DepictedPeople",
        "Credit",
    ):
        value = (metadata.get(key) or {}).get("value")
        if value:
            fields.append(_plain(str(value), 1200))
    return _fold(" ".join(fields))


def _metadata_matches_name(name: str, title: str, metadata: dict[str, Any]) -> bool:
    tokens = _name_tokens(name)
    if not tokens:
        return False
    text = _metadata_text(title, metadata)
    full_name = " ".join(tokens)
    if full_name in text:
        return True
    if len(tokens) >= 2 and all(token in text for token in tokens[:2]):
        return True
    return len(tokens) >= 3 and sum(token in text for token in tokens) >= 3


def _license_allowed(license_name: str) -> bool:
    normalized = _fold(license_name)
    return normalized.startswith(APPROVED_COMMONS_LICENSES)


def _commons_queries(name: str) -> list[str]:
    tokens = _name_tokens(name)
    if len(tokens) < 2:
        return []
    normalized = " ".join(tokens)
    return [
        f'"{name}" filetype:bitmap',
        f'"{normalized}" football filetype:bitmap',
        f'"{normalized}" "association football" filetype:bitmap',
        f'"{tokens[-1]}" "{tokens[0]}" football filetype:bitmap',
    ]


async def search_commons_player_image(
    name: str, transport: httpx.AsyncBaseTransport | None = None
) -> MediaAsset | None:
    """Return one license-filtered Commons image with metadata name relevance."""
    queries = _commons_queries(name)
    if not queries:
        return None
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=transport
    ) as client:
        for query in queries:
            response = await client.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": query,
                    "gsrnamespace": 6,
                    "gsrlimit": 8,
                    "prop": "imageinfo",
                    "iiprop": "url|extmetadata",
                    "iiurlwidth": 960,
                    "format": "json",
                    "formatversion": 2,
                },
                headers={
                    "User-Agent": (
                        "FootPulse/0.5 licensed-media-reader "
                        "(https://github.com/AlexYin-Tongji/"
                        "football-ai-harness-loop)"
                    )
                },
            )
            response.raise_for_status()
            asset = _select_commons_asset(name, response.json())
            if asset:
                return asset
    return None


def _select_commons_asset(name: str, payload: dict[str, Any]) -> MediaAsset | None:
    surname = re.findall(r"[\w'-]+", _fold(name))[-1:]
    for page in payload.get("query", {}).get("pages", []):
        title = str(page.get("title") or "")
        info = (page.get("imageinfo") or [{}])[0]
        metadata: dict[str, Any] = info.get("extmetadata") or {}
        if not _metadata_matches_name(name, title, metadata):
            if surname and surname[0] not in _metadata_text(title, metadata):
                continue
            # A surname-only hit is too risky to attach automatically.
            continue
        license_name = _plain(
            (metadata.get("LicenseShortName") or {}).get("value"), 120
        )
        if not _license_allowed(license_name):
            continue
        original_url = info.get("descriptionurl")
        thumbnail_url = info.get("thumburl") or info.get("url")
        if not original_url or not thumbnail_url:
            continue
        artist = _plain((metadata.get("Artist") or {}).get("value"))
        credit = _plain((metadata.get("Credit") or {}).get("value"))
        attribution = " · ".join(item for item in [artist, credit] if item)
        return MediaAsset(
            asset_type="image",
            title=_plain(title.removeprefix("File:"), 300),
            url=original_url,
            thumbnail_url=thumbnail_url,
            provider="Wikimedia Commons",
            license=license_name,
            attribution=attribution or "见 Wikimedia Commons 文件页",
            rights_status="review_required",
            relevance_status="metadata_match",
            relevance_reason=(
                "Commons 标题或元数据匹配目标姓名；尚未做视觉身份识别，"
                "发布前仍需人工确认。"
            ),
        )
    return None


async def search_official_youtube_video(
    query: str,
    api_key: str,
    channel_ids: list[str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> MediaAsset | None:
    """Search only manually allowlisted official channels for embeddable videos."""
    timeout = httpx.Timeout(15.0, connect=5.0)
    published_after = (
        (datetime.now(UTC) - timedelta(days=14)).isoformat().replace("+00:00", "Z")
    )
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=transport
    ) as client:
        for channel_id in channel_ids[:8]:
            response = await client.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "key": api_key,
                    "part": "snippet",
                    "type": "video",
                    "q": query[:180],
                    "channelId": channel_id,
                    "maxResults": 3,
                    "order": "relevance",
                    "publishedAfter": published_after,
                    "safeSearch": "strict",
                    "videoEmbeddable": "true",
                    "videoSyndicated": "true",
                    "relevanceLanguage": "en",
                },
            )
            response.raise_for_status()
            for item in response.json().get("items", []):
                video_id = (item.get("id") or {}).get("videoId")
                snippet = item.get("snippet") or {}
                if not video_id:
                    continue
                thumbnails = snippet.get("thumbnails") or {}
                selected_thumbnail = (
                    thumbnails.get("high") or thumbnails.get("medium") or {}
                )
                thumbnail = selected_thumbnail.get("url")
                return MediaAsset(
                    asset_type="video",
                    title=_plain(snippet.get("title"), 300),
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    thumbnail_url=thumbnail,
                    provider="YouTube official channel",
                    license="YouTube embeddable link",
                    attribution=_plain(snippet.get("channelTitle"), 200),
                    rights_status="approved",
                    relevance_status="metadata_match",
                    relevance_reason=(
                        "视频来自人工白名单官方频道，且标题由 YouTube 查询返回；"
                        "未下载或重新托管视频。"
                    ),
                )
    return None


def _latin_query(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if ord(char) < 128)
    return SPACE_RE.sub(" ", re.sub(r"[^A-Za-z0-9 '|-]", " ", text)).strip()


SPACE_RE = re.compile(r"\s+")


def _video_queries(report: GeneratedReport) -> list[str]:
    candidates = [
        report.title,
        _latin_query(report.title),
        "FIFA World Cup highlights",
        "World Cup match highlights",
    ]
    for spotlight in report.enrichment.player_spotlights[:2]:
        candidates.append(f"{spotlight.media_search_name or spotlight.name} football")
    for section in report.sections[:2]:
        candidates.append(_latin_query(f"{section.heading} {section.body[:160]}"))
    return [
        item[:180]
        for item in dict.fromkeys(candidate for candidate in candidates if candidate)
    ][:6]


async def collect_report_media(
    report: GeneratedReport,
    *,
    youtube_api_key: str | None = None,
    youtube_channel_ids: list[str] | None = None,
) -> list[MediaAsset]:
    assets: list[MediaAsset] = []
    seen_urls: set[str] = set()
    image_results = await asyncio.gather(
        *(
            search_commons_player_image(spotlight.media_search_name or spotlight.name)
            for spotlight in report.enrichment.player_spotlights[:3]
            if _commons_queries(spotlight.media_search_name or spotlight.name)
        ),
        return_exceptions=True,
    )
    for item in image_results:
        if isinstance(item, MediaAsset) and str(item.url) not in seen_urls:
            assets.append(item)
            seen_urls.add(str(item.url))
    if youtube_api_key and youtube_channel_ids:
        for query in _video_queries(report):
            try:
                video = await search_official_youtube_video(
                    query, youtube_api_key, youtube_channel_ids
                )
            except (httpx.HTTPError, ValueError, KeyError):
                video = None
            if video:
                if str(video.url) not in seen_urls:
                    assets.append(video)
                break
    return assets[:4]
