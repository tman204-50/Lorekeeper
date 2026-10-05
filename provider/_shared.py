"""Shared client resolution + tool-schema cache.

Used by both the memory provider (provider/__init__.py) and the plugin
toolset (provider/tools.py), which used to each build their own client and
each fetch /tools per agent build. One sig-cached client + one schema cache
collapses that to a single fetch.

Client caching: keyed on (home path, lorekeeper.json mtime+size, token file
mtime+size) — any edit invalidates and rebuilds; missing files cache a None
(fail-closed). Resolution happens under the lock (single-flight).

Schema caching: keyed on client identity. A successful fetch is cached for
the client's lifetime (the tool registry is static per service boot); a
FAILED fetch is cached only for _NEGATIVE_TTL seconds so a down service
isn't hammered but a transient blip recovers on the next build.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._client import LorekeeperClient

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "http://127.0.0.1:18777"
_NEGATIVE_TTL = 30.0

# -- client singleton (sig-cached) ---------------------------------------------
_client_lock = threading.Lock()
_client_cache: Optional[LorekeeperClient] = None
_client_sig: Optional[tuple] = None


def _file_sig(path: Path) -> Optional[tuple]:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _resolve_uncached() -> Optional[LorekeeperClient]:
    home = Path(os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    cfg: dict = {}
    try:
        cfg = json.loads((home / "lorekeeper.json").read_text()) or {}
    except Exception:
        cfg = {}
    host = (cfg.get("host") or os.environ.get("LOREKEEPER_HOST") or _DEFAULT_HOST).rstrip("/")
    token = cfg.get("token") or os.environ.get("LOREKEEPER_TOKEN") or ""
    if not token:
        try:
            token = (home / "lorekeeper" / "token").read_text().strip()
        except Exception:
            token = ""
    if not (host and token):
        return None
    return LorekeeperClient(host, token)


def get_client() -> Optional[LorekeeperClient]:
    """Build/return the shared LorekeeperClient from lorekeeper.json + token.

    Precedence matches the provider: lorekeeper.json token > LOREKEEPER_TOKEN
    env > service-written token file. Resolution is two small file reads, so
    the lock is held across it (cold cache rebuilds single-flight, not once
    per thread).
    """
    global _client_cache, _client_sig
    home = Path(os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    sig = (str(home), os.environ.get("LOREKEEPER_TOKEN", ""), _file_sig(home / "lorekeeper.json"), _file_sig(home / "lorekeeper" / "token"))
    with _client_lock:
        if sig == _client_sig:
            return _client_cache
        client = _resolve_uncached()
        _client_sig, _client_cache = sig, client
        return client


# -- tool schema cache -----------------------------------------------------------
_schema_lock = threading.Lock()
# id(client) -> (client ref, schemas-or-None, cached_at monotonic)
_schema_cache: Dict[int, tuple] = {}


def get_tool_schemas(client: LorekeeperClient) -> List[Dict[str, Any]]:
    """Fetch the service's tool surface, cached per client identity."""
    now = time.monotonic()
    with _schema_lock:
        entry = _schema_cache.get(id(client))
        if entry is not None and entry[0] is client:
            schemas, cached_at = entry[1], entry[2]
            if schemas is not None or now - cached_at < _NEGATIVE_TTL:
                return schemas if schemas is not None else []
    try:
        schemas = client.tools().get("tools", [])
    except Exception as e:
        logger.debug("Lorekeeper tool schema fetch failed: %s", e)
        schemas = None
    with _schema_lock:
        _schema_cache[id(client)] = (client, schemas, time.monotonic())
    return schemas if schemas is not None else []
