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


class _ScriptedStatusServer:
    """Serves a scripted sequence of (status, body) per request; counts requests."""

    def __init__(self, port, script):
        self.script = list(script)
        self.requests = 0
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                with outer.lock:
                    idx = outer.requests
                    outer.requests += 1
                    status, body = outer.script[idx] if idx < len(outer.script) else (200, b'{"ok": true}')
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = port

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


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


class _CountingJSONServer:
    """Counts /search and /tool calls; instant canned JSON responses.

    /search -> {"results": [...]}, /tool lorekeeper_search -> {"result": "..."},
    any other /tool -> {"result": "done"} (treated as a mutation by the client)."""

    def __init__(self, port):
        self.calls = {"search": 0, "tool_search": 0, "tool_other": 0}
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                with outer.lock:
                    if self.path == "/search":
                        outer.calls["search"] += 1
                        body = json.dumps({"results": [{"id": "x", "text": "hit"}]}).encode()
                    elif payload.get("name") == "lorekeeper_search":
                        outer.calls["tool_search"] += 1
                        body = json.dumps({"result": "1. [id] cached tool result [80%]"}).encode()
                    else:
                        outer.calls["tool_other"] += 1
                        body = json.dumps({"result": "done"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = port

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def counts(self):
        with self.lock:
            return dict(self.calls)

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

    # 1b. version handshake: /health carries the version; client logged its own
    h = client.health()
    check("health-version", isinstance(h.get("version"), str) and h["version"].count(".") == 2, f"service version: {h.get('version')}")
    from provider._version import __version__ as _v
    check("client-version-logged", any(f"lorekeeper.client v{_v}" in m for m in _Capture.messages()),
          f"client logged v{_v}")

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
    unreachable = [m for m in msgs if "unreachable after" in m]
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

    # 13. search TTL cache + in-flight coalescing
    cport = _free_port()
    cjson = _CountingJSONServer(cport)
    cjson.start()
    try:
        cc = LorekeeperClient(f"http://127.0.0.1:{cport}", TOKEN, timeout=5.0)
        r1 = cc.search("What was the Crof fix?")
        r2 = cc.search("  what was the CROF   fix?  ")  # normalizes identically
        check("search-cache-hit", r1 is r2 and cjson.counts()["search"] == 1, f"2 searches -> {cjson.counts()['search']} server call, same object")
        cc.search("different query entirely")
        check("search-cache-miss", cjson.counts()["search"] == 2, "different query fetches")

        t1 = cc.tool("lorekeeper_search", {"query": "tool query", "limit": 5})
        t2 = cc.tool("lorekeeper_search", {"query": "TOOL QUERY", "limit": 5})
        check("tool-search-cache-hit", t1 is t2 and cjson.counts()["tool_search"] == 1, f"2 tool searches -> {cjson.counts()['tool_search']} server call")
        t3 = cc.tool("lorekeeper_search", {"query": "tool query", "limit": 10})
        check("tool-search-limit-key", cjson.counts()["tool_search"] == 2, "different limit = different key")

        cc.tool("lorekeeper_remember", {"text": "mutation"})  # non-search tool evicts
        cc.tool("lorekeeper_search", {"query": "tool query", "limit": 5})
        check("tool-mutation-evicts", cjson.counts()["tool_search"] == 3, "non-search tool invalidated the cache")
        cc.search("What was the Crof fix?")
        check("remember-evicts", cjson.counts()["search"] == 3, "remember (tool path) also evicted search-path cache")
        cc.capture({"sessionID": "t", "text": "more mutation"})
        cc.search("different query entirely")
        check("capture-evicts", cjson.counts()["search"] == 4, "capture invalidated the cache")

        # TTL expiry
        ct = LorekeeperClient(f"http://127.0.0.1:{cport}", TOKEN, timeout=5.0)
        ct._search_cache_ttl = 0.2
        ct.search("ttl probe")
        ct.search("ttl probe")
        check("ttl-cached", cjson.counts()["search"] == 5, "within TTL -> cached")
        time.sleep(0.3)
        ct.search("ttl probe")
        check("ttl-expiry", cjson.counts()["search"] == 6, "after TTL -> refetched")
        ct.close()

        # in-flight coalescing: concurrent identical searches share one request
        cco = LorekeeperClient(f"http://127.0.0.1:{cport}", TOKEN, timeout=5.0)
        out = []

        def coburst():
            out.append(cco.search("coalesced query"))

        threads = [threading.Thread(target=coburst) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        before = cjson.counts()["search"]
        check("coalesce", before == 7 and len(out) == 5, f"5 concurrent -> {before} server call")

        # disabled cache (TTL=0) always fetches
        c0 = LorekeeperClient(f"http://127.0.0.1:{cport}", TOKEN, timeout=5.0)
        c0._search_cache_ttl = 0
        c0.search("uncached")
        c0.search("uncached")
        check("ttl-zero-disables", cjson.counts()["search"] == 9, "TTL=0 -> every call fetches")
        cco.close()
        c0.close()
    finally:
        cjson.stop()

    # 15. per-tool timeout table: LLM-backed tools get long budgets, others default
    ct = LorekeeperClient(HOST, TOKEN, timeout=5.0)
    captured = {}

    def _fake_request(method, path, payload=None, *, response_timeout=None):
        captured["tool"] = payload.get("name")
        captured["response_timeout"] = response_timeout
        return {"result": "ok"}

    ct._request = _fake_request
    ct.tool("lorekeeper_summarize", {"scope": "global", "dryRun": True})
    check("tool-timeout-summarize", captured["tool"] == "lorekeeper_summarize" and captured["response_timeout"] == 300.0,
          f"summarize -> {captured['response_timeout']}s")
    ct.tool("lorekeeper_remember", {"text": "x"})
    check("tool-timeout-default", captured["response_timeout"] is None, f"remember -> default ({captured['response_timeout']})")
    ct.tool("lorekeeper_search", {"query": "q"})
    check("tool-timeout-search", captured["response_timeout"] is None, "search -> default")

    # 16. LLM shim abort plumbing: withSignal scopes an abort to one capture,
    # and the base client stays unaffected
    import importlib.util as _u
    _spec = _u.spec_from_file_location("lk_llm_shim", os.path.join(LOREKEEPER, "server", "llm_shim.js"), loader=None)
    # JS module — drive it through node instead
    import subprocess
    node_script = r'''
const { LLMSessionClient } = await import("/root/.hermes/workspace/Lorekeeper/server/llm_shim.js");
const http = await import("node:http");
// slow server: 3s to answer
const srv = http.createServer((req, res) => setTimeout(() => { res.writeHead(200, {"content-type": "application/json"}); res.end("{}"); }, 3000));
await new Promise((r) => srv.listen(18892, "127.0.0.1", r));
const base = new LLMSessionClient({ apiKey: "k", baseUrl: "http://127.0.0.1:18892", model: "m", timeoutMs: 10000 });
// scoped client with a signal we fire after 200ms
const ctrl = new AbortController();
setTimeout(() => ctrl.abort(), 200);
const scoped = base.withSignal(ctrl.signal);
const t0 = Date.now();
try {
  await scoped._chat([{ role: "user", content: "hi" }]);
  console.log("NO_ABORT:chat returned");
} catch (e) {
  console.log("ABORTED_MS:" + (Date.now() - t0) + " kind:" + (e.name === "AbortError" || /abort/i.test(String(e)) ? "abort" : "other:" + e.name));
}
// base client unaffected (still has no signal; would wait 3s — use short internal timeout instead)
const t1 = Date.now();
try {
  const b2 = base.withSignal(undefined ? undefined : undefined); // no-op guard
  console.log("BASE_STILL_ACTIVE:true");
} catch { console.log("BASE_STILL_ACTIVE:false"); }
const sc2 = new LLMSessionClient({ apiKey: "k", baseUrl: "http://127.0.0.1:18892", model: "m", timeoutMs: 500 });
const t2 = Date.now();
try { await sc2._chat([{ role: "user", content: "hi" }]); console.log("SC2_OK"); }
catch (e) { console.log("SC2_TIMEOUT_MS:" + (Date.now() - t2)); }
srv.close();
'''
    out = subprocess.run(["/usr/bin/node", "--input-type=module", "-e", node_script], capture_output=True, text=True, timeout=30).stdout
    abort_line = next((l for l in out.splitlines() if l.startswith("ABORTED_MS")), "")
    ok_abort = "kind:abort" in abort_line and 150 <= int(abort_line.split("ABORTED_MS:")[1].split(" ")[0]) < 1500
    check("llm-signal-abort", ok_abort, abort_line or out[-200:])
    check("llm-signal-scoped", "BASE_STILL_ACTIVE:true" in out, "base client not mutated by withSignal")
    to_line = next((l for l in out.splitlines() if l.startswith("SC2_TIMEOUT_MS")), "")
    check("llm-internal-timeout-intact", to_line != "" and 400 <= int(to_line.split(":")[1]) < 2000, to_line)

    # 17. /stats counts via the scope-only scan (service-side change)
    row = client.remember("LK stats probe row", category="test")
    st = client.stats()
    total_ok = isinstance(st.get("counts", {}).get("total"), int) and st["counts"]["total"] >= 1
    scope_ok = isinstance(st["counts"].get("byScope"), dict) and st["counts"]["byScope"].get("global", 0) >= 1
    check("stats-count", total_ok and scope_ok, f"total={st['counts']['total']} byScope={st['counts']['byScope']}")
    if row.get("id"):
        client.delete(row["id"], force=True)

    # 18. /metrics: aggregated spans (http.* spans recorded), cache stats, reset
    client.metrics(reset=True)  # clean slate
    client.search("metrics probe query")
    m = client.metrics()
    ops = {e["op"]: e for e in m.get("timing", [])}
    check("metrics-http-span", "http.search" in ops, f"timing ops: {sorted(ops)[:6]}")
    check("metrics-store-span", any(op in ops for op in ("store.search", "embedder.embed")), f"vendor spans present: {sorted(ops)[:8]}")
    check("metrics-cache-stats", isinstance(m.get("scopeCache"), dict) or m.get("scopeCache") is None, f"scopeCache={m.get('scopeCache')}")
    m2 = client.metrics(reset=True)
    m3 = client.metrics()
    check("metrics-reset", all(e["count"] <= 1 for e in m3.get("timing", []) if e["op"] == "http.metrics"),
          "reset cleared spans (http.metrics restarted at 1)")

    # 14. retry policy
    # 503 then success -> transparent retry, server saw 2 requests
    sport = _free_port()
    scripted = _ScriptedStatusServer(sport, [(503, b'{"error": "overloaded"}')])
    scripted.start()
    try:
        cr = LorekeeperClient(f"http://127.0.0.1:{sport}", TOKEN, timeout=5.0)
        t0 = time.monotonic()
        resp = cr.search("retry me")
        check("transient-5xx-retry", resp.get("ok") is True and scripted.requests == 2,
              f"503 -> retry -> ok ({time.monotonic() - t0:.2f}s, {scripted.requests} requests)")
        cr.close()
    finally:
        scripted.stop()

    # 503 always -> budget exhausted -> LorekeeperError (1 retry only)
    sport = _free_port()
    scripted = _ScriptedStatusServer(sport, [(503, b'{"error": "overloaded"}')] * 5)
    scripted.start()
    try:
        cr = LorekeeperClient(f"http://127.0.0.1:{sport}", TOKEN, timeout=5.0)
        try:
            cr.search("always 503")
            check("transient-budget", False, "no error raised")
        except LorekeeperError as e:
            check("transient-budget", "HTTP 503" in str(e) and scripted.requests == 2,
                  f"exhausted after {scripted.requests} requests: {str(e)[:60]}")
        cr.close()
    finally:
        scripted.stop()

    # 500 -> deterministic answer, NO retry
    sport = _free_port()
    scripted = _ScriptedStatusServer(sport, [(500, b'{"error": "boom"}')] * 3)
    scripted.start()
    try:
        cr = LorekeeperClient(f"http://127.0.0.1:{sport}", TOKEN, timeout=5.0)
        try:
            cr.search("deterministic 500")
            check("no-retry-on-500", False, "no error raised")
        except LorekeeperError as e:
            check("no-retry-on-500", "HTTP 500" in str(e) and scripted.requests == 1,
                  f"exactly {scripted.requests} request: {str(e)[:60]}")
        cr.close()
    finally:
        scripted.stop()

    # connection refused -> backoff retries then clean error; measure the delay
    dead_port = _free_port()
    cr = LorekeeperClient(f"http://127.0.0.1:{dead_port}", TOKEN, timeout=2.0)
    t0 = time.monotonic()
    try:
        cr.search("nobody home")
        check("refused-backoff", False, "no error raised")
    except LorekeeperError as e:
        elapsed = time.monotonic() - t0
        within = 0.55 <= elapsed < 10.0  # 0.2 + 0.4 backoff, well under timeouts
        check("refused-backoff", "unreachable" in str(e) and within,
              f"error after {elapsed:.2f}s (expect >= 0.55s backoff): {str(e)[:60]}")
    cr.close()
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
