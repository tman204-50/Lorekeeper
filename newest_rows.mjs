import { createClient } from "./vendor/dist/client.js";
const client = createClient({ baseUrl: "http://127.0.0.1:18777", token: require("fs").readFileSync("/root/.hermes/lorekeeper/token", "utf8").trim() });
const rows = await client.memory.list({ limit: 8 });
console.log(JSON.stringify(rows, null, 1));
