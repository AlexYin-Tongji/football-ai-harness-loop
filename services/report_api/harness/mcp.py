from __future__ import annotations

import json
import os
from pathlib import Path

from services.mcp_servers.common import load_source_registry
from services.report_api.harness.models import MCPServerCapability

SERVER_SOURCE_ALIASES = {
    "football-data": "football-data-org",
    "news-discovery": "gdelt-doc",
    "rss-news": "guardian-football-rss",
    "sportmonks": "sportmonks",
    "media-assets": "wikimedia-commons-api",
}


def _source_status_by_id() -> dict[str, str]:
    return {
        item["id"]: str(item["production_status"])
        for item in load_source_registry()["sources"]
    }


def _capability_status(
    item: dict, configured: bool, production_status: str | None
) -> str:
    if not configured:
        return "needs_configuration"
    if production_status and production_status.startswith("blocked"):
        return "blocked"
    if production_status and production_status.startswith("candidate"):
        return "candidate"
    return "available"


def load_mcp_capabilities() -> list[MCPServerCapability]:
    repository_root = Path(__file__).resolve().parents[3]
    config_path = repository_root / "config" / "mcp_servers.example.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    servers = payload["servers"]
    source_status = _source_status_by_id()
    if isinstance(servers, dict):
        capabilities: list[MCPServerCapability] = []
        for server_id, item in servers.items():
            required_env = list(item.get("required_env") or [])
            optional_env = list(item.get("optional_env") or [])
            configured = all(os.getenv(name) for name in required_env)
            production_status = source_status.get(
                SERVER_SOURCE_ALIASES.get(server_id, server_id)
            )
            notes = None
            if optional_env and not any(os.getenv(name) for name in optional_env):
                notes = "部分可选能力未配置，运行时会安全降级。"
            capabilities.append(
                MCPServerCapability(
                    id=server_id,
                    status=_capability_status(item, configured, production_status),
                    read_only=bool(item["read_only"]),
                    configured=configured,
                    required_env=required_env,
                    production_status=production_status,
                    notes=notes,
                )
            )
        return capabilities
    return [
        MCPServerCapability(
            id=item["id"],
            status=_capability_status(
                item,
                all(os.getenv(name) for name in item.get("required_env", [])),
                source_status.get(SERVER_SOURCE_ALIASES.get(item["id"], item["id"])),
            ),
            read_only=bool(item["read_only"]),
            configured=all(os.getenv(name) for name in item.get("required_env", [])),
            required_env=list(item.get("required_env") or []),
            production_status=source_status.get(
                SERVER_SOURCE_ALIASES.get(item["id"], item["id"])
            ),
        )
        for item in servers
    ]
