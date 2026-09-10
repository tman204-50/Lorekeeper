"""Minimal HTTP client for the Lorekeeper memory service (stdlib only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class LorekeeperError(RuntimeError):
    """Raised on transport failure or a non-2xx service response."""


class LorekeeperClient:
    """Bearer-authenticated JSON client for the loopback Lorekeeper service."""

    def __init__(self, host: str, token: str, timeout: float = 10.0):
        self._base = host.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = f"{self._base}{path}"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                pass
            raise LorekeeperError(f"Lorekeeper service HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LorekeeperError(f"Lorekeeper service unreachable at {self._base}: {e.reason}") from e

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

    def close(self) -> None:
        pass  # urllib has no persistent connection to close