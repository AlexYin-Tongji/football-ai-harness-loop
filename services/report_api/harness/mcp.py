from __future__ import annotations

import json
from pathlib import Path

from services.report_api.harness.models import MCPServerCapability


def load_mcp_capabilities() -> list[MCPServerCapability]:
    repository_root = Path(__file__).resolve().parents[3]
    config_path = repository_root / "config" / "mcp_servers.example.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    servers = payload["servers"]
    if isinstance(servers, dict):
        return [
            MCPServerCapability(
                id=server_id,
                status="implemented",
                read_only=bool(item["read_only"]),
            )
            for server_id, item in servers.items()
        ]
    return [
        MCPServerCapability(
            id=item["id"], status="planned", read_only=bool(item["read_only"])
        )
        for item in servers
    ]
