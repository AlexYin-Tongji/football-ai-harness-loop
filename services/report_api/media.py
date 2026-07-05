from __future__ import annotations

import asyncio
import hashlib
import html
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from services.report_api.domain import GeneratedReport, MediaAsset, ReportSection

TAG_RE = re.compile(r"<[^>]+>")
APPROVED_COMMONS_LICENSES = (
    "cc by",
    "cc0",
    "public domain",
)
NAME_STOP_WORDS = {"jr", "sr", "ii", "iii", "iv", "de", "da", "dos", "van", "von"}
VIDEO_QUERY_STOP_WORDS = {
    "fifa",
    "football",
    "highlights",
    "soccer",
    "world",
    "cup",
    "official",
    "match",
}
MATCH_SECTION_RE = re.compile(
    r"世界杯|比赛|淘汰赛|进球|VAR|点球|绝杀|高光|晋级|击败|战胜|"
    r"\b(?:beat|defeat|win|won|goal|scored|var|penalty|highlights?)\b",
    re.I,
)
LATIN_QUERY_REPLACEMENTS = {
    "葡萄牙": "Portugal",
    "克罗地亚": "Croatia",
    "西班牙": "Spain",
    "奥地利": "Austria",
    "英格兰": "England",
    "墨西哥": "Mexico",
    "澳大利亚": "Australia",
    "埃及": "Egypt",
    "瑞士": "Switzerland",
    "阿尔及利亚": "Algeria",
    "日本": "Japan",
    "热刺": "Tottenham",
    "托特纳姆": "Tottenham",
    "西汉姆联": "West Ham",
    "拉莫斯": "Ramos",
    "贡萨洛": "Goncalo",
    "C罗": "Cristiano Ronaldo",
    "罗纳尔多": "Ronaldo",
    "费尔南德斯": "Fernandes",
}
NATIONAL_TEAM_NAMES = (
    "Portugal",
    "Croatia",
    "Spain",
    "Austria",
    "England",
    "Mexico",
    "Australia",
    "Egypt",
    "Switzerland",
    "Algeria",
    "Japan",
)
GOAL_PLAYER_QUERIES = {
    "Ramos": "Goncalo Ramos Goal",
    "Goncalo Ramos": "Goncalo Ramos Goal",
    "Ronaldo": "Cristiano Ronaldo Goal",
    "Cristiano Ronaldo": "Cristiano Ronaldo Goal",
}
LOCAL_MEDIA_HOSTS = {"upload.wikimedia.org", "i.ytimg.com", "img.youtube.com"}
LOCAL_MEDIA_MAX_BYTES = 3_000_000


def _plain(value: str | None, limit: int = 500) -> str:
    return html.unescape(TAG_RE.sub(" ", value or "")).strip()[:limit]


def _media_cache_dir() -> Path:
    root = Path(__file__).resolve().parents[2]
    path = root / "artifacts" / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _thumbnail_extension(url: str, content_type: str | None) -> str:
    lowered = (content_type or "").lower()
    if "png" in lowered:
        return ".png"
    if "webp" in lowered:
        return ".webp"
    if "jpeg" in lowered or "jpg" in lowered:
        return ".jpg"
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


async def cache_media_thumbnail(
    asset: MediaAsset, transport: httpx.AsyncBaseTransport | None = None
) -> MediaAsset:
    if not asset.thumbnail_url:
        return asset
    url = str(asset.thumbnail_url)
    parsed = urlparse(url)
    if parsed.hostname not in LOCAL_MEDIA_HOSTS:
        return asset
    timeout = httpx.Timeout(15.0, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.get(url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if not content_type.lower().startswith("image/"):
                return asset
            content = response.content
            if not content or len(content) > LOCAL_MEDIA_MAX_BYTES:
                return asset
    except httpx.HTTPError:
        return asset
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    ext = _thumbnail_extension(url, content_type)
    filename = f"{digest}{ext}"
    target = _media_cache_dir() / filename
    if not target.exists():
        target.write_bytes(content)
    return asset.model_copy(
        update={
            "local_thumbnail_url": f"/media/{filename}",
            "local_path": str(target),
        }
    )


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


def _video_query_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", _fold(value))
        if token not in VIDEO_QUERY_STOP_WORDS and re.search(r"[a-z]", token)
    }


def _video_matches_query(query: str, title: str) -> bool:
    tokens = _video_query_tokens(query)
    if not tokens:
        return False
    title_tokens = _video_query_tokens(title)
    overlap = tokens & title_tokens
    if len(tokens) >= 2:
        return len(overlap) >= 2
    return bool(overlap)


def _video_match_score(query: str, title: str) -> int:
    tokens = _video_query_tokens(query)
    title_tokens = _video_query_tokens(title)
    overlap = tokens & title_tokens
    if not overlap:
        return 0
    folded_query = _fold(query)
    folded_title = _fold(title)
    score = len(overlap) * 10
    if "highlight" in folded_title:
        score += 14
    if "post-show" in folded_title or "post show" in folded_title:
        score += 8
    if re.search(r"\bround\s+of\s+\d+\b", folded_title):
        score += 6
    if re.search(r"\b(?:vs?|versus)\b", folded_title):
        score += 4
    if re.search(
        r"ruled out|equaliser|equalizer|simple explanation", folded_title
    ) and not re.search(r"var|equaliser|equalizer|ruled", folded_query):
        score -= 8
    if re.search(r"\b(?:train|training|preview|press conference)\b", folded_title):
        score -= 12
    if "#fifaworldcuponyt" in folded_title:
        score -= 6
    if len(title) < 36:
        score -= 4
    return score


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
    candidates: list[tuple[int, int, MediaAsset]] = []
    candidate_index = 0
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=transport
    ) as client:
        for channel_id in channel_ids[:8]:
            try:
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
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    return await search_official_youtube_feed_video(
                        query, channel_ids, transport=transport
                    )
                raise
            for item in response.json().get("items", []):
                video_id = (item.get("id") or {}).get("videoId")
                snippet = item.get("snippet") or {}
                if not video_id:
                    continue
                title = _plain(snippet.get("title"), 300)
                if not _video_matches_query(query, title):
                    continue
                score = _video_match_score(query, title)
                thumbnails = snippet.get("thumbnails") or {}
                selected_thumbnail = (
                    thumbnails.get("high") or thumbnails.get("medium") or {}
                )
                thumbnail = selected_thumbnail.get("url")
                candidates.append(
                    (
                        score,
                        -candidate_index,
                        MediaAsset(
                            asset_type="video",
                            title=title,
                            url=f"https://www.youtube.com/watch?v={video_id}",
                            embed_url=f"https://www.youtube.com/embed/{video_id}",
                            thumbnail_url=thumbnail,
                            provider="YouTube official channel",
                            license="YouTube embeddable link",
                            attribution=_plain(snippet.get("channelTitle"), 200),
                            rights_status="approved",
                            relevance_status="metadata_match",
                            relevance_reason=(
                                "视频来自人工白名单官方频道，且标题由 YouTube "
                                "查询返回；"
                                "未下载或重新托管视频。"
                            ),
                        ),
                    )
                )
                candidate_index += 1
    if candidates:
        return max(candidates, key=lambda item: (item[0], item[1]))[2]
    return await search_official_youtube_feed_video(
        query, channel_ids, transport=transport
    )


YOUTUBE_FEED_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


async def search_official_youtube_feed_video(
    query: str,
    channel_ids: list[str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> MediaAsset | None:
    """Fallback to the same allowlisted channels' public video feeds."""
    timeout = httpx.Timeout(12.0, connect=5.0)
    candidates: list[tuple[int, int, MediaAsset]] = []
    candidate_index = 0
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=transport
    ) as client:
        for channel_id in channel_ids[:8]:
            response = await client.get(
                "https://www.youtube.com/feeds/videos.xml",
                params={"channel_id": channel_id},
            )
            try:
                response.raise_for_status()
                root = ET.fromstring(response.content)
            except (httpx.HTTPError, ET.ParseError):
                continue
            channel_title = _plain(
                root.findtext("atom:title", default="", namespaces=YOUTUBE_FEED_NS),
                200,
            )
            for entry in root.findall("atom:entry", YOUTUBE_FEED_NS)[:20]:
                title = _plain(
                    entry.findtext(
                        "atom:title", default="", namespaces=YOUTUBE_FEED_NS
                    ),
                    300,
                )
                if not _video_matches_query(query, title):
                    continue
                video_id = entry.findtext(
                    "yt:videoId", default="", namespaces=YOUTUBE_FEED_NS
                )
                if not video_id:
                    continue
                thumbnail_el = entry.find(
                    "media:group/media:thumbnail", YOUTUBE_FEED_NS
                )
                thumbnail = (
                    thumbnail_el.get("url") if thumbnail_el is not None else None
                )
                if thumbnail:
                    thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                score = _video_match_score(query, title)
                candidates.append(
                    (
                        score,
                        -candidate_index,
                        MediaAsset(
                            asset_type="video",
                            title=title,
                            url=f"https://www.youtube.com/watch?v={video_id}",
                            embed_url=f"https://www.youtube.com/embed/{video_id}",
                            thumbnail_url=thumbnail,
                            provider="YouTube official channel",
                            license="YouTube channel feed link",
                            attribution=channel_title or "YouTube official channel",
                            rights_status="approved",
                            relevance_status="metadata_match",
                            relevance_reason=(
                                "视频来自人工白名单官方频道公开 feed；只保存标题、"
                                "视频链接和缩略图元数据，未下载或重新托管视频。"
                            ),
                        ),
                    )
                )
                candidate_index += 1
    if candidates:
        return max(candidates, key=lambda item: (item[0], item[1]))[2]
    return None


def _latin_query(value: str) -> str:
    for source, target in LATIN_QUERY_REPLACEMENTS.items():
        value = value.replace(source, f" {target} ")
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if ord(char) < 128)
    return SPACE_RE.sub(" ", re.sub(r"[^A-Za-z0-9 '|-]", " ", text)).strip()


SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class VideoRequest:
    query: str
    placement: str
    target: str
    evidence_ids: list[str]


@dataclass(frozen=True)
class ImageRequest:
    target: str
    placement: str
    evidence_ids: list[str]


def _known_teams(value: str) -> list[str]:
    latin = _latin_query(value)
    found = [
        team
        for team in NATIONAL_TEAM_NAMES
        if re.search(rf"(?<![A-Za-z]){re.escape(team)}(?![A-Za-z])", latin)
    ]
    return list(dict.fromkeys(found))


def _section_video_query(section: ReportSection) -> str:
    text = f"{section.heading} {section.body[:240]}"
    if section.category == "match":
        teams = _known_teams(text)
        if len(teams) >= 2:
            return " ".join([*teams[:2], "FIFA World Cup 2026 Highlights"])
    return _latin_query(text)


def _section_goal_video_requests(
    section: ReportSection, excluded_players: set[str] | None = None
) -> list[VideoRequest]:
    text = f"{section.heading} {section.body[:360]}"
    if not re.search(r"进球|破门|点球|制胜|扳平|goal|penalty|winner", text, re.I):
        return []
    latin = _latin_query(text)
    teams = _known_teams(text)
    suffix = " ".join([*teams[:2], "FIFA World Cup 2026"]).strip()
    requests: list[VideoRequest] = []
    excluded_players = excluded_players or set()
    for token, prefix in GOAL_PLAYER_QUERIES.items():
        if any(part in excluded_players for part in _name_tokens(token)):
            continue
        if re.search(rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])", latin):
            query = SPACE_RE.sub(" ", f"{prefix} {suffix}").strip()
            requests.append(
                VideoRequest(
                    query=query,
                    placement="section",
                    target=prefix,
                    evidence_ids=section.evidence_ids,
                )
            )
    return requests[:2]


IMAGE_TARGET_BLOCKLIST = {
    "BBC Sport",
    "The Guardian",
    "World Cup",
    "FIFA World",
    "FIFA World Cup",
    "Premier League",
    "Champions League",
    "YouTube",
}
VENUE_IMAGE_TARGETS = (
    "Estadio Azteca",
    "Mexico City Stadium",
)


def _section_image_targets(report: GeneratedReport) -> list[ImageRequest]:
    requests: list[ImageRequest] = []
    seen: set[str] = set()
    spotlight_targets = {
        (spotlight.media_search_name or spotlight.name).casefold()
        for spotlight in report.enrichment.player_spotlights
    }
    for section in report.sections[:6]:
        text = f"{section.heading} {section.body}"
        candidates = re.findall(
            r"\b[A-Z][A-Za-zÀ-ÿ'’.-]{2,}(?:\s+[A-Z][A-Za-zÀ-ÿ'’.-]{2,}){1,3}\b",
            text,
        )
        for venue in VENUE_IMAGE_TARGETS:
            if venue in text:
                candidates.append(venue)
        for candidate in candidates:
            normalized = SPACE_RE.sub(" ", candidate).strip()
            key = normalized.casefold()
            if (
                not normalized
                or key in seen
                or key in spotlight_targets
                or normalized in IMAGE_TARGET_BLOCKLIST
                or any(block.casefold() in key for block in IMAGE_TARGET_BLOCKLIST)
            ):
                continue
            if len(_name_tokens(normalized)) < 2:
                continue
            requests.append(
                ImageRequest(
                    target=normalized,
                    placement="section",
                    evidence_ids=section.evidence_ids,
                )
            )
            seen.add(key)
            if len(requests) >= 4:
                return requests
    return requests


def _report_video_requests(report: GeneratedReport) -> list[VideoRequest]:
    requests: list[VideoRequest] = []
    for spotlight in report.enrichment.player_spotlights[:2]:
        query = f"{spotlight.media_search_name or spotlight.name} football"
        requests.append(
            VideoRequest(
                query=query,
                placement="section",
                target=spotlight.name,
                evidence_ids=spotlight.evidence_ids,
            )
        )
    cover_added = False
    goal_requests: list[VideoRequest] = []
    timeline_players = {
        token
        for event in report.enrichment.match_timeline
        for token in _name_tokens(event.player or "")
    }
    for section in report.sections[:5]:
        section_text = f"{section.heading} {section.body}"
        query = _section_video_query(section)
        if not query:
            continue
        if not cover_added and section.category == "match" and MATCH_SECTION_RE.search(
            section_text
        ) and len(
            _known_teams(section_text)
        ) >= 2:
            cover_added = True
            requests.append(
                VideoRequest(
                    query=query,
                    placement="report_cover",
                    target=section.heading,
                    evidence_ids=section.evidence_ids,
                )
            )
        goal_requests.extend(_section_goal_video_requests(section, timeline_players))
    requests.extend(goal_requests[:3])
    for event in report.enrichment.match_timeline[:4]:
        query = _latin_query(
            " ".join(
                item
                for item in [
                    event.team,
                    event.player,
                    event.description[:160],
                    event.score_after,
                ]
                if item
            )
        )
        if query:
            requests.append(
                VideoRequest(
                    query=query,
                    placement="timeline",
                    target=event.description[:140],
                    evidence_ids=event.evidence_ids,
                )
            )
    has_match_section = any(section.category == "match" for section in report.sections)
    title_query = _latin_query(report.title)
    if title_query and not cover_added and has_match_section:
        requests.append(
            VideoRequest(
                query=title_query,
                placement="report_cover",
                target=report.title,
                evidence_ids=[],
            )
        )

    deduped: list[VideoRequest] = []
    seen: set[str] = set()
    for request in requests:
        key = SPACE_RE.sub(" ", request.query.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(request)
    return deduped[:8]


async def collect_report_media(
    report: GeneratedReport,
    *,
    youtube_api_key: str | None = None,
    youtube_channel_ids: list[str] | None = None,
) -> list[MediaAsset]:
    assets: list[MediaAsset] = []
    seen_urls: set[str] = set()
    spotlight_requests = [
        spotlight
        for spotlight in report.enrichment.player_spotlights[:3]
        if _commons_queries(spotlight.media_search_name or spotlight.name)
    ]
    image_results = await asyncio.gather(
        *(
            search_commons_player_image(spotlight.media_search_name or spotlight.name)
            for spotlight in spotlight_requests
        ),
        return_exceptions=True,
    )
    for spotlight, item in zip(spotlight_requests, image_results, strict=False):
        if isinstance(item, MediaAsset) and str(item.url) not in seen_urls:
            item = await cache_media_thumbnail(item)
            assets.append(
                item.model_copy(
                    update={
                        "placement": "spotlight",
                        "target": spotlight.name,
                        "evidence_ids": spotlight.evidence_ids,
                    }
                )
            )
            seen_urls.add(str(item.url))
    section_image_requests = [
        request
        for request in _section_image_targets(report)
        if _commons_queries(request.target)
    ][:4]
    section_image_results = await asyncio.gather(
        *(
            search_commons_player_image(request.target)
            for request in section_image_requests
        ),
        return_exceptions=True,
    )
    for request, item in zip(
        section_image_requests, section_image_results, strict=False
    ):
        if isinstance(item, MediaAsset) and str(item.url) not in seen_urls:
            item = await cache_media_thumbnail(item)
            assets.append(
                item.model_copy(
                    update={
                        "placement": request.placement,
                        "target": request.target,
                        "evidence_ids": request.evidence_ids,
                    }
                )
            )
            seen_urls.add(str(item.url))
    if youtube_api_key and youtube_channel_ids:
        video_count = 0
        for request in _report_video_requests(report):
            try:
                video = await search_official_youtube_video(
                    request.query, youtube_api_key, youtube_channel_ids
                )
            except (httpx.HTTPError, ValueError, KeyError):
                video = None
            if video:
                if str(video.url) not in seen_urls:
                    video = await cache_media_thumbnail(video)
                    assets.append(
                        video.model_copy(
                            update={
                                "placement": request.placement,
                                "target": request.target,
                                "evidence_ids": request.evidence_ids,
                            }
                        )
                    )
                    seen_urls.add(str(video.url))
                    video_count += 1
                if video_count >= 3:
                    break
    return assets[:6]
