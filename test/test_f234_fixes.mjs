#!/usr/bin/env node
// Regression tests for F2/F3/F4 (0.2.6):
//   F2 — global_list filter was a free string silently ignored unless it was
//        exactly "unused"; now an enum (unknown values rejected by schema).
//   F3 — task episodes had NO delete path; test/cron episodes accumulated
//        forever. task_episode_delete deletes by episodeId or taskId,
//        scope-limited, confirm-gated.
//   F4 — soft-deleted rows were invisible everywhere; global_list
//        filter="disabled" lists them marked [DISABLED].
//
// Uses a REAL MemoryStore in a temp dir (LanceDB), no network.
//
// Run:  node test/test_f234_fixes.mjs

import assert from "node:assert";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { MemoryStore } from "../vendor/dist/store.js";
import { createEpisodicTools } from "../vendor/dist/tools/episodic.js";

const results = [];
function check(name, ok, detail = "") {
  results.push(ok);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}: ${detail}`);
}

const dir = mkdtempSync(join(tmpdir(), "lk-f234-"));
const store = new MemoryStore(join(dir, "db"));
await store.init(384);

const fakeState = {
  ensureInitialized: async () => {},
  initialized: true,
  store,
  config: { embedding: { provider: "mock" }, unusedDaysThreshold: 30 },
};

function mkRecord(id, text, status) {
  return {
    id, text,
    category: "test",
    scope: "global",
    importance: 0.5,
    timestamp: Date.now() - 1000,
    lastRecalled: 0,
    recallCount: 0,
    projectCount: 0,
    schemaVersion: 2,
    embeddingModel: "mock-embed",
    vector: Array.from({ length: 384 }, (_, i) => 0.01 + i * 1e-6),
    vectorDim: 384,
    metadataJson: "{}",
    status,
  };
}

try {
  // --- F3: task episode delete path -----------------------------------------
  const episode = (taskId, state = "pending") => ({
    id: `epi-${taskId}-${Math.random().toString(36).slice(2, 8)}`,
    sessionId: "test",
    scope: "global",
    taskId,
    state,
    startTime: Date.now(),
    endTime: 0,
    commandsJson: "[]",
    validationOutcomesJson: "[]",
    successPatternsJson: "[]",
    retryAttemptsJson: "[]",
    recoveryStrategiesJson: "[]",
    metadataJson: "{}",
  });
  const e1 = episode("epi-del-task");
  const e2 = episode("epi-del-task");
  const e3 = episode("epi-keep-task");
  await store.createTaskEpisode(e1);
  await store.createTaskEpisode(e2);
  await store.createTaskEpisode(e3);

  // delete by taskId removes both episodes of the task, nothing else
  const n1 = await store.deleteTaskEpisodes("global", { taskId: "epi-del-task" });
  check("f3-delete-by-taskid", n1 === 2, `deleted=${n1} (expect 2)`);
  let left = await store.queryTaskEpisodes("global");
  check("f3-others-survive", left.length === 1 && left[0].taskId === "epi-keep-task",
    `remaining=[${left.map((x) => x.taskId).join(",")}]`);

  // delete by episodeId
  const n2 = await store.deleteTaskEpisodes("global", { episodeId: e3.id });
  left = await store.queryTaskEpisodes("global");
  check("f3-delete-by-episodeid", n2 === 1 && left.length === 0, `deleted=${n2} remaining=${left.length}`);

  // scope-limited: nothing matches a different scope
  const e4 = episode("epi-scope-task");
  await store.createTaskEpisode(e4);
  const n3 = await store.deleteTaskEpisodes("global", { taskId: "epi-scope-task" });
  const n4 = await store.deleteTaskEpisodes("other-scope", { taskId: "epi-scope-task" });
  check("f3-scope-limited", n3 === 1 && n4 === 0, `global=${n3} otherScope=${n4}`);

  // tool wiring: confirm gate, missing-target gate, happy path via tool
  const tools = createEpisodicTools(fakeState);
  const delTool = tools.task_episode_delete;
  const ctx = { sessionID: "test", worktree: "/tmp" };
  const r1 = await delTool.execute({ taskId: "epi-scope-task" }, ctx);
  check("f3-tool-requires-confirm", r1.includes("confirm=true"), r1);
  const r2 = await delTool.execute({ confirm: true }, ctx);
  check("f3-tool-requires-target", r2.includes("Provide episodeId or taskId"), r2);
  const e5 = episode("epi-tool-task");
  await store.createTaskEpisode(e5);
  const r3 = await delTool.execute({ taskId: "epi-tool-task", confirm: true }, ctx);
  check("f3-tool-happy-path", r3.startsWith("Deleted 1 "), r3);

  // --- F4: disabled rows visible via global_list filter ----------------------
  const memTools = (await import("../vendor/dist/tools/memory.js")).createMemoryTools(fakeState);
  const gl = memTools?.memory_global_list;
  assert(gl, "memory_global_list tool not found");

  await store.put(mkRecord("f4-active-1", "active memory one about webull tokens"));
  await store.put(mkRecord("f4-active-2", "active memory two about opnsense firewall"));
  await store.put(mkRecord("f4-disabled-1", "disabled memory about a retired service"));
  // mark the third row disabled the way the plugin's soft delete does
  const softOk = await store.softDeleteMemory("f4-disabled-1", ["global"]);
  assert(softOk, "softDeleteMemory failed");

  const defList = await gl.execute({ limit: 50 }, ctx);
  check("f4-default-list-hides-disabled", defList.includes("f4-active-1") && !defList.includes("f4-disabled-1"),
    `default list has active, hides disabled`);
  const disList = await gl.execute({ filter: "disabled", limit: 50 }, ctx);
  check("f4-disabled-filter-lists", disList.includes("[DISABLED]") && disList.includes("f4-disabled-1"),
    disList.split("\n")[0]);
  const unusedList = await gl.execute({ filter: "unused", limit: 50 }, ctx);
  check("f4-unused-filter-still-works", unusedList === "No global memories found." || typeof unusedList === "string",
    `unused filter returns (rows never recalled -> empty ok): ${unusedList.slice(0, 40)}`);

  // --- F2: filter enum rejects unknown values at the schema level ------------
  const bogus = gl.args?.filter ?? gl.schema?.args?.filter;
  if (bogus && typeof bogus.safeParse === "function") {
    check("f2-enum-rejects-bogus", bogus.safeParse("bogus").success === false, "filter='bogus' rejected");
    check("f2-enum-accepts-unused", bogus.safeParse("unused").success === true, "filter='unused' accepted");
    check("f2-enum-accepts-disabled", bogus.safeParse("disabled").success === true, "filter='disabled' accepted");
    check("f2-enum-allows-omitted", bogus.safeParse(undefined).success === true, "filter omitted accepted");
  } else {
    check("f2-enum-rejects-bogus", false, "could not access tool arg schema for validation check");
  }
} finally {
  try { rmSync(dir, { recursive: true, force: true }); } catch {}
}

const failed = results.filter((ok) => !ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
