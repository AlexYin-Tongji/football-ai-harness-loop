from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from services.mcp_servers.common import load_publisher_registry, load_source_registry

mcp = FastMCP(
    "footpulse-source-registry", instructions="Authoritative source allowlist."
)


@mcp.tool()
def list_approved_sources(category: str | None = None) -> dict:
    """List usable sources while retaining blocked/candidate labels."""
    registry = load_source_registry()
    sources = registry["sources"]
    if category:
        sources = [item for item in sources if item["category"] == category]
    return {"version": registry["version"], "sources": sources}


@mcp.tool()
def list_publishers(topic: str | None = None) -> dict:
    """List publisher domains approved for discovery and citation."""
    registry = load_publisher_registry()
    publishers = registry["publishers"]
    if topic:
        publishers = [item for item in publishers if topic in item["topics"]]
    return {
        "version": registry["version"],
        "policy": registry["policy"],
        "publishers": publishers,
    }


@mcp.resource("registry://sources")
def source_registry() -> str:
    return json.dumps(load_source_registry(), ensure_ascii=False, indent=2)


@mcp.resource("registry://publishers")
def publisher_registry() -> str:
    return json.dumps(load_publisher_registry(), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
