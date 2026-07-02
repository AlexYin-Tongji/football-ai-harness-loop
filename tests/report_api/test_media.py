from __future__ import annotations

import asyncio

import httpx

from services.report_api.media import (
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
                            "title": "Official match highlights",
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
            "match highlights",
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
