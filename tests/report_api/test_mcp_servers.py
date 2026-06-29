from __future__ import annotations

from services.mcp_servers.source_registry import list_approved_sources, list_publishers


def test_source_registry_exposes_approved_and_blocked_boundaries() -> None:
    payload = list_approved_sources()
    by_id = {item["id"]: item for item in payload["sources"]}

    assert by_id["football-data-org"]["production_status"] == "approved_with_key"
    assert by_id["gdelt-doc"]["production_status"] == "approved_discovery_only"
    assert by_id["transfermarkt"]["production_status"].startswith("blocked")
    assert by_id["transfermarkt"]["allowed_content"] == []


def test_publisher_registry_has_official_and_cross_check_sources() -> None:
    payload = list_publishers("transfer")
    by_id = {item["id"]: item for item in payload["publishers"]}

    assert by_id["premier-league"]["tier"] == "S0"
    assert by_id["reuters"]["access"] == "gdelt_discovery_then_citation"
    assert payload["policy"] == "discovery_and_citation_only_no_full_text_storage"
