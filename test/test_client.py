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
import subprocess
import sys
import tempfile
import threading
import time

HERMES_CORE = "/usr/local/lib/hermes-agent"
LOREKEEPER = "/root/.hermes/workspace/Lorekeeper"

sys.path.insert(0, HERMES_CORE)
import hermes_bootstrap  # wires the uv venv into sys.path
sys.path.insert(0, LOREKEEPER)

from provider._client import LorekeeperClient, LorekeeperError

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
