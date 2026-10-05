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
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("lorekeeper.client")

_syslog_ready = False


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
        self._conn: Optional[http.client.HTTPConnection] = None
        self._lock = threading.Lock()

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
        with self._lock:
            for attempt in (0, 1):
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
                        # A real server answer: no retry, surface it.
                        detail = raw.decode("utf-8", "replace")[:200]
                        logger.error("%s %s -> HTTP %s: %s", method, path, resp.status, detail)
                        raise LorekeeperError(f"Lorekeeper service HTTP {resp.status}: {detail}")
                    return json.loads(raw) if raw else {}
                except LorekeeperError:
                    raise
                except (http.client.HTTPException, OSError) as e:
                    self._close_conn()
                    transport_err = e
                    if attempt:
                        logger.error("%s %s unreachable after reconnect: %s", method, path, e)
                        break
                    logger.info("%s %s stale connection (%s), reconnecting", method, path, type(e).__name__)
        raise LorekeeperError(f"Lorekeeper service unreachable at {self._base}: {transport_err}") from transport_err

    # -- endpoints -----------------------------------------------------------

    def health(self) -> dict:
        return self._request("GET", "/health")

    def init(self) -> dict:
        return self._request("POST", "/init", {})

    def search(self, query: str, limit: int = 5, scope: Optional[str] = None) -> dict:
        return self._request("POST", "/search", {"query": query, "limit": limit, "scope": scope})

    def remember(self, content: str, category: Optional[str] = None, importance: Optional[float] = None, scope: Optional[str] = None) -> dict:
        payload: Dict[str, Any] = {"content": content}
        if category is not None:
            payload["category"] = category
        if importance is not None:
            payload["importance"] = importance
        if scope is not None:
            payload["scope"] = scope
        return self._request("POST", "/remember", payload)

    def delete(self, memory_id: str, force: bool = False) -> dict:
        return self._request("POST", "/delete", {"id": memory_id, "force": force})

    def stats(self) -> dict:
        return self._request("POST", "/stats", {})

    def capture(self, payload: dict) -> dict:
        """Run heuristics/LLM auto-capture on buffered turn text.

        Uses the extended capture timeout (server LLM extraction is capped at
        90s; a shorter client timeout would abort first and cause a duplicate
        re-send on the next flush)."""
        return self._request("POST", "/capture", payload, response_timeout=self._capture_timeout)

    def tools(self) -> dict:
        """List all registered tool schemas from the service."""
        return self._request("POST", "/tools", {})

    def tool(self, name: str, tool_args: dict) -> dict:
        """Dispatch a single fork tool call to the service."""
        return self._request("POST", "/tool", {"name": name, "toolArgs": tool_args})

    def close(self) -> None:
        with self._lock:
            self._close_conn()
        logger.debug("connection closed")
