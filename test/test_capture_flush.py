#!/usr/bin/env python3
"""Regression tests for the provider's capture-flush logic (no service needed).

Instantiates LorekeeperMemoryProvider directly with a fake client and asserts
the buffer/flush contract:
  - small turns are buffered, NOT sent per turn (one LLM extraction per
    threshold, not per turn)
  - crossing the threshold flushes the accumulated text exactly once
  - on_session_end / pre-compress flush the remainder
  - a failed flush retains the buffer for the next attempt
  - assistant text is capped before it enters the buffer
  - lorekeeper.json captureFlushChars override is honored

Run:  python3 lorekeeper-tests/test_capture_flush.py
Exit 0 = all pass, 1 = failures.
"""
import importlib
import json
import sys
import time
import threading

HERMES_CORE = "/usr/local/lib/hermes-agent"
LOREKEEPER = "/root/.hermes/workspace/Lorekeeper"

sys.path.insert(0, HERMES_CORE)
import hermes_bootstrap  # wires the uv-managed venv (ruamel etc.) into sys.path
sys.path.insert(0, LOREKEEPER)

# provider/__init__.py imports hermes core (MemoryProvider, tool_error) — both
# are stdlib-light. The service must NOT be touched: fake client only.
provider = importlib.import_module("provider")
LorekeeperMemoryProvider = provider.LorekeeperMemoryProvider

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


class FakeClient:
    """Records /capture payloads; optional failure injection."""

    def __init__(self):
        self.captures = []
        self.fail = False
        self.lock = threading.Lock()

    def capture(self, payload):
        with self.lock:
            if self.fail:
                raise RuntimeError("injected failure")
            self.captures.append(payload)

    def search(self, query, limit=5, scope=None, source=None):
        return {"results": [{"text": f"prefetched: {query}"}]}

    def wait_captures(self, n, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if len(self.captures) >= n:
                    return list(self.captures)
            time.sleep(0.01)
        with self.lock:
            return list(self.captures)


def make_provider(client):
    p = LorekeeperMemoryProvider()
    p._client = client
    p._current_session = "s1"
    return p


def flush_chars(p):
    try:
        return int(p._config.get("captureFlushChars") or provider._CAPTURE_FLUSH_CHARS)
    except (TypeError, ValueError):
        return provider._CAPTURE_FLUSH_CHARS


client = FakeClient()
p = make_provider(client)
THRESH = flush_chars(p)
SID = "s1"

# --- 1. small turns stay buffered --------------------------------------------
turn = "x" * 100
for i in range(max(1, THRESH // len(turn) - 2)):
    p.sync_turn(f"turn {i} {turn}", "ok", session_id=SID)
time.sleep(0.2)
n0 = client.wait_captures(1, 0.3)
check("no-flush-below-threshold", len(n0) == 0, f"{len(n0)} captures after small turns (threshold={THRESH})")

# --- 2. crossing threshold flushes accumulated text ---------------------------
big = "y" * (THRESH + 50)
p.sync_turn("big turn", big, session_id=SID)
caps = client.wait_captures(1)
flushed = caps[0]["text"] if caps else ""
has_all = all(f"turn {i}" in flushed for i in range(max(1, THRESH // len(turn) - 2)))
check("threshold-flush", len(caps) == 1 and caps[0]["sessionID"] == SID and has_all,
      f"1 flush containing all buffered turns (len={len(flushed)})")

# buffer drained after flush
with p._capture_lock:
    remaining = p._capture_buffers.get(SID, "")
check("buffer-drained-after-flush", remaining == "", f"remaining={len(remaining)} chars")

# --- 3. on_session_end flushes the remainder ----------------------------------
p.sync_turn("tail turn", "short answer", session_id=SID)
time.sleep(0.1)
caps = client.wait_captures(1)
check("no-flush-remainder", len(caps) == 1, "remainder still buffered")
p.on_session_end([])
caps = client.wait_captures(2)
check("session-end-flush", len(caps) == 2 and "tail turn" in caps[1]["text"], f"captured: {caps[1]['text'][:60] if len(caps) > 1 else 'missing'}")

# --- 4. on_pre_compress flushes ------------------------------------------------
p.sync_turn("pre-compress turn", "short", session_id=SID)
time.sleep(0.1)
p.on_pre_compress([])
caps = client.wait_captures(3)
check("pre-compress-flush", len(caps) == 3 and "pre-compress turn" in caps[2]["text"], f"captured: {caps[2]['text'][:60] if len(caps) > 2 else 'missing'}")

# --- 5. failed flush retains buffer --------------------------------------------
p.sync_turn("before failure", "ans", session_id=SID)
client.fail = True
p.on_session_end([])  # flush attempt fails
time.sleep(0.2)
with p._capture_lock:
    retained = p._capture_buffers.get(SID, "")
check("failed-flush-retains", "before failure" in retained, f"retained {len(retained)} chars")
client.fail = False
p.on_session_end([])
caps = client.wait_captures(4)
check("retained-buffer-recovered", len(caps) == 4 and "before failure" in caps[3]["text"], f"recovered: {caps[3]['text'][:60] if len(caps) > 3 else 'missing'}")

# --- 6. assistant text cap ------------------------------------------------------
huge = "z" * (provider._CAPTURE_TURN_MAX_CHARS + 5000)
p._capture_buffers.pop(SID, None)
p.sync_turn("user question", huge, session_id=SID)
with p._capture_lock:
    buffered = p._capture_buffers.get(SID, "")
expected_max = len("user question") + 1 + provider._CAPTURE_TURN_MAX_CHARS
check("assistant-capped", len(buffered) <= expected_max, f"buffered {len(buffered)} <= {expected_max}")
p._capture_buffers.pop(SID, None)

# --- 7. config override ----------------------------------------------------------
p2 = make_provider(FakeClient())
p2._config = {"captureFlushChars": "300"}
sid2 = "s2"
for i in range(4):
    p2.sync_turn(f"t{i} " + "a" * 90, "ok", session_id=sid2)
caps2 = p2._client.wait_captures(1)
check("config-override", len(caps2) >= 1 and caps2[0]["sessionID"] == sid2,
      f"{len(caps2)} flush(es) at override threshold=300")

# --- 8. empty turn is a no-op ----------------------------------------------------
before = len(client.captures)
p.sync_turn("", "", session_id=SID)
time.sleep(0.1)
check("empty-turn-noop", len(client.captures) == before, "no capture for empty turn")

# --- 9. in-flight guard: concurrent flush doesn't double-send --------------------
p._capture_buffers.pop(SID, None)
client.captures.clear()
p.sync_turn("guard turn", "ok", session_id=SID)
p._flush_capture(SID, synchronous=True)
p._flush_capture(SID, synchronous=True)
caps = client.wait_captures(1)
guard_ok = len(caps) == 1 and all("guard turn" in c["text"] for c in caps)
check("in-flight-guard", guard_ok, f"{len(caps)} capture(s), buffer popped after first")

# --- 10. session switch flushes on reset ------------------------------------------
client.captures.clear()
p.sync_turn("old session turn", "ok", session_id="old")
p.on_session_switch("new", reset=True)
caps = client.wait_captures(1)
check("session-switch-flush", len(caps) == 1 and caps[0]["sessionID"] == "old" and "old session turn" in caps[0]["text"],
      f"flushed old-session buffer on reset")

# --- 11. prefetch returns the searched body --------------------------------------
client.captures.clear()
p.on_turn_start(1, "what was the crof fix?")
body = p.prefetch("what was the crof fix?")
check("prefetch-result", "prefetched: what was the crof fix?" in body, f"body: {body[:60]!r}")
body2 = p.prefetch("completely different query", session_id="x")
check("prefetch-new-query", "prefetched: completely different query" in body2, "new query searched synchronously, no stale cache")

# --- 12. shared executor: repeated flushes keep the thread count bounded ---------
p._capture_buffers.pop(SID, None)
client.captures.clear()
baseline = threading.active_count()
for i in range(30):
    p.sync_turn(f"pool turn {i} " + "m" * 50, "ok", session_id="pooltest")
    if i % 3 == 0:
        p._flush_capture("pooltest", synchronous=False)
time.sleep(0.5)
after = threading.active_count()
check("executor-bounded", after <= baseline + 5, f"threads {baseline} -> {after} after 30 flush cycles")
p._capture_buffers.pop("pooltest", None)

# --- summary -----------------------------------------------------------------------
passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
print(f"\nPASS {passed} / FAIL {failed} / TOTAL {len(results)}")
sys.exit(1 if failed else 0)
