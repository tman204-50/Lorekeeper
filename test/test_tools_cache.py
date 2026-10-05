#!/usr/bin/env python3
"""Regression tests for the provider tools config/token cache (no service needed).

Asserts the check_fn/_client() contract after the disk-read caching fix:
  - repeated calls with unchanged files return the SAME client object (cached,
    no rebuild)
  - rewriting the token file invalidates the cache -> new client
  - rewriting lorekeeper.json (host/token change) invalidates -> new client
  - removing the files -> _available() goes False (fail closed, cached None)
  - changing HERMES_HOME points the cache at the new home (path is in the key)

Run:  python3 lorekeeper-tests/test_tools_cache.py
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

from provider import tools

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


os.environ.pop("LOREKEEPER_HOST", None)  # keep resolution file-driven
tmp = tempfile.mkdtemp(prefix="lk-tools-cache-")
os.environ["HERMES_HOME"] = tmp
home = Path(tmp)
(home / "lorekeeper").mkdir()


def write_files(home_dir=home, token="tok-a", host="http://127.0.0.1:18777"):
    h = Path(home_dir)
    (h / "lorekeeper.json").write_text(f'{{"host": "{host}"}}')
    (h / "lorekeeper" / "token").write_text(token)


write_files()

# 1. repeated calls return the same cached object
c1 = tools._client()
c2 = tools._client()
check("cached-same-object", c1 is not None and c1 is c2, "identical client instance")

# 2. token rewrite -> cache invalidated, new client (different size guarantees sig change)
write_files(token="tok-b-longer")
c3 = tools._client()
check("token-invalidate", c3 is not None and c3 is not c1, "rebuilt after token change")

# 3. token rewrite with SAME size (mtime-only change) -> still invalidated
time.sleep(0.01)
write_files(token="tok-b-longr")  # same length as tok-b-longer
c4 = tools._client()
check("mtime-invalidate", c4 is not None and c4 is not c3, "rebuilt on mtime change alone")

# 4. config rewrite (host change) -> invalidated
write_files(host="http://127.0.0.1:18799")
c5 = tools._client()
check("config-invalidate", c5 is not None and c5 is not c4, "rebuilt after config change")

# 5. files removed -> fail closed, cached None
(home / "lorekeeper.json").unlink()
(home / "lorekeeper" / "token").unlink()
c6 = tools._client()
check("missing-files-fail-closed", c6 is None, "_client() -> None")
check("available-false", tools._available() is False, "check_fn refuses")

# 6. missing files cached -> repeated fast-path None
c7 = tools._client()
check("none-cached", c7 is None and c7 is c6, "None result cached (no rebuild churn)")

# 7. files restored -> recovers
write_files()
c8 = tools._client()
check("recovery", c8 is not None and tools._available() is True, "recovers after restore")

# 8. HERMES_HOME switch -> path is part of the key, no stale cross-home client
tmp2 = tempfile.mkdtemp(prefix="lk-tools-cache2-")
os.environ["HERMES_HOME"] = tmp2
Path(tmp2, "lorekeeper").mkdir()
c9 = tools._client()
check("home-switch", c9 is None, "different home with no files -> None (not home1's client)")
write_files(tmp2)
c10 = tools._client()
check("home-switch-build", c10 is not None and c10 is not c8, "new home builds its own client")

# 9. concurrent calls are safe
os.environ["HERMES_HOME"] = tmp
errs = []
objs = []
lock = threading.Lock()


def worker():
    try:
        for _ in range(10):
            c = tools._client()
            with lock:
                objs.append(id(c))
    except Exception as e:  # noqa: BLE001
        errs.append(e)


threads = [threading.Thread(target=worker) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
unique = set(objs)
check("concurrent-safe", not errs and len(unique) == 1, f"40 calls -> {len(unique)} instance, {len(errs)} errors")

passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
print(f"\nPASS {passed} / FAIL {failed} / TOTAL {len(results)}")
sys.exit(1 if failed else 0)
