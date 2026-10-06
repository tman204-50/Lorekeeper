#!/usr/bin/env node
// Regression tests for the 0.2.9 tmp-namespace guard (F1).
//
// The service runs under systemd PrivateTmp=yes, so /tmp /var/tmp /dev/shm
// paths written by the service land in a private mount namespace invisible to
// the Hermes agent. memory_export/memory_import must reject those paths with
// an actionable error before touching the store. Two layers are verified:
//   1. the pure predicate + throw helper (server/path_guard.js)
//   2. the wiring — a scratch service answers POST /tool for lorekeeper_export
//      with a /tmp path as HTTP 500 (the guard fires before store init, so no
//      embedder is required).
//
// Run:  node test/test_v029_guard.mjs

import assert from "node:assert";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import net from "node:net";
import { isTmpNamespacePath, assertSafeFilePath } from "../server/path_guard.js";

const results = [];
function check(name, ok, detail = "") {
  results.push(ok);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}: ${detail}`);
}

// --- 1. pure predicate -------------------------------------------------------
const blocked = ["/tmp", "/tmp/x.json", "/var/tmp", "/var/tmp/a/b", "/dev/shm/x", "tmp/x", "var/tmp/x", "/dev\\shm\\y", "\\tmp\\y"];
const allowed = ["/tmpfoo", "/root/tmp/x", "/root/.hermes/workspace/backup.json", "/var/tmpX", "/dev/shmfoo", "./x.json", "relative/x.json", "/home/todd/export.json", "/tmp", null, ""];
// "/tmp" is in both lists intentionally (see below): assert it is blocked.

for (const p of blocked) {
  check(`predicate-blocks ${JSON.stringify(p)}`, isTmpNamespacePath(p) === true, `isTmpNamespacePath(${JSON.stringify(p)})`);
}
for (const p of allowed.filter((x) => x !== "/tmp")) {
  check(`predicate-allows ${JSON.stringify(p)}`, isTmpNamespacePath(p) === false, `isTmpNamespacePath(${JSON.stringify(p)})`);
}

// --- 2. throw helper ---------------------------------------------------------
let threw = null;
try { assertSafeFilePath("lorekeeper_export", { path: "/tmp/out.json" }); } catch (e) { threw = e.message; }
check("export-tmp-throws", thrownIncludes(threw, "private-tmp"), threw || "no throw");

threw = null;
try { assertSafeFilePath("lorekeeper_import", { path: "/var/tmp/in.json" }); } catch (e) { threw = e.message; }
check("import-tmp-throws", thrownIncludes(threw, "private-tmp"), threw || "no throw");

threw = null;
try { assertSafeFilePath("lorekeeper_export", { path: "/root/.hermes/workspace/ok.json" }); } catch (e) { threw = e.message; }
check("export-workspace-ok", threw === null, threw || "unexpected throw");

threw = null;
try { assertSafeFilePath("lorekeeper_search", { path: "/tmp/ignored.json" }); } catch (e) { threw = e.message; }
check("guard-ignores-other-tools", threw === null, threw || "unexpected throw on non-export tool");

threw = null;
try { assertSafeFilePath("lorekeeper_export", {}); } catch (e) { threw = e.message; }
check("guard-ignores-missing-path", threw === null, threw || "unexpected throw on missing path");

function thrownIncludes(msg, sub) {
  return typeof msg === "string" && msg.includes(sub);
}

// --- 3. wiring: scratch service rejects /tmp via /tool -----------------------
const dir = mkdtempSync(join(tmpdir(), "lk-guard-"));
const db = join(dir, "db");
const graph = join(dir, "graph.db");
const token = "guard-token";
const port = await freePort();

const child = spawn(process.execPath, ["server/index.js"], {
  cwd: new URL("..", import.meta.url).pathname,
  env: {
    ...process.env,
    LOREKEEPER_PORT: String(port),
    LOREKEEPER_TOKEN: token,
    LOREKEEPER_DB_PATH: db,
    LOREKEEPER_GRAPH_PATH: graph,
  },
  stdio: ["ignore", "ignore", "pipe"],
});

try {
  await waitForHealth(port);
  const res = await fetch(`http://127.0.0.1:${port}/tool`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ name: "lorekeeper_export", toolArgs: { path: "/tmp/invisible.json" } }),
  });
  const body = await res.json().catch(() => ({}));
  check("wiring-export-tmp-500", res.status === 500 && typeof body.error === "string" && body.error.includes("private-tmp"),
    `status=${res.status} error=${body.error ?? ""}`);
} finally {
  child.kill("SIGTERM");
  try { rmSync(dir, { recursive: true, force: true }); } catch {}
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
    srv.on("error", reject);
  });
}

async function waitForHealth(port, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/health`);
      if (r.ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error(`scratch service did not come up on port ${port}`);
}

const failed = results.filter((ok) => !ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);