#!/usr/bin/env python3
"""Regression tests for the shared client + tool-schema cache (no service needed).

Asserts the provider/toolset sharing contract:
  - _shared.get_client() is sig-cached and single-flight (same object; invalidates
    on file change; env-token parity with the provider's precedence)
  - _shared.get_tool_schemas() fetches once per client identity (provider and
    toolset share the fetch)
  - failed fetches are negative-cached briefly, then retried
  - provider.get_tool_schemas() routes through the cache
  - provider.shutdown() drops its reference WITHOUT closing the shared
    connection (the toolset keeps using it)

Run:  python3 test/test_shared.py
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HERMES_CORE = "/usr/local/lib/hermes-agent"
LOREKEEPER = "/root/.hermes/workspace/Lorekeeper"

sys.path.insert(0, HERMES_CORE)
import hermes_bootstrap
sys.path.insert(0, LOREKEEPER)

os.environ.pop("LOREKEEPER_HOST", None)
os.environ.pop("LOREKEEPER_TOKEN", None)

from provider import _shared
from provider._client import LorekeeperClient
import provider as provider_mod  # provider/__init__.py

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


tmp = tempfile.mkdtemp(prefix="lk-shared-")
os.environ["HERMES_HOME"] = tmp
Path(tmp, "lorekeeper").mkdir()


class CountingClient:
    """Fake client: counts .tools() calls, optional failure injection."""

    def __init__(self, fail_first=0):
        self.tools_calls = 0
        self._fail_first = fail_first
        self.closed = False

    def tools(self):
        self.tools_calls += 1
        if self.tools_calls <= self._fail_first:
            raise RuntimeError("injected failure")
        return {"tools": [{"name": f"lorekeeper_t{i}", "description": f"tool {i}"} for i in range(34)]}

    def close(self):
        self.closed = True


def write_files(token="tok-a", host="http://127.0.0.1:18777", home_dir=tmp):
    h = Path(home_dir)
    (h / "lorekeeper.json").write_text(f'{{"host": "{host}"}}')
    (h / "lorekeeper" / "token").write_text(token)


# --- 1. shared client: cached identity, invalidated on change -------------------
write_files()
c1 = _shared.get_client()
c2 = _shared.get_client()
check("client-cached", c1 is not None and c1 is c2 and isinstance(c1, LorekeeperClient), "same singleton instance")
write_files(token="tok-b-longer")
c3 = _shared.get_client()
check("client-invalidated", c3 is not None and c3 is not c1, "rebuilt after token change")

# --- 2. env-token parity (provider precedence: cfg > env > file) -----------------
write_files(token="tok-file")
os.environ["LOREKEEPER_TOKEN"] = "tok-env"
cenv = _shared.get_client()
check("env-token-precedence", cenv is not None and cenv._token == "tok-env", f"token={cenv._token}")
os.environ.pop("LOREKEEPER_TOKEN", None)
cfile = _shared.get_client()
check("file-token-fallback", cfile._token == "tok-file", f"token={cfile._token}")

# --- 3. schema cache: one fetch per client identity ------------------------------
fake = CountingClient()
s1 = _shared.get_tool_schemas(fake)
s2 = _shared.get_tool_schemas(fake)
check("schema-fetched-once", fake.tools_calls == 1 and len(s1) == 34 and s2 is s1, f"{fake.tools_calls} fetch for 2 calls")

# different client identity -> refetch
fake2 = CountingClient()
_shared.get_tool_schemas(fake2)
check("schema-per-client", fake2.tools_calls == 1, "new client refetches")

# --- 4. negative cache: failure cached briefly, retried after TTL ----------------
_shared._schema_cache.clear()
failc = CountingClient(fail_first=1)
s = _shared.get_tool_schemas(failc)
check("failure-empty", s == [], "failed fetch returns []")
_shared.get_tool_schemas(failc)  # immediate retry -> negative cache hit
check("negative-cached", failc.tools_calls == 1, f"no refetch within TTL ({failc.tools_calls} fetches)")
_shared._NEGATIVE_TTL = 0.2
time.sleep(0.3)
s = _shared.get_tool_schemas(failc)  # TTL expired -> retry succeeds (only 1st call fails)
check("negative-expires", len(s) == 34, f"retried after TTL, {len(s)} tools ({failc.tools_calls} fetches)")
_shared._NEGATIVE_TTL = 30.0
_shared._schema_cache.clear()

# --- 5. provider.get_tool_schemas routes through the cache ------------------------
p = provider_mod.LorekeeperMemoryProvider()
pfake = CountingClient()
p._client = pfake
a = p.get_tool_schemas()
b = p.get_tool_schemas()
check("provider-uses-cache", pfake.tools_calls == 1 and len(a) == 34, f"2 calls -> {pfake.tools_calls} fetch")

# --- 6. provider shutdown does NOT close the shared client ------------------------
p2 = provider_mod.LorekeeperMemoryProvider()
p2._client = CountingClient()
held = p2._client
p2.shutdown()
check("shutdown-no-close", p2._client is None and not held.closed, "reference dropped, connection left open")

# --- 7. provider._ensure_client resolves via shared (real files) ------------------
p3 = provider_mod.LorekeeperMemoryProvider()
p3._ensure_client()
check("ensure-shared-client", p3._client is cfile, "same shared singleton as get_client()")

# --- 8. concurrent single-flight on cold schema cache ------------------------------
_shared._schema_cache.clear()
fc = CountingClient()
outs = []
lock = threading.Lock()


def worker():
    r = _shared.get_tool_schemas(fc)
    with lock:
        outs.append(len(r))


threads = [threading.Thread(target=worker) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("schema-single-flight", fc.tools_calls == 1 and outs.count(34) == 4, f"4 threads -> {fc.tools_calls} fetch")

passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
print(f"\nPASS {passed} / FAIL {failed} / TOTAL {len(results)}")
sys.exit(1 if failed else 0)
