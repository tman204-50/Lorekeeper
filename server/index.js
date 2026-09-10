// Lorekeeper memory service — standalone HTTP wrapper around the forked
// opencode-memory-pro store. Loopback only, bearer-token auth.
//
// The service owns the MemoryStore + graph + embedder. It exposes a small
// JSON-RPC-ish HTTP API that the Hermes Python provider calls.
//
// Env:
//   LOREKEEPER_PORT       (default 18777)
//   LOREKEEPER_TOKEN      (default: random at boot; provider reads it from file)
//   LOREKEEPER_DB_PATH    (default ~/.hermes/lorekeeper/lancedb)
//   LOREKEEPER_GRAPH_PATH (default ~/.hermes/lorekeeper/graph.db)
//   LOREKEEPER_CONFIG     (optional path to a lorekeeper.json config)
//   OPENCODE_MEMORY_PRO_* (passthrough knobs the vendor config resolver reads)

import { createServer } from "node:http";
import { mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { randomBytes, randomUUID } from "node:crypto";
import { resolveMemoryConfig } from "../vendor/dist/config.js";
import { createEmbedder } from "../vendor/dist/embedder.js";
import { MemoryStore } from "../vendor/dist/store.js";
import { createGraphStore } from "../vendor/dist/graph.js";
import { initLogger, configureLogger, log } from "../vendor/dist/logger.js";
import { deriveProjectScope } from "../vendor/dist/scope.js";

const PORT = Number(process.env.LOREKEEPER_PORT ?? 18777);
const HOME = homedir();
const DB_PATH = process.env.LOREKEEPER_DB_PATH ?? join(HOME, ".hermes", "lorekeeper", "lancedb");
const GRAPH_PATH = process.env.LOREKEEPER_GRAPH_PATH ?? join(HOME, ".hermes", "lorekeeper", "graph.db");
const TOKEN = process.env.LOREKEEPER_TOKEN ?? "";

// --- token handling ---------------------------------------------------------
// If no token is set, generate one and write it to the data dir so the Hermes
// provider can read it (same pattern as mem0's config file).
const tokenPath = process.env.LOREKEEPER_TOKEN_PATH ?? join(HOME, ".hermes", "lorekeeper", "token");
let authToken = TOKEN;
if (!authToken) {
  authToken = randomBytes(32).toString("hex");
}

// --- config resolution ------------------------------------------------------
// Default: Ollama + nomic-embed-text (the fork's default). Override via env
// (OPENCODE_MEMORY_PRO_*) or a lorekeeper.json config file.
process.env.OPENCODE_MEMORY_PRO_DB_PATH ??= DB_PATH;
process.env.OPENCODE_MEMORY_PRO_GRAPH_DB_PATH ??= GRAPH_PATH;
process.env.OPENCODE_MEMORY_PRO_SCOPING ??= "global"; // single-user

const configPath = process.env.LOREKEEPER_CONFIG;
if (configPath) {
  process.env.OPENCODE_MEMORY_PRO_CONFIG_PATH = configPath;
}

// --- state ------------------------------------------------------------------
const state = {
  config: null,
  embedder: null,
  store: null,
  graph: null,
  initialized: false,
  initPromise: null,
};

async function ensureInit() {
  if (state.initialized) return;
  if (state.initPromise) return state.initPromise;
  state.initPromise = (async () => {
    try {
      const resolved = resolveMemoryConfig(undefined, process.cwd());
      state.config = resolved;
      state.embedder = createEmbedder(resolved.embedding);
      state.store = new MemoryStore(resolved.dbPath);
      if (resolved.retention) state.store.setRetentionConfig(resolved.retention);
      if (resolved.retention?.scoring) state.store.setRetentionScoringConfig(resolved.retention.scoring);
      const graph = resolved.graph?.enabled ? await createGraphStore(resolved.graph) : null;
      if (graph) {
        try { state.store.attachGraph(graph); } catch (e) { log("warn", `graph attach failed: ${e}`); }
        state.graph = graph;
      }
      const dim = await state.embedder.dim();
      await state.store.init(dim);
      if (state.store.indexState?.dimensionMismatch) {
        log("warn", "embedding dimension mismatch detected; repair needed");
      }
      state.initialized = true;
      log("info", `Lorekeeper service ready. db=${resolved.dbPath} embedder=${resolved.embedding.provider}/${resolved.embedding.model}`);
    } catch (e) {
      log("error", `init failed: ${e}`);
      throw e;
    } finally {
      state.initPromise = null;
    }
  })();
  return state.initPromise;
}

// --- HTTP plumbing ------------------------------------------------------------
function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => { chunks.push(c); size += c.length; if (size > 10 * 1024 * 1024) { reject(new Error("body too large")); req.destroy(); } });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
    req.on("error", reject);
  });
}

function unauthorized(res) {
  sendJson(res, 401, { error: "unauthorized" });
}

// --- route handlers ------------------------------------------------------------
const handlers = {
  async health() {
    return {
      ok: true,
      version: "0.1.0",
      initialized: state.initialized,
      dbPath: state.config?.dbPath ?? DB_PATH,
      embedder: state.config ? `${state.config.embedding.provider}/${state.config.embedding.model}` : "not-initialized",
    };
  },
  async init() {
    await ensureInit();
    return { ok: true, dim: state.store ? await state.embedder.dim() : null };
  },
  async remember(args) {
    await ensureInit();
    const { content, category, importance, scope } = args;
    if (!content || !content.trim()) throw new Error("content is required");
    // Match the fork's memory record schema (store.js bootstrap).
    const now = Date.now();
    const record = {
      id: randomUUID(),
      text: content.trim(),
      category: category ?? "general",
      scope: scope ?? "global",
      importance: importance ?? 0.5,
      timestamp: now,
      lastRecalled: 0,
      recallCount: 0,
      projectCount: 0,
      schemaVersion: 2,
      embeddingModel: state.config.embedding.model,
      metadataJson: "{}",
      status: "active",
    };
    // Embed the text so the vector column is populated.
    record.vector = await state.embedder.embed(record.text);
    await state.store.put(record);
    return { id: record.id };
  },
  async search(args) {
    await ensureInit();
    const { query, limit, scope } = args;
    const activeScope = scope ?? "global";
    const scopes = ["global"]; // single-user
    let queryVector = [];
    try { queryVector = await state.embedder.embed(query); } catch (e) { queryVector = []; }
    const isFallback = queryVector.length === 0;
    const results = await state.store.search({
      query,
      queryVector,
      scopes,
      limit: limit ?? 5,
      vectorWeight: isFallback ? 0 : state.config.retrieval.vectorWeight,
      bm25Weight: isFallback ? 1 : state.config.retrieval.bm25Weight,
      fuzzyWeight: state.config.retrieval.fuzzyWeight,
      fuzzyThreshold: state.config.retrieval.fuzzyThreshold,
      minScore: state.config.retrieval.minScore,
      rrfK: state.config.retrieval.rrfK,
      recencyBoost: state.config.retrieval.recencyBoost,
      recencyHalfLifeHours: state.config.retrieval.recencyHalfLifeHours,
      importanceWeight: state.config.retrieval.importanceWeight,
      feedbackWeight: state.config.retrieval.feedbackWeight,
      globalDiscountFactor: state.config.globalDiscountFactor,
    });
    const items = results.map((r) => {
      const rec = r.record ?? r;
      return {
        id: rec.id,
        text: rec.text,
        score: r.score,
        scope: rec.scope,
        category: rec.category,
        importance: rec.importance,
        timestamp: rec.timestamp,
      };
    });
    return { results: items, count: items.length };
  },
  async delete(args) {
    await ensureInit();
    const { id, force } = args;
    if (!id) throw new Error("id is required");
    const ok = force
      ? await state.store.deleteByIdForce(id, ["global"])
      : await state.store.softDeleteMemory(id, ["global"]);
    return { ok: !!ok, id };
  },
  async stats() {
    await ensureInit();
    const records = await state.store.readAllActive();
    const byScope = {};
    for (const r of records) byScope[r.scope] = (byScope[r.scope] ?? 0) + 1;
    return {
      counts: { total: records.length, byScope },
      index: state.store.getIndexHealth ? await state.store.getIndexHealth() : null,
      initialized: state.initialized,
    };
  },
  async list(args) {
    await ensureInit();
    const { scope, limit } = args;
    const records = await state.store.list(scope ?? "global", limit ?? 100);
    return { results: records.map((r) => ({ id: r.id, text: r.text, scope: r.scope, category: r.category, importance: r.importance, timestamp: r.timestamp })) };
  },
  async exportAll() {
    await ensureInit();
    const records = await state.store.exportAllRecords(["global"]);
    return { memories: records };
  },
  async import(args) {
    await ensureInit();
    const { memories, mode } = args;
    if (!Array.isArray(memories)) throw new Error("memories must be an array");
    let count = 0;
    for (const m of memories) {
      if (!m?.text) continue;
      const record = {
        id: m.id ?? randomUUID(),
        text: m.text,
        category: m.category ?? "general",
        importance: m.importance ?? 0.5,
        scope: m.scope ?? "global",
        timestamp: m.timestamp ?? Date.now(),
        lastRecalled: m.lastRecalled ?? 0,
        recallCount: m.recallCount ?? 0,
        projectCount: m.projectCount ?? 0,
        schemaVersion: 2,
        embeddingModel: state.config.embedding.model,
        metadataJson: m.metadataJson ?? "{}",
        status: m.status ?? "active",
      };
      // Re-embed on import so vectors match the current embedder.
      record.vector = await state.embedder.embed(record.text);
      await state.store.put(record);
      count++;
    }
    return { imported: count };
  },
};

// --- server -------------------------------------------------------------------
const server = createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const path = url.pathname;
  if (req.method === "GET" && path === "/health") {
    return handlers.health().then((r) => sendJson(res, 200, r)).catch((e) => sendJson(res, 500, { error: e.message }));
  }
  if (req.method !== "POST") {
    return sendJson(res, 405, { error: "method not allowed" });
  }
  // Auth: bearer token (except /health)
  const auth = req.headers?.get?.("authorization") ?? req.headers?.authorization ?? "";
  if (path !== "/health" && (!authToken || auth !== `Bearer ${authToken}`)) {
    return unauthorized(res);
  }
  const handler = handlers[path.slice(1)];
  if (!handler) {
    return sendJson(res, 404, { error: `unknown route: ${path}` });
  }
  readBody(req)
    .then((body) => JSON.parse(body || "{}"))
    .then((args) => handler(args))
    .then((result) => sendJson(res, 200, result))
    .catch((e) => sendJson(res, 500, { error: e.message }));
});

// Write token + ensure data dir before listening.
mkdirSync(join(HOME, ".hermes", "lorekeeper"), { recursive: true });
mkdirSync(DB_PATH, { recursive: true });
if (!TOKEN) {
  writeFileSync(tokenPath, authToken, "utf8");
  log("info", `token written to ${tokenPath}`);
}

server.listen(PORT, "127.0.0.1", () => {
  log("info", `Lorekeeper service listening on http://127.0.0.1:${PORT}`);
});

process.once("exit", () => {
  try { state.store?.close(); } catch {}
});

// Keep the process alive.
setInterval(() => {}, 1 << 30);