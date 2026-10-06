#!/usr/bin/env python3
"""Regression tests for the 0.2.9 client truthfulness hardening (F2).

Vendor tools that fail inside a 2xx response (e.g. memory_import's read/
validation errors) return a JSON body with an "error" key instead of throwing.
Both dispatch paths must surface those as tool failures rather than a
clean-looking result. Covers:
  - error_from_result() discrimination (pure)
  - tools._make_handler: error-shaped result -> "Lorekeeper tool failed: ..."
  - tools._make_handler: clean result passes through unchanged
  - non-2xx (LorekeeperError) already handled by the handler

Run:  python3 lorekeeper-tests/test_v029_client.py
No network, no LLM spend.
"""
import json
import sys

HERMES_CORE = "/usr/local/lib/hermes-agent"
LOREKEEPER = "/root/.hermes/workspace/Lorekeeper"

sys.path.insert(0, HERMES_CORE)
import hermes_bootstrap  # wires the uv venv into sys.path
sys.path.insert(0, LOREKEEPER)

from provider._client import error_from_result, LorekeeperError
from provider.tools import _make_handler

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


# --- error_from_result: pure discrimination -----------------------------------
check("err-string-json-error",
      error_from_result({"result": json.dumps({"error": "Failed to read /tmp/x: ENOENT"})})
      == "Failed to read /tmp/x: ENOENT")
check("err-dict-payload-error",
      error_from_result({"result": {"error": "boom"}}) == "boom")
check("err-clean-json",
      error_from_result({"result": json.dumps({"imported": 5, "replaced": 0})}) is None)
check("err-clean-text",
      error_from_result({"result": "1. foo\n2. bar"}) is None)
check("err-empty-string",
      error_from_result({"result": ""}) is None)
check("err-non-dict",
      error_from_result(None) is None)
check("err-missing-result-key",
      error_from_result({"ok": True}) is None)
check("err-malformed-json",
      error_from_result({"result": "{not json"}) is None)
check("err-null-error-value",
      error_from_result({"result": json.dumps({"error": None})}) is None)

# --- _make_handler: error-shaped result surfaced as failure -------------------
class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def tool(self, name, args):
        return {"result": self._payload}


h_err = _make_handler(_FakeClient(json.dumps({"error": "Not a memory_export backup"})), "lorekeeper_import")
out = h_err({})
check("handler-surfaces-200-error",
      isinstance(out, str) and out.startswith("Lorekeeper tool failed: Not a memory_export backup"), out)

h_ok = _make_handler(_FakeClient(json.dumps({"imported": 3, "replaced": 0})), "lorekeeper_import")
out = h_ok({})
check("handler-passes-clean-json",
      isinstance(out, str) and json.loads(out).get("imported") == 3, out)

h_text = _make_handler(_FakeClient("plain result text\n"), "lorekeeper_global_list")
out = h_text({})
check("handler-passes-plain-string", out == "plain result text\n", repr(out))

# --- non-2xx already raises and is caught by the handler ----------------------
class _RaisingClient:
    def tool(self, name, args):
        raise LorekeeperError("Lorekeeper service HTTP 404: unknown route: /export")


h_raise = _make_handler(_RaisingClient(), "lorekeeper_export")
out = h_raise({})
check("handler-surfaces-raised-error",
      isinstance(out, str) and out.startswith("Lorekeeper tool failed: Lorekeeper service HTTP 404"), out)

failed = sum(1 for ok in results if not ok)
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)