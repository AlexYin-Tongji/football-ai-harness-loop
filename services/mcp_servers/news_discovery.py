from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from services.mcp_servers.common import get_json

mcp = FastMCP(
    "footpulse-news-discovery",
    instructions=(
        "Discover candidate football articles; metadata only, never full-text storage."
    ),
)


@mcp.tool()
async def search_football_news(
    query: str, max_records: int = 25, timespan: str = "24h"
) -> dict[str, Any]:
    """Search GDELT DOC; results require source-level verification."""
    if not 1 <= max_records <= 50:
        raise ValueError("max_records must be between 1 and 50")
    if len(query) > 300:
        raise ValueError("query is too long")
    payload = await get_json(
        "https://api.gdeltproject.org/api/v2/doc/doc",
        params={
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": max_records,
            "timespan": timespan,
            "sort": "datedesc",
        },
    )
    articles = payload.get("articles", [])[:max_records]
    return {
        "source_id": "gdelt-doc",
        "discovery_only": True,
        "articles": [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "domain": item.get("domain"),
                "published_at": item.get("seendate"),
                "language": item.get("language"),
            }
            for item in articles
        ],
    }


@mcp.resource("source://gdelt-doc/policy")
def source_policy() -> str:
    return (
        "Discovery only. Persist metadata/excerpts; verify claims at publisher "
        "or official source."
    )


if __name__ == "__main__":
    mcp.run()
