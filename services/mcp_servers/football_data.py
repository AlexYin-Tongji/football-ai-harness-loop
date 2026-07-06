from __future__ import annotations

import os
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from services.mcp_servers.common import get_json

mcp = FastMCP(
    "footpulse-football-data",
    instructions="Read-only normalized match facts from football-data.org.",
)


def _key() -> str:
    value = os.getenv("FOOTBALL_DATA_API_KEY")
    if not value:
        raise RuntimeError("FOOTBALL_DATA_API_KEY is not configured")
    return value


@mcp.tool()
async def list_competition_matches(
    competition: str = "WC", date_from: str | None = None, date_to: str | None = None
) -> dict[str, Any]:
    """List World Cup fixtures and results as normalized facts."""
    params: dict[str, str] = {}
    if date_from:
        date.fromisoformat(date_from)
        params["dateFrom"] = date_from
    if date_to:
        date.fromisoformat(date_to)
        params["dateTo"] = date_to
    payload = await get_json(
        f"https://api.football-data.org/v4/competitions/{competition}/matches",
        params=params,
        headers={"X-Auth-Token": _key()},
    )
    matches = payload.get("matches", [])[:100]
    return {
        "source_id": "football-data-org",
        "retrieved_count": len(matches),
        "matches": [
            {
                "id": item.get("id"),
                "utc_date": item.get("utcDate"),
                "status": item.get("status"),
                "stage": item.get("stage"),
                "home": item.get("homeTeam", {}).get("name"),
                "away": item.get("awayTeam", {}).get("name"),
                "score": item.get("score"),
            }
            for item in matches
        ],
    }


@mcp.resource("source://football-data-org/policy")
def source_policy() -> str:
    """Explain rights and storage boundaries for this connector."""
    return (
        "Approved for fixtures/results/standings. Store normalized facts, "
        "never credentials."
    )


if __name__ == "__main__":
    mcp.run()
