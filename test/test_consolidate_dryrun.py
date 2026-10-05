#!/usr/bin/env python3
"""Regression tests for consolidate dryRun (B2 / CONSOLIDATE_DRYRUN 0.2.4).

Background: memory_consolidate had NO dryRun arg — zod silently stripped the
agent's dryRun=true and the call ran a REAL merge on the live store
(2026-10-05: 1 pair merged during a "dry" call). dryRun is now first-class:

  - dryRun=true returns a read-only estimate (pairs, counters) and writes
    NOTHING (row counts, statuses and search results unchanged)
  - the estimate is fast: cache-first row read + in-memory exact cosine
    (spanExtra.source == 'cache'), no LanceDB ANN scan
  - a real run still merges (confirm=true path unchanged)

Boots a scratch service pointed at a mock Ollama embedder so duplicate
similarity is deterministic.

Run:  python3 test/test_consolidate_dryrun.py
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
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

LOREKEEPER = "/root/.hermes/workspace/Lorekeeper"
PORT = 18787
OLLAMA_PORT = 11435
HOST = f"http://127.0.0.1:{PORT}"
TOKEN = "consolidate-dryrun-token"

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def call(path, payload, timeout=60):
    req = urllib.request.Request(f"{HOST}{path}", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# --- mock Ollama embedder: deterministic 8-dim vectors -----------------------
# DUP-A* texts -> e1 (unit axis 1); UNRELATED-B -> e2; OTHER-C -> e3.
# The two DUP-A texts get a tiny deterministic offset so sim ~0.999 (>= 0.95
# threshold) but not exactly 1.0.
VECTORS = {
    "DUP-A-1": [1.0, 0, 0, 0, 0, 0, 0, 0.02],
    "DUP-A-2": [1.0, 0, 0, 0, 0, 0, 0, 0.0],
    "UNRELATED-B": [0, 1.0, 0, 0, 0, 0, 0, 0],
    "OTHER-C": [0, 0, 1.0, 0, 0, 0, 0, 0],
}


class MockOllama(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        prompt = body.get("prompt", "")
        key = prompt.split("|")[0].strip()
        # unknown prompts (e.g. the "dimension probe" at init) get a default
        # unit vector instead of a 404
        vec = VECTORS.get(key, [1.0, 0, 0, 0, 0, 0, 0, 0])
        norm = sum(x * x for x in vec) ** 0.5
        resp = {"embedding": [x / norm for x in vec]}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


ollama = HTTPServer(("127.0.0.1", OLLAMA_PORT), MockOllama)
threading.Thread(target=ollama.serve_forever, daemon=True).start()

tmp = tempfile.mkdtemp(prefix="lk-consolidate-")
proc = subprocess.Popen(["/usr/bin/node", "server/index.js"], cwd=LOREKEEPER,
    env=dict(os.environ, LOREKEEPER_PORT=str(PORT), LOREKEEPER_DB_PATH=os.path.join(tmp, "db"),
             LOREKEEPER_GRAPH_PATH=os.path.join(tmp, "graph.db"), LOREKEEPER_TOKEN=TOKEN,
             OPENCODE_MEMORY_PRO_CAPTURE_MODE="heuristics",
             OPENCODE_MEMORY_PRO_OLLAMA_BASE_URL=f"http://127.0.0.1:{OLLAMA_PORT}",
             OPENCODE_MEMORY_PRO_EMBEDDING_MODEL="mock-embed"),
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def total_active():
    return call("/stats", {})["counts"]["total"]


try:
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            call("/health", {})
            break
        except Exception:
            time.sleep(0.2)
    call("/init", {})

    id_a1 = call("/remember", {"content": "DUP-A-1| lorekeeper deploy workflow details", "category": "test"})["id"]
    id_a2 = call("/remember", {"content": "DUP-A-2| lorekeeper deploy workflow details", "category": "test"})["id"]
    id_b = call("/remember", {"content": "UNRELATED-B| webull token refresh cadence", "category": "test"})["id"]
    id_c = call("/remember", {"content": "OTHER-C| opnsense firewall api workflow", "category": "test"})["id"]

    # warm the scope cache: the patch-on-put path only maintains a fresh
    # entry after one exists, so build it with one search first
    call("/search", {"query": "warm cache", "limit": 3})

    # 1. dryRun detects the near-duplicate pair and reports it
    res = call("/consolidate", {"scope": "global", "dryRun": True})
    r = res["result"]
    pair_ids = {id_a1, id_a2}
    got_pair = (r.get("dryRun") is True and r["mergedPairs"] == 1 and len(r.get("pairs", [])) == 1
                and {r["pairs"][0]["survivor"], r["pairs"][0]["absorbed"]} == pair_ids)
    check("dryrun-detects-pair", got_pair,
          f"dryRun={r.get('dryRun')} mergedPairs={r['mergedPairs']} pair={r.get('pairs')}")

    # 2. dryRun writes nothing: counts and search results unchanged
    check("dryrun-no-writes-count", total_active() == 4, f"total after dryRun = {total_active()} (expect 4)")
    s = call("/search", {"query": "DUP-A-1 lorekeeper deploy workflow", "limit": 5})
    still_both = {id_a1, id_a2} <= {x["id"] for x in s["results"]}
    check("dryrun-no-writes-search", still_both, f"search after dryRun: {[x['id'][:8] for x in s['results']]}")

    # 3. dryRun reads from the scope cache (fast path) and is quick
    m = call("/metrics", {})
    span = next((e for e in m["timing"] if e["op"] == "store.consolidate" and e.get("lastExtra", {}).get("source") == "cache"), None)
    check("dryrun-cache-source", span is not None, f"consolidate span lastExtra: {'missing' if span is None else span.get('lastExtra')}")
    check("dryrun-fast", r["elapsedMs"] < 3000, f"estimate took {r['elapsedMs']}ms")

    # 4. unrelated rows never pair up
    res2 = call("/consolidate", {"scope": "global", "dryRun": True})
    r2 = res2["result"]
    check("dryrun-single-pair-only", r2["mergedPairs"] == 1 and r2["candidatePairs"] if False else r2["mergedPairs"] == 1,
          f"mergedPairs={r2['mergedPairs']} (only the A1/A2 pair)")

    # 5. real run still merges the same pair
    real = call("/consolidate", {"scope": "global"})["result"]
    check("real-run-merges", real["mergedPairs"] == 1 and "dryRun" not in real,
          f"mergedPairs={real['mergedPairs']} keys={sorted(real.keys())}")
    check("real-run-merged-hidden", total_active() == 3, f"total after real merge = {total_active()} (expect 3)")
    s = call("/search", {"query": "DUP-A-1 lorekeeper deploy workflow", "limit": 5})
    check("real-run-search-1-hit", [x["id"] for x in s["results"]].count(id_a1) + [x["id"] for x in s["results"]].count(id_a2) == 1,
          f"alpha rows visible after merge: {sum(1 for x in s['results'] if x['id'] in pair_ids)}")

    # 6. dryRun after the real merge: nothing left to merge
    after = call("/consolidate", {"scope": "global", "dryRun": True})["result"]
    check("dryrun-clean-after-merge", after["mergedPairs"] == 0 and len(after.get("pairs", [])) == 0,
          f"mergedPairs={after['mergedPairs']} pairs={after.get('pairs')}")

finally:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    ollama.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
