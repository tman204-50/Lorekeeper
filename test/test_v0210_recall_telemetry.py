#!/usr/bin/env python3
"""Regression test for 0.2.10 recall telemetry: client.search() source passthrough.

The /search route now writes a recall event tagged by an optional `source`
field. The client must pass the caller's source through (prefetch uses
system-transform; omitting it defaults to manual-search server-side). Asserts:
  - search(..., source="system-transform") sends "source" in the /search payload
  - search(...) with no source omits it (server defaults to manual-search)
  - the search result shape is unchanged (client returns {results, count})

Run:  python3 test/test_v0210_recall_telemetry.py
No store, no ollama, no LLM spend — a scripted HTTP server captures the body.
"""
import json
import threading
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERMES_CORE = "/usr/local/lib/hermes-agent"
LOREKEEPER = "/root/.hermes/workspace/Lorekeeper"

sys.path.insert(0, HERMES_CORE)
import hermes_bootstrap  # wires the uv venv into sys.path
sys.path.insert(0, LOREKEEPER)

from provider._client import LorekeeperClient

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


class _CaptureServer:
    """Records each POST body; answers /search with a canned result."""

    def __init__(self):
        self.bodies = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                with outer.lock:
                    outer.bodies.append((self.path, json.loads(raw) if raw else {}))
                body = b'{"results": [{"id": "x", "text": "hi"}], "count": 1}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


srv = _CaptureServer()
srv.start()
try:
    client = LorekeeperClient(f"http://127.0.0.1:{srv.port}", "tok", timeout=5.0)

    # with source
    r1 = client.search("hello world", limit=5, source="system-transform")
    check("source-passthrough", r1.get("count") == 1 and len(r1.get("results", [])) == 1, repr(r1))

    # without source
    r2 = client.search("second query", limit=3)

    # inspect captured bodies
    with srv.lock:
        b1 = dict(srv.bodies[0][1])
        b2 = dict(srv.bodies[1][1]) if len(srv.bodies) > 1 else {}

    check("body-1-has-source", b1.get("source") == "system-transform", repr(b1))
    check("body-2-omits-source", "source" not in b2, repr(b2))
    check("body-keeps-query-limit-scope", b1.get("query") == "hello world" and b1.get("limit") == 5, repr(b1))
    check("response-shape-unchanged", set(r1.keys()) == {"results", "count"}, repr(r1))

    client.close()
finally:
    srv.stop()

failed = sum(1 for ok in results if not ok)
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)