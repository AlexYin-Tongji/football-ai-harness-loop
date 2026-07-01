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
    "CC BY",
    "CC0",
    "Public domain",
)


def _plain(value: str | None, limit: int = 500) -> str:
    return html.unescape(TAG_RE.sub(" ", value or "")).strip()[:limit]


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


async def search_commons_player_image(
    name: str, transport: httpx.AsyncBaseTransport | None = None
) -> MediaAsset | None:
    """Return one license-filtered Commons image whose title matches the player."""
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=transport
    ) as client:
        response = await client.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": f'"{name}" filetype:bitmap',
                "gsrnamespace": 6,
                "gsrlimit": 6,
                "prop": "imageinfo",
                "iiprop": "url|extmetadata",
                "iiurlwidth": 960,
                "format": "json",
                "formatversion": 2,
            },
            headers={
                "User-Agent": (
                    "FootPulse/0.4 licensed-media-reader "
                    "(https://github.com/AlexYin-Tongji/"
                    "football-ai-harness-loop)"
                )
            },
        )
        response.raise_for_status()
        payload = response.json()
    surname = re.findall(r"[\w'-]+", _fold(name))[-1:]
    for page in payload.get("query", {}).get("pages", []):
        title = str(page.get("title") or "")
        if surname and surname[0] not in _fold(title):
            continue
        info = (page.get("imageinfo") or [{}])[0]
        metadata: dict[str, Any] = info.get("extmetadata") or {}
        license_name = _plain(
            (metadata.get("LicenseShortName") or {}).get("value"), 120
        )
        if not license_name.startswith(APPROVED_COMMONS_LICENSES):
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
    published_after = (datetime.now(UTC) - timedelta(days=14)).isoformat().replace(
        "+00:00", "Z"
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
                )
    return None


async def collect_report_media(
    report: GeneratedReport,
    *,
    youtube_api_key: str | None = None,
    youtube_channel_ids: list[str] | None = None,
) -> list[MediaAsset]:
    assets: list[MediaAsset] = []
    image_results = await asyncio.gather(
        *(
            search_commons_player_image(spotlight.media_search_name or spotlight.name)
            for spotlight in report.enrichment.player_spotlights[:3]
        ),
        return_exceptions=True,
    )
    assets.extend(
        item for item in image_results if isinstance(item, MediaAsset)
    )
    if youtube_api_key and youtube_channel_ids:
        try:
            video = await search_official_youtube_video(
                report.title, youtube_api_key, youtube_channel_ids
            )
        except (httpx.HTTPError, ValueError, KeyError):
            video = None
        if video:
            assets.append(video)
    return assets[:4]
