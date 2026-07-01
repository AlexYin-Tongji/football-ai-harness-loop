from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from services.report_api.media import (
    search_commons_player_image,
    search_official_youtube_video,
)

mcp = FastMCP(
    "footpulse-media-assets",
    instructions="Discover license-filtered images and allowlisted official videos.",
)


@mcp.tool()
async def find_player_image(name: str) -> dict:
    """Find one Commons image with accepted license and attribution metadata."""
    asset = await search_commons_player_image(name)
    return {
        "source_id": "wikimedia-commons-api",
        "asset": asset.model_dump(mode="json") if asset else None,
    }


@mcp.tool()
async def find_official_video(query: str) -> dict:
    """Find an embeddable video only within manually approved official channels."""
    api_key = os.getenv("YOUTUBE_API_KEY")
    channel_ids = [
        item.strip()
        for item in os.getenv("YOUTUBE_OFFICIAL_CHANNEL_IDS", "").split(",")
        if item.strip()
    ]
    if not api_key or not channel_ids:
        raise RuntimeError("YouTube key and official channel allowlist are required")
    asset = await search_official_youtube_video(query, api_key, channel_ids)
    return {
        "source_id": "youtube-data-api",
        "asset": asset.model_dump(mode="json") if asset else None,
    }


if __name__ == "__main__":
    mcp.run()
