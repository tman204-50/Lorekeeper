"""HTTP client for the Lorekeeper memory service (stdlib only).

Holds a single keep-alive connection (urllib opened a fresh TCP connection
per call). The connection is NOT thread-safe — the provider calls it from
concurrent threads (prefetch, capture flush, tool dispatch) — so requests
are serialized with a lock. A stale server-closed keep-alive is recovered by
one reconnect+retry; non-2xx answers are raised without retry.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import Future
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from ._version import __version__

logger = logging.getLogger("lorekeeper.client")

_syslog_ready = False

# Retry policy (see _request): stale keep-alive and generic transport errors
# get one immediate reconnect; connection-refused (service restarting) gets
# backoff retries because a refused connect means the request never reached
# the app (safe to replay even for remember); transient gateway statuses get
# one backoff retry; everything else (500, 4xx) is a deterministic server
# answer and fails immediately.
_RECONNECT_RETRIES = 1
_REFUSED_RETRIES = 2
_CONNECT_RETRY_DELAYS = (0.2, 0.4)
_SERVER_ERROR_RETRY_DELAY = 0.3
_TRANSIENT_STATUSES = {502, 503, 504}

# Per-tool response timeouts for /tool dispatch. The 10s default is right for
# search/CRUD, but some fork tools are LLM-backed or rescore whole scopes:
# summarize runs LLM digest generation, consolidate/reembed/import re-embed
# and rescore everything. Values must stay UNDER the gateway's tool-call
# budget (HERMES_CONCURRENT_TOOL_TIMEOUT_S, default 420s) or the gateway
# kills the call first.
_TOOL_TIMEOUTS = {
    "lorekeeper_summarize": 300.0,
    "lorekeeper_consolidate": 300.0,
    "lorekeeper_consolidate_all": 300.0,
    "lorekeeper_reembed": 300.0,
    "lorekeeper_import": 300.0,
    "lorekeeper_expire": 120.0,
    "lorekeeper_event_cleanup": 120.0,
    "lorekeeper_export": 120.0,
}


def _ensure_syslog_logger() -> None:
    """Route lorekeeper.client records to syslog the way hermes does.

    Hermes itself doesn't call SysLogHandler — the gateway's stderr StreamHandler
    is picked up by journald and forwarded to /var/log/messages (tagged
    ``hermes[pid]``). So: under the gateway this logger simply inherits
    hermes' root handlers; standalone (scripts, tests, other units) nothing
    upstream is configured, so attach a stderr handler — whatever unit runs
    the process journald-forwards it under that unit's tag. With
    LOREKEEPER_CLIENT_DEBUG=1 attach a dedicated DEBUG handler so per-request
    lines survive a gateway running at INFO.
    """
    global _syslog_ready
    if _syslog_ready:
        return
    _syslog_ready = True
    logger.info("lorekeeper.client v%s (first request)", __version__)
    if os.environ.get("LOREKEEPER_CLIENT_DEBUG"):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("lorekeeper-client: %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        return
    node = logger
    while node is not None and not node.handlers:
        node = node.parent
    if node is not None and node.handlers:
        return  # inherit upstream (hermes gateway) as-is
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("lorekeeper-client: %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class LorekeeperError(RuntimeError):
    """Raised on transport failure or a non-2xx service response."""


def error_from_result(result: Any) -> Optional[str]:
    """Detect a tool that failed inside a 200 response.

    Most fork tools signal failure by throwing (the service answers 500), but
    a few return a JSON body with an ``error`` key from a 2xx (e.g.
    memory_import's read/validation failures). Surface those as errors so the
    agent sees a failure instead of a clean-looking result.
    """
    if not isinstance(result, dict):
        return None
    payload = result.get("result")
    if isinstance(payload, str):
        stripped = payload.strip()
        if not stripped.startswith("{"):
            return None
        try:
            obj = json.loads(stripped)
        except Exception:
            return None
    elif isinstance(payload, dict):
        obj = payload
    else:
        return None
    if isinstance(obj, dict) and obj.get("error"):
        return str(obj["error"])
    return None


class LorekeeperClient:
    """Bearer-authenticated JSON client for the loopback Lorekeeper service."""

    def __init__(self, host: str, token: str, timeout: float = 10.0, capture_timeout: Optional[float] = None):
        self._base = host.rstrip("/")
        parts = urlsplit(self._base)
        self._node = parts.hostname or "127.0.0.1"
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self._https = parts.scheme == "https"
        self._token = token
        self._timeout = timeout
        # /capture runs server-side LLM extraction with a 90s hard cap; a
        # 10s client timeout aborts first, the provider retains the buffer,
        # and the next flush re-sends it (duplicate LLM extraction). The
        # capture response timeout must sit ABOVE the server's cap.
        try:
            self._capture_timeout = float(
                capture_timeout or os.environ.get("LOREKEEPER_CAPTURE_TIMEOUT") or 120.0
            )
        except (TypeError, ValueError):
            self._capture_timeout = 120.0
        # Short-TTL search cache: the model often re-searches what the
        # prefetch just searched (derived from the same turn text) — an exact
        # repeat costs a full server search (embedder + LanceDB + scoring).
        # LOREKEEPER_SEARCH_CACHE_TTL=0 disables. Loosely bounded LRU.
        try:
            self._search_cache_ttl = float(os.environ.get("LOREKEEPER_SEARCH_CACHE_TTL") or 60.0)
        except (TypeError, ValueError):
            self._search_cache_ttl = 60.0
        self._search_cache_max = 16
        self._search_cache: Dict[tuple, tuple] = {}  # key -> (result, monotonic)
        self._search_inflight: Dict[tuple, Future] = {}  # key -> future (coalesce)
        self._search_cache_lock = threading.Lock()
        self._conn: Optional[http.client.HTTPConnection] = None
        self._lock = threading.Lock()

    @staticmethod
    def _normalize_query(query: str) -> str:
        return " ".join((query or "").split()).lower()

    def _search_cache_get(self, key: tuple):
        if self._search_cache_ttl <= 0:
            return None
        with self._search_cache_lock:
            entry = self._search_cache.get(key)
            if entry is None:
                return None
            result, cached_at = entry
            if time.monotonic() - cached_at > self._search_cache_ttl:
                del self._search_cache[key]
                return None
            return result

    def _search_fetch(self, key: tuple, fetch):
        """Fetch with TTL cache + in-flight coalescing (thread-safe).

        Concurrent identical fetches (the prefetch worker and the model's
        lorekeeper_search tool call) share one request: losers block on the
        owner's future instead of issuing a second server search."""
        hit = self._search_cache_get(key)
        if hit is not None:
            logger.debug("search cache hit (%s)", str(key[0]))
            return hit
        with self._search_cache_lock:
            fut = self._search_inflight.get(key)
            owner = fut is None
            if owner:
                fut = Future()
                self._search_inflight[key] = fut
        if not owner:
            return fut.result()
        try:
            result = fetch()
        except Exception as e:
            with self._search_cache_lock:
                self._search_inflight.pop(key, None)
            fut.set_exception(e)
            raise
        with self._search_cache_lock:
            self._search_inflight.pop(key, None)
            if self._search_cache_ttl > 0:
                if len(self._search_cache) >= self._search_cache_max:
                    # dict keeps insertion order: drop the oldest entry
                    self._search_cache.pop(next(iter(self._search_cache)), None)
                self._search_cache[key] = (result, time.monotonic())
        fut.set_result(result)
        return result

    def _evict_search_cache(self) -> None:
        with self._search_cache_lock:
            self._search_cache.clear()

    def _new_conn(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        return cls(self._node, self._port, timeout=self._timeout)

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _request(self, method: str, path: str, payload: Optional[dict] = None, *, response_timeout: Optional[float] = None) -> dict:
        _ensure_syslog_logger()
        body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        transport_err: Optional[Exception] = None
        attempt = 0  # reconnect budget (stale keep-alive / generic transport errors)
        refused_retries = 0  # connection-refused budget (service restart race)
        transient_retried = False  # 502/503/504: one backoff retry
        with self._lock:
            while True:
                reused = self._conn is not None
                started = time.monotonic()
                try:
                    if self._conn is None:
                        self._conn = self._new_conn()
                    self._conn.request(method, path, body=body, headers=headers)
                    # Per-request response timeout: capture must outwait the
                    # server's LLM extraction cap without slowing anything
                    # else. The connection is lock-serialized, so restore the
                    # default immediately after this response.
                    if response_timeout is not None and self._conn.sock is not None:
                        self._conn.sock.settimeout(response_timeout)
                    resp = self._conn.getresponse()
                    raw = resp.read()
                    if self._conn.sock is not None and response_timeout is not None:
                        self._conn.sock.settimeout(self._timeout)
                    logger.debug(
                        "%s %s -> %s (%.0f ms, conn=%s)",
                        method, path, resp.status, (time.monotonic() - started) * 1000,
                        "reused" if reused else "new",
                    )
                    if resp.status // 100 != 2:
                        if resp.status in _TRANSIENT_STATUSES and not transient_retried:
                            # Gateway/transient: the server explicitly says it
                            # didn't process the request — safe to replay.
                            transient_retried = True
                            self._close_conn()
                            logger.info("%s %s -> HTTP %s (transient), retrying in %.1fs",
                                        method, path, resp.status, _SERVER_ERROR_RETRY_DELAY)
                            time.sleep(_SERVER_ERROR_RETRY_DELAY)
                            continue
                        # A real server answer: no retry, surface it.
                        detail = raw.decode("utf-8", "replace")[:200]
                        logger.error("%s %s -> HTTP %s: %s", method, path, resp.status, detail)
                        raise LorekeeperError(f"Lorekeeper service HTTP {resp.status}: {detail}")
                    return json.loads(raw) if raw else {}
                except LorekeeperError:
                    raise
                except (http.client.HTTPException, OSError) as e:
                    # Dropped socket (stale keep-alive) or failed connect:
                    # drop the connection and retry per the policy above.
                    self._close_conn()
                    transport_err = e
                    if reused:
                        if attempt >= _RECONNECT_RETRIES:
                            logger.error("%s %s unreachable after reconnect: %s", method, path, e)
                            break
                        attempt += 1
                        logger.info("%s %s stale connection (%s), reconnecting", method, path, type(e).__name__)
                        continue
                    if isinstance(e, ConnectionRefusedError) and refused_retries < _REFUSED_RETRIES:
                        delay = _CONNECT_RETRY_DELAYS[refused_retries]
                        refused_retries += 1
                        logger.info("%s %s refused (%s), retrying in %.1fs", method, path, type(e).__name__, delay)
                        time.sleep(delay)
                        continue
                    if attempt < _RECONNECT_RETRIES:
                        attempt += 1
                        logger.info("%s %s connection error (%s), reconnecting", method, path, type(e).__name__)
                        continue
                    logger.error("%s %s unreachable after retries: %s", method, path, e)
                    break
        raise LorekeeperError(f"Lorekeeper service unreachable at {self._base}: {transport_err}") from transport_err

    # -- endpoints -----------------------------------------------------------

    def health(self) -> dict:
        return self._request("GET", "/health")

    def init(self) -> dict:
        return self._request("POST", "/init", {})

    def search(self, query: str, limit: int = 5, scope: Optional[str] = None) -> dict:
        key = ("search", self._normalize_query(query), limit, scope)
        return self._search_fetch(key, lambda: self._request("POST", "/search", {"query": query, "limit": limit, "scope": scope}))

    def remember(self, content: str, category: Optional[str] = None, importance: Optional[float] = None, scope: Optional[str] = None) -> dict:
        payload: Dict[str, Any] = {"content": content}
        if category is not None:
            payload["category"] = category
        if importance is not None:
            payload["importance"] = importance
        if scope is not None:
            payload["scope"] = scope
        result = self._request("POST", "/remember", payload)
        self._evict_search_cache()
        return result

    def delete(self, memory_id: str, force: bool = False) -> dict:
        result = self._request("POST", "/delete", {"id": memory_id, "force": force})
        self._evict_search_cache()
        return result

    def stats(self) -> dict:
        return self._request("POST", "/stats", {})

    def metrics(self, reset: bool = False) -> dict:
        """Aggregated timing spans + scope-cache stats (server-side, ops-facing)."""
        return self._request("POST", "/metrics", {"reset": True} if reset else {})

    def capture(self, payload: dict) -> dict:
        """Run heuristics/LLM auto-capture on buffered turn text.

        Uses the extended capture timeout (server LLM extraction is capped at
        90s; a shorter client timeout would abort first and cause a duplicate
        re-send on the next flush). Success evicts the search cache: freshly
        stored memories must be searchable immediately."""
        result = self._request("POST", "/capture", payload, response_timeout=self._capture_timeout)
        self._evict_search_cache()
        return result

    def tools(self) -> dict:
        """List all registered tool schemas from the service."""
        return self._request("POST", "/tools", {})

    def tool(self, name: str, tool_args: dict) -> dict:
        """Dispatch a single fork tool call to the service.

        lorekeeper_search participates in the TTL cache (same query/limit ->
        one server search). Any OTHER lorekeeper_* tool may mutate what a
        search returns (remember/delete/feedback/citation/scope...) or its
        result view, so it evicts the cache."""
        if name == "lorekeeper_search":
            key = (
                "tool",
                self._normalize_query(tool_args.get("query") or ""),
                tool_args.get("limit") or 5,
                tool_args.get("scope"),
            )
            return self._search_fetch(key, lambda: self._request("POST", "/tool", {"name": name, "toolArgs": tool_args}))
        result = self._request("POST", "/tool", {"name": name, "toolArgs": tool_args}, response_timeout=_TOOL_TIMEOUTS.get(name))
        self._evict_search_cache()
        return result

    def close(self) -> None:
        with self._lock:
            self._close_conn()
        logger.debug("connection closed")
