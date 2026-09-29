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

from ._client import LorekeeperClient, LorekeeperError

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "http://127.0.0.1:18777"


def _client() -> LorekeeperClient | None:
    """Build the client from lorekeeper.json + token without session kwargs."""
    home = Path(os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    cfg: dict = {}
    try:
        cfg = json.loads((home / "lorekeeper.json").read_text()) or {}
    except Exception:
        cfg = {}
    host = (cfg.get("host") or os.environ.get("LOREKEEPER_HOST") or _DEFAULT_HOST).rstrip("/")
    token = cfg.get("token") or ""
    if not token:
        try:
            token = (home / "lorekeeper" / "token").read_text().strip()
        except Exception:
            token = ""
    if not (host and token):
        return None
    return LorekeeperClient(host, token)


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
    try:
        schemas = client.tools().get("tools", [])
    except Exception as e:
        logger.warning("Lorekeeper tool schemas unavailable (service down?): %s", e)
        return
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
