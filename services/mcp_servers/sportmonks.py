from __future__ import annotations

import os
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

from services.mcp_servers.common import get_json

mcp = FastMCP(
    "footpulse-sportmonks",
    instructions=(
        "Read licensed player profiles, statistics and fixture events; "
        "never expose token."
    ),
)


def _token() -> str:
    value = os.getenv("SPORTMONKS_API_TOKEN")
    if not value:
        raise RuntimeError("SPORTMONKS_API_TOKEN is not configured")
    return value


def _auth_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": _token(),
    }


@mcp.tool()
async def search_players(query: str, max_items: int = 5) -> dict:
    """Search player identities before requesting a profile."""
    if not 2 <= len(query) <= 120:
        raise ValueError("query length must be between 2 and 120")
    payload = await get_json(
        f"https://api.sportmonks.com/v3/football/players/search/{quote(query)}",
        headers=_auth_headers(),
    )
    return {
        "source_id": "sportmonks",
        "players": [
            {
                "id": item.get("id"),
                "name": item.get("display_name") or item.get("name"),
                "position_id": item.get("position_id"),
                "nationality_id": item.get("nationality_id"),
                "image_path": item.get("image_path"),
            }
            for item in payload.get("data", [])[: max(1, min(max_items, 10))]
        ],
    }


@mcp.tool()
async def get_player_profile(player_id: int) -> dict:
    """Get a licensed player profile and season statistics."""
    if player_id <= 0:
        raise ValueError("player_id must be positive")
    payload = await get_json(
        f"https://api.sportmonks.com/v3/football/players/{player_id}",
        params={"include": "statistics"},
        headers=_auth_headers(),
    )
    return {"source_id": "sportmonks", "player": payload.get("data")}


@mcp.tool()
async def get_fixture_story(fixture_id: int) -> dict:
    """Get participants, score changes, lineups and timestamped match events."""
    if fixture_id <= 0:
        raise ValueError("fixture_id must be positive")
    payload = await get_json(
        f"https://api.sportmonks.com/v3/football/fixtures/{fixture_id}",
        params={
            "include": "participants;events;timeline;scores;lineups;statistics",
        },
        headers=_auth_headers(),
    )
    return {"source_id": "sportmonks", "fixture": payload.get("data")}


@mcp.resource("source://sportmonks/policy")
def source_policy() -> str:
    return (
        "Subscription and redistribution rights apply. Store normalized facts and "
        "approved media metadata only; never store or return the API token."
    )


if __name__ == "__main__":
    mcp.run()
