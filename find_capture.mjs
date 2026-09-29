// Find the captured ERROR-20260910-001 text in the store.
import { connect } from "@lancedb/lancedb";

const con = await connect("/root/.hermes/lorekeeper/lancedb");
const table = await con.openTable("memories");
const rows = await table.query().select(["id", "text", "category", "timestamp"]).toArray();

const hits = rows.filter((r) => /store schema|createdAt/.test(String(r.text)));
console.log("rows matching 'store schema'/'createdAt':", hits.length);
for (const h of hits) {
  console.log(`\n--- id: ${h.id} | cat: ${h.category} | ts: ${new Date(h.timestamp).toISOString()}`);
  console.log(String(h.text).slice(0, 300));
}

// also: how many rows were captured TODAY (auto-capture artifacts)?
const today = rows.filter((r) => r.timestamp > new Date("2026-09-10").getTime());
console.log(`\ntotal rows captured today: ${today.length}`);
for (const t of today.slice(0, 10)) {
  console.log(`  [${t.category}] ${String(t.text).slice(0, 80).replace(/\n/g, " ⏎ ")}`);
}
await con.close();