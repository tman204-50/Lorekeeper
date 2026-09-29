// Check the capture events table for recent events.
import { connect } from "@lancedb/lancedb";

const con = await connect("/root/.hermes/lorekeeper/lancedb");
const tables = await con.tableNames();
console.log("tables:", tables);
for (const t of tables) {
  const table = await con.openTable(t);
  const schema = await table.schema();
  console.log(`\n=== ${t} ===`);
  console.log("fields:", schema.fields.map((f) => f.name).join(","));
  const count = await table.countRows();
  console.log("rows:", count);
  if (t.includes("event")) {
    const rows = await table.query().select(["id", "type", "outcome", "skipReason", "timestamp", "text"]).toArray();
    // recent events
    const sorted = rows.sort((a, b) => (b.timestamp ?? 0) - (a.timestamp ?? 0));
    for (const r of sorted.slice(0, 8)) {
      console.log(`  [${new Date(r.timestamp ?? 0).toISOString()}] type=${r.type} outcome=${r.outcome} skip=${r.skipReason}`);
      if (r.text) console.log(`      text: ${String(r.text).slice(0, 120).replace(/\n/g, " ⏎ ")}`);
    }
  }
}
await con.close();