from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]


def load_source_registry() -> dict[str, Any]:
    return json.loads((ROOT / "config" / "source_registry.json").read_text("utf-8"))


def load_publisher_registry() -> dict[str, Any]:
    return json.loads((ROOT / "config" / "publisher_registry.json").read_text("utf-8"))


async def get_json(
    url: str,
    *,
    params: dict[str, str | int] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        if int(response.headers.get("content-length", "0")) > 2_000_000:
            raise ValueError("upstream response exceeds 2 MB")
        return response.json()
