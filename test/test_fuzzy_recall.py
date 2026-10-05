#!/usr/bin/env python3
"""Regression tests for the trigram fuzzy channel (R1, store.js).

Boots an EMPTY scratch service and asserts the typo-tolerance contract that
Fuse.js used to provide:
  - exact queries still hit
  - typo'd queries still hit the right rows (the fuzzy channel's whole job)
  - irrelevant queries return no false positives from the fuzzy channel
  - fuzzy channel is fast: warm store.search well under the old Fuse cost

Run:  python3 test/test_fuzzy_recall.py
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

LOREKEEPER = "/root/.hermes/workspace/Lorekeeper"
PORT = 18786
HOST = f"http://127.0.0.1:{PORT}"
TOKEN = "fuzzy-test-token"

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def call(path, payload, timeout=60):
    req = urllib.request.Request(f"{HOST}{path}", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


tmp = tempfile.mkdtemp(prefix="lk-fuzzy-")
proc = subprocess.Popen(["/usr/bin/node", "server/index.js"], cwd=LOREKEEPER,
    env=dict(os.environ, LOREKEEPER_PORT=str(PORT), LOREKEEPER_DB_PATH=os.path.join(tmp, "db"),
             LOREKEEPER_GRAPH_PATH=os.path.join(tmp, "graph.db"), LOREKEEPER_TOKEN=TOKEN,
             OPENCODE_MEMORY_PRO_CAPTURE_MODE="heuristics"),
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            call("/health", {})
            break
        except Exception:
            time.sleep(0.2)
    call("/init", {})

    rows = [
        "The lorekeeper deploy workflow uses install.sh plugin-only mode",
        "Webull MCP token expires every two hours and needs a refresh script",
        "The quick brown fox jumps over the lazy dog near the birch tree",
        "OPNsense firewall changes go through the API, never SSH",
    ]
    ids = []
    for text in rows:
        ids.append(call("/remember", {"content": text, "category": "test"})["id"])

    # exact + typo probes: (query, expected-id-index)
    probes = [
        ("lorekeeper deploy workflow", 0),
        ("install.sh plugin only mode", 0),
        ("webull mcp token refresh", 1),
        ("webull mcp tokn refresh", 1),          # typo: tokn
        ("quick brown fox jumps lazy dog", 2),
        ("quik brown fox jups over layz dog", 2),  # typos: quik/jups/layz
        ("opnsense firewall api changes", 3),
        ("opnsense firewal api chnges", 3),        # typos: firewal/chnges
    ]
    for q, expect in probes:
        res = call("/search", {"query": q, "limit": 5})
        top = res["results"][0]["id"] if res["results"] else None
        in5 = ids[expect] in [x["id"] for x in res["results"]]
        check(f"recall:{q[:34]}", in5 and top == ids[expect], f"top={'ok' if top == ids[expect] else 'WRONG'} in5={in5}")

    # negative: an unrelated query must not single out any row. (On a tiny
    # corpus RRF returns all rows at ~0.88-0.91 — pre-existing behavior,
    # identical under old Fuse — so assert the SPREAD stays small rather than
    # absolute scores.)
    res = call("/search", {"query": "quantum flux capacitor retroencabulator", "limit": 5})
    scores = [x["score"] for x in res["results"] if x["id"] in ids]
    spread = max(scores) - min(scores) if len(scores) >= 2 else 0.0
    check("no-false-differentiation", spread < 0.05, f"{len(scores)} rows, spread {spread:.3f} (all ~equal = fuzzy silent)")

    # perf: warm store.search avg should be far below the old Fuse cost
    call("/metrics", {"reset": True})
    for q, _ in probes[:4]:
        call("/search", {"query": q, "limit": 5})
    m = call("/metrics", {})
    ss = next(e for e in m["timing"] if e["op"] == "store.search")
    # 10 rows: even Fuse was fast here — assert we're in the trigram regime (<10ms)
    check("fuzzy-fast", ss["avgMs"] < 10, f"store.search avg {ss['avgMs']}ms over {len(probes[:4])} warm searches")

    # R2: a put must NOT invalidate the scope cache (incremental patch).
    # misses should stay put across a remember + search cycle.
    before = call("/metrics", {})["scopeCache"]
    call("/remember", {"content": "R2 probe row: incremental cache patch keeps the search cache warm", "category": "test"})
    call("/search", {"query": "incremental cache patch warm", "limit": 5})
    after = call("/metrics", {})["scopeCache"]
    delta = after["misses"] - before["misses"]
    check("r2-cache-stays-warm", delta == 0, f"cache misses delta {delta} after put+search (0 = patched, 1+ = full rebuild)")
    hit = call("/search", {"query": "R2 probe row incremental", "limit": 3})["results"]
    check("r2-new-row-visible", len(hit) > 0 and "R2 probe row" in hit[0]["text"], f"patched row searchable: {hit[0]['text'][:40] if hit else 'none'}")
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
