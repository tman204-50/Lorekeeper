#!/usr/bin/env node
// Regression tests for F1 follow-ups (0.2.7):
//   Gap 1 — MERGE_TEXT_STASH: consolidate used to hide the absorbed row with
//   NO text copy; unique wording in a near-dup left recall permanently
//   (status "merged", recoverable only by DB surgery). The survivor's
//   metadata now carries mergedTexts: [{id, text, mergedAt}].
//   Gap 2 — DIGEST_FEEDBACK_EVENT: digest creation used to be a silent put;
//   the events stream never recorded that memories were folded away. Now a
//   capture event with outcome "digest" is emitted by summarize and the
//   retention sweep, and summarizeEvents/aggregateEvents count capture.digests.
//
// Uses a REAL MemoryStore in a temp dir (LanceDB), mock embedder.
//
// Run:  node test/test_gap12_f1.mjs

import assert from "node:assert";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { MemoryStore } from "../vendor/dist/store.js";
import { sweepExpiredMemories } from "../vendor/dist/tools/memory.js";

// parseMetadata is module-private in store.js — local copy (JSON-parse with
// non-object fallback, same semantics)
const parseMetadata = (s) => {
  try {
    const v = JSON.parse(s || "{}");
    return v && typeof v === "object" ? v : {};
  } catch {
    return {};
  }
};

const results = [];
function check(name, ok, detail = "") {
  results.push(ok);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}: ${detail}`);
}

const dir = mkdtempSync(join(tmpdir(), "lk-gap12-"));
const store = new MemoryStore(join(dir, "db"));
await store.init(384);

function vec(seed) {
  // deterministic near-orthogonal 384-dim unit vectors
  const v = Array.from({ length: 384 }, (_, i) => Math.sin(seed + i * 0.01));
  const norm = Math.sqrt(v.reduce((s, x) => s + x * x, 0));
  return v.map((x) => x / norm);
}
function mkRecord(id, text, { seed = 1, timestamp = Date.now(), lastRecalled = 0, importance = 0.5, category = "fact" } = {}) {
  return {
    id, text,
    category,
    scope: "global",
    importance,
    timestamp,
    lastRecalled,
    recallCount: 0,
    projectCount: 0,
    schemaVersion: 2,
    embeddingModel: "mock-embed",
    vector: vec(seed),
    vectorDim: 384,
    metadataJson: "{}",
    status: "active",
  };
}
const dupVec = vec(1).map((x, i) => x + (i === 0 ? 0.02 : 0)); // cosine ~0.9998 vs vec(1)

const fakeState = {
  ensureInitialized: async () => {},
  initialized: true,
  store,
  defaultScope: "global",
  config: { includeGlobalScope: true, embedding: { provider: "mock", model: "mock-embed" } },
  embedder: { embed: async (text) => vec(text.length % 7 + 2) },
};

try {
  // ---- Gap 1: merge text stash ------------------------------------------------
  const s1 = mkRecord("gap1-survivor", "SURVIVOR TEXT: lorekeeper deploys via install.sh plugin-only mode", { seed: 1 });
  const a1 = mkRecord("gap1-absorbed", "ABSORBED TEXT: lorekeeper deploys via install.sh with plugin-only flag and systemd", { seed: 1, timestamp: s1.timestamp + 10, vector: undefined });
  a1.vector = dupVec;
  await store.put(s1);
  await store.put(a1);

  const merge = await store.consolidateDuplicates("global", 0.95, 50);
  check("gap1-merge-happened", merge.mergedPairs === 1, JSON.stringify(merge));

  // survivor is the NEWER row; read it back incl. merged rows
  const rows = await store.readByScopesIncludingMerged(["global"]);
  const survivor = rows.find((r) => r.status !== "merged" && r.status !== "disabled");
  const meta = parseMetadata(survivor?.metadataJson ?? "{}");
  const stash = meta.mergedTexts ?? [];
  check("gap1-stash-present", Array.isArray(stash) && stash.length === 1,
    `mergedTexts=${JSON.stringify(stash).slice(0, 120)}`);
  check("gap1-stash-content", stash.length === 1 && stash[0].id === (survivor.id === s1.id ? a1.id : s1.id)
    && stash[0].text.startsWith("ABSORBED TEXT") === (survivor.id === s1.id),
    `stash[0].id=${stash[0]?.id} survivor=${survivor?.id}`);
  check("gap1-stash-bounded", typeof stash[0]?.mergedAt === "number" && stash[0].text.length <= 800,
    `mergedAt=${stash[0]?.mergedAt} len=${stash[0]?.text?.length}`);

  // ---- Gap 2: digest events from the retention sweep --------------------------
  // 3 old, never-recalled memories in category fact -> 1 digest group
  const now = Date.now();
  for (let i = 0; i < 3; i++) {
    await store.put(mkRecord(`gap2-old-${i}`, `retention sweep candidate number ${i} about webull token refresh`,
      { seed: 2 + i, timestamp: now - 200 * 24 * 3600 * 1000, lastRecalled: now - 120 * 24 * 3600 * 1000, importance: 0.4 }));
  }
  const sweep = await sweepExpiredMemories(fakeState, {
    scope: "global",
    unusedDays: 60,
    minAgeDays: 180,
    minGroupSize: 2,
    targetChars: 500,
    minImportance: 0.3,
  });
  check("gap2-sweep-created-digest", sweep.digestsCreated >= 1, JSON.stringify({ digestsCreated: sweep.digestsCreated, digested: sweep.digested, eligible: sweep.eligible }));

  const events = await store.readEventsByScopes(["global"]);
  const digestEvents = events.filter((e) => e.type === "capture" && e.outcome === "digest");
  check("gap2-digest-event-emitted", digestEvents.length === sweep.digestsCreated,
    `digest events=${digestEvents.length}, digestsCreated=${sweep.digestsCreated}`);
  const evMeta = digestEvents.length ? JSON.parse(digestEvents[0].metadataJson ?? "{}") : {};
  check("gap2-event-metadata", digestEvents.length === 1 && evMeta.source === "digest" && evMeta.group === "fact"
    && evMeta.digestMode === "extractive" && typeof evMeta.absorbed === "number",
    JSON.stringify(evMeta));

  // summarizeEvents counts them under capture.digests
  const summary = await store.summarizeEvents("global", false);
  check("gap2-effectiveness-counts-digests", summary.capture.digests === digestEvents.length,
    `capture.digests=${summary.capture.digests}`);

  // aggregateEvents (KPI weekly path) mirrors the counter
  const agg = store.aggregateEvents("global", events, rows);
  check("gap2-kpi-counts-digests", agg.capture.digests === digestEvents.length,
    `aggregate capture.digests=${agg.capture.digests}`);
} finally {
  try { rmSync(dir, { recursive: true, force: true }); } catch {}
}

const failed = results.filter((ok) => !ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
