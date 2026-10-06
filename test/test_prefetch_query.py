#!/usr/bin/env python3
"""Regression tests for prefetch query shaping (no service needed).

Asserts:
  - _shape_prefetch_query: passthrough, head truncation at sentence boundary,
    question-sentence preference (even when the question sits past the
    truncation point), code-block/URL stripping, idempotency
  - provider: on_turn_start AND prefetch(keyed identically) send the SHAPED
    query to the client — exactly one search per turn
  - lorekeeper.json prefetchQueryChars override is honored

Run:  python3 test/test_prefetch_query.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HERMES_CORE = "/usr/local/lib/hermes-agent"
LOREKEEPER = "/root/.hermes/workspace/Lorekeeper"

sys.path.insert(0, HERMES_CORE)
import hermes_bootstrap
sys.path.insert(0, LOREKEEPER)

os.environ.pop("LOREKEEPER_HOST", None)
os.environ.pop("LOREKEEPER_TOKEN", None)

import provider as provider_mod
from provider import _shape_prefetch_query as shape

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


class RecordingClient:
    def __init__(self):
        self.searches = []

    def search(self, query, limit=5, scope=None, source=None):
        self.searches.append(query)
        return {"results": [{"text": f"hit for {query[:30]}"}]}


# --- unit: shaping ---------------------------------------------------------------
check("passthrough", shape("short question about crof", 400) == "short question about crof", "short text unchanged")

long_msg = "A" * 300 + ". " + "B" * 300 + ". " + "C" * 100 + "."
out = shape(long_msg, 400)
check("head-truncate", len(out) <= 400 and out.endswith("."), f"len={len(out)}, cut at sentence boundary")

long_with_question = "Here is a lot of context. " + "filler text. " * 20 + "What was the crof fix about?"
out = shape(long_with_question, 100)
check("question-preference", out == "What was the crof fix about?", f"got: {out!r}")

code_msg = "```python\ndef f():\n    return 'x' * 10000\n```\nHow do I fix the parser error?"
out = shape(code_msg, 400)
check("code-stripped", "def f" not in out and out == "How do I fix the parser error?", f"got: {out!r}")

url_msg = "Check https://example.com/very/long/path?with=params and tell me what is happening here"
out = shape(url_msg, 400)
check("url-stripped", "example.com" not in out, f"got: {out!r}")

check("whitespace-collapse", shape("a\n\n  b   c", 400) == "a b c", "collapsed")
check("idempotent", shape(shape(long_with_question, 100), 100) == shape(long_with_question, 100), "shape(shape(x)) == shape(x)")
check("empty", shape("", 400) == "" and shape("   ", 400) == "", "empty -> empty")

# --- provider: both entry points key on the shaped query ---------------------------
p = provider_mod.LorekeeperMemoryProvider()
rec = RecordingClient()
p._client = rec
p._current_session = "s1"

long_turn = (
    "Todd here. I want to refactor the whole provider module tonight. "
    "Also review the test suite while you are at it. " * 6
    + "By the way, whatever happened with the crof fix?"
)
p.on_turn_start(1, long_turn)
deadline = time.time() + 3
while time.time() < deadline and not rec.searches:
    time.sleep(0.01)
check("shaped-on-turn-start", rec.searches == ["By the way, whatever happened with the crof fix?"],
      f"searched: {rec.searches[:1]!r}")

# prefetch() with the same raw message keys identically -> no second search
body = p.prefetch(long_turn, session_id="s1")
time.sleep(0.2)
check("prefetch-same-key", len(rec.searches) == 1 and "hit for" in body, f"{len(rec.searches)} search, body={body[:40]!r}")

# a genuinely different turn refetches
p2 = provider_mod.LorekeeperMemoryProvider()
rec2 = RecordingClient()
p2._client = rec2
p2.on_turn_start(2, "short different turn")
deadline = time.time() + 3
while time.time() < deadline and not rec2.searches:
    time.sleep(0.01)
p2.prefetch("short different turn", session_id="s1")
check("new-turn-searches", rec2.searches == ["short different turn"], f"searched: {rec2.searches!r}")

# --- config override ----------------------------------------------------------------
p3 = provider_mod.LorekeeperMemoryProvider()
rec3 = RecordingClient()
p3._client = rec3
p3._config = {"prefetchQueryChars": "40"}
p3.on_turn_start(3, "x" * 300 + ". tail sentence that matters")
deadline = time.time() + 3
while time.time() < deadline and not rec3.searches:
    time.sleep(0.01)
q = rec3.searches[0] if rec3.searches else ""
check("config-override", len(q) <= 40, f"override honored, len={len(q)}: {q!r}")

# all-code message falls back to a raw head slice (never an empty query)
p4 = provider_mod.LorekeeperMemoryProvider()
rec4 = RecordingClient()
p4._client = rec4
p4.on_turn_start(4, "```\n" + "print('x')\n" * 50 + "\n```")
deadline = time.time() + 3
while time.time() < deadline and not rec4.searches:
    time.sleep(0.01)
q = rec4.searches[0] if rec4.searches else ""
check("all-code-fallback", q.startswith("```") and len(q) <= 400, f"fallback slice len={len(q)}")

passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
print(f"\nPASS {passed} / FAIL {failed} / TOTAL {len(results)}")
sys.exit(1 if failed else 0)
