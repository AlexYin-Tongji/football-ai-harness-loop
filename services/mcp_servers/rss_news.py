from __future__ import annotations

from datetime import date

from mcp.server.fastmcp import FastMCP

from services.report_api.domain import ConsumerReportRequest, ReportType
from services.report_api.evidence import collect_guardian_evidence

mcp = FastMCP(
    "footpulse-rss-news",
    instructions=(
        "Read approved publisher RSS metadata and excerpts; never store full text."
    ),
)


@mcp.tool()
async def search_guardian_football(
    query: str, report_type: str = "world_cup_daily", max_items: int = 12
) -> dict:
    """Search the approved Guardian football feed for recent evidence."""
    request = ConsumerReportRequest(
        report_type=ReportType(report_type),
        subject=query,
        report_date=date.today(),
        match_stage="knockout" if report_type == "match_prediction" else None,
    )
    items = await collect_guardian_evidence(request, max_items=max_items)
    return {
        "source_id": "guardian-football-rss",
        "items": [item.model_dump(mode="json") for item in items],
    }


@mcp.resource("source://guardian-football-rss/policy")
def source_policy() -> str:
    return (
        "Development/non-commercial discovery and citation only. Store title, URL, "
        "time and short excerpt; do not persist full feed content."
    )


if __name__ == "__main__":
    mcp.run()
