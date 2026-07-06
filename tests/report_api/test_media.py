from __future__ import annotations

import asyncio

import httpx

from services.report_api.domain import GeneratedReport, MediaAsset
from services.report_api.media import (
    cache_media_thumbnail,
    collect_report_media,
    search_commons_player_image,
    search_official_youtube_video,
)


def test_commons_image_requires_license_and_attribution() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "commons.wikimedia.org"
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {
                            "title": "File:Example Player 2026.jpg",
                            "imageinfo": [
                                {
                                    "descriptionurl": (
                                        "https://commons.wikimedia.org/wiki/"
                                        "File:Example_Player_2026.jpg"
                                    ),
                                    "thumburl": (
                                        "https://upload.wikimedia.org/example.jpg"
                                    ),
                                    "extmetadata": {
                                        "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                        "Artist": {"value": "<b>Photographer</b>"},
                                        "Credit": {"value": "Wikimedia Commons"},
                                    },
                                }
                            ],
                        }
                    ]
                }
            },
        )

    asset = asyncio.run(
        search_commons_player_image(
            "Example Player", transport=httpx.MockTransport(handler)
        )
    )

    assert asset is not None
    assert asset.rights_status == "review_required"
    assert asset.relevance_status == "metadata_match"
    assert asset.license == "CC BY-SA 4.0"
    assert "Photographer" in asset.attribution


def test_cache_media_thumbnail_downloads_approved_image_host() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "upload.wikimedia.org"
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=b"\xff\xd8\xff\xe0test-image",
        )

    asset = MediaAsset(
        asset_type="image",
        title="Example Player",
        url="https://commons.wikimedia.org/wiki/File:Example.jpg",
        thumbnail_url="https://upload.wikimedia.org/example.jpg",
        provider="Wikimedia Commons",
        license="CC BY-SA 4.0",
        attribution="Photographer",
        rights_status="review_required",
        relevance_status="metadata_match",
    )

    cached = asyncio.run(
        cache_media_thumbnail(asset, transport=httpx.MockTransport(handler))
    )

    assert cached.local_thumbnail_url
    assert cached.local_thumbnail_url.startswith("/media/")
    assert cached.local_path


def test_commons_image_rejects_surname_only_match() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {
                            "title": "File:Player 2026.jpg",
                            "imageinfo": [
                                {
                                    "descriptionurl": (
                                        "https://commons.wikimedia.org/wiki/"
                                        "File:Player_2026.jpg"
                                    ),
                                    "thumburl": (
                                        "https://upload.wikimedia.org/player.jpg"
                                    ),
                                    "extmetadata": {
                                        "LicenseShortName": {"value": "CC BY 4.0"},
                                    },
                                }
                            ],
                        }
                    ]
                }
            },
        )

    asset = asyncio.run(
        search_commons_player_image(
            "Example Player", transport=httpx.MockTransport(handler)
        )
    )

    assert asset is None


def test_youtube_video_is_limited_to_allowlisted_channel() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["channelId"] == "official-channel"
        assert request.url.params["videoEmbeddable"] == "true"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": {"videoId": "video-123"},
                        "snippet": {
                            "title": "England official match highlights",
                            "channelTitle": "Official Football Channel",
                            "thumbnails": {
                                "high": {
                                    "url": "https://i.ytimg.com/vi/video-123/hq.jpg"
                                }
                            },
                        },
                    }
                ]
            },
        )

    asset = asyncio.run(
        search_official_youtube_video(
            "England match highlights",
            "test-key",
            ["official-channel"],
            transport=httpx.MockTransport(handler),
        )
    )

    assert asset is not None
    assert asset.asset_type == "video"
    assert "video-123" in str(asset.url)
    assert asset.rights_status == "approved"
    assert asset.relevance_status == "metadata_match"


def test_youtube_video_rejects_unrelated_official_result() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": {"videoId": "video-123"},
                        "snippet": {
                            "title": "England 2-1 Congo DR highlights",
                            "channelTitle": "FIFA",
                        },
                    }
                ]
            },
        )

    asset = asyncio.run(
        search_official_youtube_video(
            "Portugal Croatia Diogo Jota tribute",
            "test-key",
            ["official-channel"],
            transport=httpx.MockTransport(handler),
        )
    )

    assert asset is None


def test_youtube_video_does_not_match_on_year_or_score_only() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": {"videoId": "video-123"},
                        "snippet": {
                            "title": (
                                "Highlights | Ecuador 2-1 Germany | "
                                "FIFA World Cup 2026"
                            ),
                            "channelTitle": "FIFA",
                        },
                    }
                ]
            },
        )

    asset = asyncio.run(
        search_official_youtube_video(
            "2-1 2026 7 21",
            "test-key",
            ["official-channel"],
            transport=httpx.MockTransport(handler),
        )
    )

    assert asset is None


def test_collect_report_media_places_timeline_video(monkeypatch) -> None:
    async def fake_video(query, *_args, **_kwargs):
        if "Ramos" not in query:
            return None
        return (
            await search_official_youtube_video(
                "Portugal Ramos",
                "test-key",
                ["official-channel"],
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200,
                        json={
                            "items": [
                                {
                                    "id": {"videoId": "video-123"},
                                    "snippet": {
                                        "title": "Portugal Ramos goal highlights",
                                        "channelTitle": "FIFA",
                                    },
                                }
                            ]
                        },
                    )
                ),
            )
        )

    monkeypatch.setattr(
        "services.report_api.media.search_official_youtube_video", fake_video
    )
    report = GeneratedReport.model_validate(
        {
            "title": "葡萄牙击败克罗地亚",
            "executive_summary": "葡萄牙凭借 Ramos 进球晋级。",
            "sections": [
                {
                    "heading": "葡萄牙晋级",
                    "body": "Ramos 打入制胜球。",
                    "evidence_ids": ["ev-1"],
                }
            ],
            "prediction": None,
            "enrichment": {
                "player_spotlights": [],
                "match_timeline": [
                    {
                        "minute": "89",
                        "event_type": "goal",
                        "player": "Goncalo Ramos",
                        "team": "Portugal",
                        "description": "第89分钟，Goncalo Ramos 完成进球。",
                        "evidence_ids": ["ev-1"],
                    }
                ],
                "media_assets": [],
            },
        }
    )

    assets = asyncio.run(
        collect_report_media(
            report,
            youtube_api_key="test-key",
            youtube_channel_ids=["official-channel"],
        )
    )

    assert assets
    assert assets[0].placement == "timeline"
    assert assets[0].evidence_ids == ["ev-1"]


def test_collect_report_media_adds_section_image_without_spotlight(monkeypatch) -> None:
    seen_targets: list[str] = []

    async def fake_image(target, *_args, **_kwargs):
        seen_targets.append(target)
        if target != "Alex Oxlade-Chamberlain":
            return None
        return MediaAsset(
            asset_type="image",
            title="Alex Oxlade-Chamberlain",
            url=(
                "https://commons.wikimedia.org/wiki/"
                "File:Alex_Oxlade-Chamberlain.jpg"
            ),
            provider="Wikimedia Commons",
            license="CC BY-SA 4.0",
            attribution="Photographer",
            rights_status="review_required",
            relevance_status="metadata_match",
        )

    monkeypatch.setattr(
        "services.report_api.media.search_commons_player_image", fake_image
    )
    report = GeneratedReport.model_validate(
        {
            "title": "今日球脉",
            "executive_summary": "凯尔特人续约成为转会市场焦点。",
            "sections": [
                {
                    "heading": "Oxlade-Chamberlain续约凯尔特人一年",
                    "body": (
                        "Celtic confirmed Alex Oxlade-Chamberlain signed a new "
                        "one-year deal."
                    ),
                    "evidence_ids": ["ev-transfer"],
                    "category": "transfer",
                }
            ],
            "prediction": None,
        }
    )

    assets = asyncio.run(collect_report_media(report))

    assert "Alex Oxlade-Chamberlain" in seen_targets
    assert assets
    assert assets[0].asset_type == "image"
    assert assets[0].placement == "section"
    assert assets[0].target == "Alex Oxlade-Chamberlain"
    assert assets[0].evidence_ids == ["ev-transfer"]


def test_collect_report_media_translates_chinese_match_query(monkeypatch) -> None:
    seen_queries: list[str] = []

    async def fake_video(query, *_args, **_kwargs):
        seen_queries.append(query)
        if "Portugal" not in query or "Croatia" not in query:
            return None
        return (
            await search_official_youtube_video(
                "Portugal Croatia",
                "test-key",
                ["official-channel"],
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200,
                        json={
                            "items": [
                                {
                                    "id": {"videoId": "video-456"},
                                    "snippet": {
                                        "title": "Portugal Croatia match highlights",
                                        "channelTitle": "FIFA",
                                    },
                                }
                            ]
                        },
                    )
                ),
            )
        )

    monkeypatch.setattr(
        "services.report_api.media.search_official_youtube_video", fake_video
    )
    report = GeneratedReport.model_validate(
        {
            "title": "葡萄牙击败克罗地亚",
            "executive_summary": "葡萄牙晋级。",
            "sections": [
                {
                    "heading": "葡萄牙VAR绝杀克罗地亚",
                    "body": "贡萨洛·拉莫斯打入制胜球，葡萄牙淘汰克罗地亚。",
                    "evidence_ids": ["ev-1"],
                    "category": "match",
                }
            ],
            "prediction": None,
        }
    )

    assets = asyncio.run(
        collect_report_media(
            report,
            youtube_api_key="test-key",
            youtube_channel_ids=["official-channel"],
        )
    )

    assert any("Portugal" in query and "Croatia" in query for query in seen_queries)
    assert assets
    assert assets[0].placement == "report_cover"


def test_official_youtube_search_prefers_full_highlights() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        channel_id = request.url.params.get("channelId")
        if channel_id == "fifa-channel":
            title = "Portugal 🆚 Croatia #FIFAWorldCupOnYT"
            video_id = "short-video"
            channel_title = "FIFA"
        else:
            title = (
                "Portugal vs Croatia | Round of 32 | "
                "FIFA World Cup 2026™ Highlights"
            )
            video_id = "highlight-video"
            channel_title = "Mediacorp Sports"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": {"videoId": video_id},
                        "snippet": {
                            "title": title,
                            "channelTitle": channel_title,
                            "thumbnails": {
                                "high": {
                                    "url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                                }
                            },
                        },
                    }
                ]
            },
        )

    asset = asyncio.run(
        search_official_youtube_video(
            "Portugal Croatia Round of 32 FIFA World Cup 2026 Highlights",
            "test-key",
            ["fifa-channel", "broadcast-channel"],
            transport=httpx.MockTransport(handler),
        )
    )

    assert asset is not None
    assert asset.title.endswith("Highlights")
    assert str(asset.url).endswith("highlight-video")


def test_official_youtube_search_falls_back_to_channel_feed_on_rate_limit() -> None:
    feed_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:yt="http://www.youtube.com/xml/schemas/2015"
          xmlns:media="http://search.yahoo.com/mrss/">
      <title>FIFA</title>
      <entry>
        <yt:videoId>ramos-goal</yt:videoId>
        <title>Goncalo Ramos Goal | Portugal 2-1 Croatia | FIFA World Cup 2026™</title>
        <media:group>
          <media:thumbnail url="https://i.ytimg.com/vi/ramos-goal/hqdefault.jpg" />
        </media:group>
      </entry>
    </feed>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "googleapis" in request.url.host:
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, content=feed_xml.encode("utf-8"))

    asset = asyncio.run(
        search_official_youtube_video(
            "Portugal Croatia FIFA World Cup 2026 Highlights",
            "test-key",
            ["fifa-channel"],
            transport=httpx.MockTransport(handler),
        )
    )

    assert asset is not None
    assert asset.title.startswith("Goncalo Ramos Goal")
    assert str(asset.thumbnail_url).endswith("hqdefault.jpg")
