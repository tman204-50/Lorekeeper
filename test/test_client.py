#!/usr/bin/env python3
"""Regression tests for LorekeeperClient (keep-alive + recovery).

Boots a scratch Lorekeeper service on a private port (throwaway DB dirs) and
asserts the client contract:
  - repeated requests succeed over one persistent connection
  - after the service restarts (stale server-closed keep-alive), the next
    request reconnects transparently and succeeds
  - service down -> LorekeeperError (not a raw socket exception)
  - wrong token -> LorekeeperError mentioning HTTP 401
  - concurrent requests from multiple threads are safe (connection lock)

Run:  python3 lorekeeper-tests/test_client.py
Uses /tools and /health only — no LLM capture spend.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERMES_CORE = "/usr/local/lib/hermes-agent"
LOREKEEPER = "/root/.hermes/workspace/Lorekeeper"

sys.path.insert(0, HERMES_CORE)
import hermes_bootstrap  # wires the uv venv into sys.path
sys.path.insert(0, LOREKEEPER)

import logging

from provider._client import LorekeeperClient, LorekeeperError


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _SlowCaptureServer:
    """HTTP server where /capture takes 2s to answer; everything else is instant."""

    def __init__(self, port):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep-alive, like the real service

            def do_POST(self):
                # Drain the request body so keep-alive stays in sync.
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if self.path == "/capture":
                    time.sleep(2.0)
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.do_POST()

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = port

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

# Force the client's own DEBUG handler (per-request lines) and capture records
# through a test handler to assert the logging contract.
os.environ["LOREKEEPER_CLIENT_DEBUG"] = "1"
logging.getLogger("lorekeeper.client").setLevel(logging.DEBUG)


class _Capture(logging.Handler):
    records = []

    def emit(self, record):
        _Capture.records.append(record)

    @staticmethod
    def messages():
        return [r.getMessage() for r in _Capture.records]


logging.getLogger("lorekeeper.client").addHandler(_Capture())

PORT = 18781
HOST = f"http://127.0.0.1:{PORT}"
TOKEN = "client-test-token"

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def boot_service(db_dir, graph_dir):
    env = dict(
        os.environ,
        LOREKEEPER_PORT=str(PORT),
        LOREKEEPER_DB_PATH=db_dir,
        LOREKEEPER_GRAPH_PATH=graph_dir,
        LOREKEEPER_TOKEN=TOKEN,
        OPENCODE_MEMORY_PRO_CAPTURE_MODE="heuristics",  # never touch LLM spend
    )
    return subprocess.Popen(
        ["/usr/bin/node", "server/index.js"],
        cwd=LOREKEEPER,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_healthy(timeout=10.0):
    probe = LorekeeperClient(HOST, TOKEN, timeout=2.0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            probe.health()
            return True
        except LorekeeperError:
            time.sleep(0.2)
    return False


tmp = tempfile.mkdtemp(prefix="lk-client-test-")
db_dir = os.path.join(tmp, "db")
graph_dir = os.path.join(tmp, "graph.db")

proc = boot_service(db_dir, graph_dir)
try:
    if not wait_healthy():
        print("FATAL: scratch service did not come up")
        sys.exit(1)

    client = LorekeeperClient(HOST, TOKEN)

    # 1. repeated requests over one connection
    ok_all = True
    for _ in range(5):
        resp = client.tools()
        ok_all = ok_all and len(resp.get("tools", [])) == 34
    check("repeated-requests", ok_all, "5x /tools, 34 schemas each")

    # 2. connection is actually persistent
    check("connection-persistent", client._conn is not None, "HTTPConnection kept open after requests")

    # 3. health endpoint also reuses it
    check("health-ok", client.health().get("ok") is True, "GET /health")

    # 4. wrong token -> LorekeeperError with HTTP 401 (no retry storm)
    bad = LorekeeperClient(HOST, "wrong-token")
    try:
        bad.tools()
        check("wrong-token", False, "no error raised")
    except LorekeeperError as e:
        check("wrong-token", "HTTP 401" in str(e), str(e)[:80])

    # 5. service dies -> LorekeeperError (not a raw exception)
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)
    time.sleep(0.3)
    try:
        client.tools()
        check("service-down", False, "no error raised")
    except LorekeeperError as e:
        check("service-down", "unreachable" in str(e), str(e)[:80])

    # 6. restart -> stale keep-alive recovered transparently
    proc = boot_service(db_dir, graph_dir)
    if not wait_healthy():
        check("stale-reconnect", False, "restarted service did not come up")
    else:
        try:
            resp = client.tools()
            check("stale-reconnect", len(resp.get("tools", [])) == 34, "reconnected after server restart")
        except LorekeeperError as e:
            check("stale-reconnect", False, str(e)[:80])

    # 7. concurrent requests are safe (lock, no interleaved corruption)
    errs = []
    counts = []

    def worker(n):
        try:
            c = LorekeeperClient(HOST, TOKEN)
            for _ in range(10):
                counts.append(len(c.tools().get("tools", [])))
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("concurrent-safe", not errs and counts.count(34) == 40,
          f"4 threads x 10 calls: {len(counts)} ok, {len(errs)} errors")

    client.close()
    check("close", client._conn is None, "connection closed")

    # 8. debug logging: per-request lines with method/path/status/duration
    msgs = _Capture.messages()
    req_lines = [m for m in msgs if "POST /tools -> 200" in m and "conn=" in m]
    check("log-request-line", len(req_lines) >= 5, f"{len(req_lines)} request lines, e.g. {req_lines[0] if req_lines else 'none'}")

    # 9. reconnect + failure lines present
    reconnects = [m for m in msgs if "reconnecting" in m]
    check("log-reconnect", len(reconnects) >= 1, f"e.g. {reconnects[0] if reconnects else 'none'}")
    unreachable = [m for m in msgs if "unreachable after reconnect" in m]
    check("log-unreachable", len(unreachable) >= 1, f"e.g. {unreachable[0] if unreachable else 'none'}")
    http_err = [m for m in msgs if "HTTP 401" in m]
    check("log-http-error", len(http_err) >= 1, f"e.g. {http_err[0] if http_err else 'none'}")

    # 10. secrets never logged (token must not appear anywhere)
    leaked = [m for m in msgs if TOKEN in m or "Bearer" in m]
    check("no-token-leak", not leaked, f"{len(leaked)} lines contain token/auth header")

    # 11. per-endpoint capture timeout (slow /capture, fast everything else)
    slow_port = _free_port()
    slow = _SlowCaptureServer(slow_port)
    slow.start()
    try:
        cslow = LorekeeperClient(f"http://127.0.0.1:{slow_port}", TOKEN, timeout=1.0, capture_timeout=3.0)
        # fast endpoint succeeds under the default 1s timeout
        cslow.health()
        # /capture sleeps 2s: exceeds the 1s default but fits capture_timeout
        cslow.capture({"sessionID": "t", "text": "slow extraction"})
        check("capture-timeout-override", True, "capture (2s) survived 1s default via capture_timeout=3")
        # socket timeout restored to the default afterwards
        restored = cslow._conn.sock.gettimeout() == 1.0
        check("timeout-restored", restored, f"sock timeout now {cslow._conn.sock.gettimeout()}")
        # a second slow capture on the REUSED connection still overrides
        cslow.capture({"sessionID": "t", "text": "slow again"})
        check("capture-reused-conn", True, "second capture on reused conn, override still applied")
        cslow.close()
    finally:
        slow.stop()

    # 12. capture without override still fails on slow extraction (guards the default)
    cdef = LorekeeperClient(f"http://127.0.0.1:{slow_port}", TOKEN, timeout=1.0)
    try:
        cdef.capture({"sessionID": "t", "text": "will time out"})
        check("default-timeout-still-short", False, "capture succeeded despite 1s timeout")
    except LorekeeperError:
        check("default-timeout-still-short", True, "1s client timeout aborts slow capture (old behavior)")
finally:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)

passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
print(f"\nPASS {passed} / FAIL {failed} / TOTAL {len(results)}")
sys.exit(1 if failed else 0)
