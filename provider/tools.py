"""Lorekeeper plugin tools — the full lorekeeper_* surface served by the Node service.

Registered as a plugin toolset (like a2a/spotify) so the tools flow through the
normal model_tools registry on every platform (api_server included). The memory
provider's own tool injection skips names already present in the tool table, so
installing this alongside the provider is duplicate-safe on every build.

The tool schemas are fetched live from the service (/tools endpoint); if the
service is unreachable at registration time the toolset registers nothing and
check_fn keeps it gated until the next agent build.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from . import _shared
from ._client import LorekeeperClient, LorekeeperError

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "http://127.0.0.1:18777"  # informational; resolution lives in _shared


def _client() -> LorekeeperClient | None:
    """Sig-cached shared client (see provider/_shared.py)."""
    return _shared.get_client()


def _available() -> bool:
    """check_fn: serve the tools ONLY when lorekeeper.json + token resolve. Fail closed."""
    return _client() is not None


def _make_handler(client: LorekeeperClient, name: str):
    def handler(args: dict, **_: Any) -> str:
        try:
            result = client.tool(name, args or {})
        except LorekeeperError as e:
            return f"Lorekeeper tool failed: {e}"
        except Exception as e:  # never break the agent loop on transport hiccups
            return f"Lorekeeper tool failed: {e}"
        payload = result.get("result")
        return payload if isinstance(payload, str) else json.dumps(payload)

    return handler


def register_tools(ctx) -> None:
    """Register the service's full tool surface in the ``lorekeeper`` toolset."""
    client = _client()
    if client is None:
        logger.warning("Lorekeeper tools not registered: lorekeeper.json/token missing or unreadable")
        return
    # Shared schema cache: if the provider already fetched /tools this build,
    # this reuses it instead of a second round-trip.
    schemas = _shared.get_tool_schemas(client)
    if not schemas:
        logger.warning("Lorekeeper tool schemas unavailable (service down?): see lorekeeper.client log")
    for schema in schemas:
        name = schema.get("name")
        if not name:
            continue
        description = schema.get("description", "")
        parameters = schema.get("parameters") or {"type": "object", "properties": {}}
        ctx.register_tool(
            name=name,
            toolset="lorekeeper",
            handler=_make_handler(client, name),
            description=description,
            schema={"name": name, "description": description, "parameters": parameters},
            emoji="\U0001f9d0",  # magnifying glass
            check_fn=_available,
        )
    logger.info("Lorekeeper toolset registered %d tools", len(schemas))
