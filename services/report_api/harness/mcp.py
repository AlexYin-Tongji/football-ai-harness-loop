from __future__ import annotations

import json
from pathlib import Path

from services.report_api.harness.models import MCPServerCapability


def load_mcp_capabilities() -> list[MCPServerCapability]:
    repository_root = Path(__file__).resolve().parents[3]
    config_path = repository_root / "config" / "mcp_servers.example.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return [
        MCPServerCapability(
            id=item["id"],
            status="planned",
            read_only=bool(item["read_only"]),
        )
        for item in payload["servers"]
    ]
