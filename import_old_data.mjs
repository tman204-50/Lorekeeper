// Phase 5: extract gold memories from the old OpenClaw store and import
// them into Lorekeeper via the service /import endpoint.
//
// Gold = categories {memory, fact, preference, learning, profile}, minus
// obvious test junk. The /import endpoint re-embeds with the current
// embedder (nomic-embed-text, 768-dim) and stores under global scope.

import { connect } from "@lancedb/lancedb";

const OLD_PATH = "/root/.openclaw/memory/lancedb";
const LOREKEEPER_URL = "http://127.0.0.1:18777";
const { readFileSync } = await import("node:fs");
const TOKEN = readFileSync("/root/.hermes/lorekeeper/token", "utf8").trim();

const GOLD = new Set(["memory", "fact", "preference", "learning", "profile"]);
const TEST_RE = /remember this|test memory|for testing|lancedb debugging|dummy|placeholder|be sure to remember this|my favorite color is blue and my cat/;

const con = await connect(OLD_PATH);
const table = await con.openTable("memories");
const rows = await table.query().select(["id", "text", "category", "createdAt"]).toArray();

const gold = [];
let droppedTest = 0;
for (const r of rows) {
  if (!GOLD.has(r.category)) continue;
  const text = String(r.text ?? "").trim();
  if (!text || TEST_RE.test(text.toLowerCase())) {
    droppedTest++;
    continue;
  }
  gold.push({
    id: r.id,
    text,
    category: r.category === "memory" ? "general" : r.category, // old "memory" -> "general"
    importance: r.category === "profile" ? 0.9 : r.category === "preference" ? 0.8 : 0.7,
    timestamp: r.createdAt && r.createdAt > 0 ? r.createdAt : Date.now(),
  });
}

console.log(`extracted ${gold.length} gold memories (dropped ${droppedTest} test-junk)`);

// Import in batches of 200 (avoid one giant request).
const BATCH = 200;
let imported = 0;
for (let i = 0; i < gold.length; i += BATCH) {
  const batch = gold.slice(i, i + BATCH);
  const resp = await fetch(`${LOREKEEPER_URL}/import`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${TOKEN}`,
    },
    body: JSON.stringify({ memories: batch }),
  });
  if (!resp.ok) {
    const err = await resp.text();
    console.error(`batch ${i / BATCH} failed: HTTP ${resp.status}: ${err.slice(0, 300)}`);
    process.exit(1);
  }
  const data = await resp.json();
  imported += data.imported ?? 0;
  console.log(`  batch ${i / BATCH + 1}: imported ${data.imported}`);
}

console.log(`\nDONE: ${imported}/${gold.length} imported`);
await con.close();