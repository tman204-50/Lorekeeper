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
import { mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { randomBytes, randomUUID } from "node:crypto";
import { resolveMemoryConfig } from "../vendor/dist/config.js";
import { createEmbedder } from "../vendor/dist/embedder.js";
import { MemoryStore } from "../vendor/dist/store.js";
import { createGraphStore } from "../vendor/dist/graph.js";
import { initLogger, configureLogger, log } from "../vendor/dist/logger.js";
import { deriveProjectScope } from "../vendor/dist/scope.js";
import { extractCaptureCandidate } from "../vendor/dist/extract.js";
import { requestLLMCapture } from "../vendor/dist/llm.js";
import { generateId } from "../vendor/dist/utils.js";
import { createMemoryTools } from "../vendor/dist/tools/memory.js";
import { createFeedbackTools } from "../vendor/dist/tools/feedback.js";
import { createEpisodicTools } from "../vendor/dist/tools/episodic.js";
import { zodToJsonSchema } from "zod-to-json-schema";
import { LLMSessionClient } from "./llm_shim.js";

const SCHEMA_VERSION = 1;

const PORT = Number(process.env.LOREKEEPER_PORT ?? 18777);
const HOME = homedir();
const DB_PATH = process.env.LOREKEEPER_DB_PATH ?? join(HOME, ".hermes", "lorekeeper", "lancedb");
const GRAPH_PATH = process.env.LOREKEEPER_GRAPH_PATH ?? join(HOME, ".hermes", "lorekeeper", "graph.db");
const TOKEN = process.env.LOREKEEPER_TOKEN ?? "";

// Load OPENROUTER_API_KEY from $HERMES_HOME/.env if not already set (the
// service runs as its own process; Hermes' .env isn't auto-exported).
function loadEnvFile(path) {
  try {
    const lines = readFileSync(path, "utf8").split("\n");
    for (const line of lines) {
      const m = line.match(/^\s*export\s+([A-Z0-9_]+)=(.*)$/) || line.match(/^\s*([A-Z0-9_]+)=(.*)$/);
      if (m && !process.env[m[1]]) {
        let val = m[2].trim();
        if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
          val = val.slice(1, -1);
        }
        process.env[m[1]] = val;
      }
    }
  } catch (e) {
    // No .env — fine, LLM features just stay off.
  }
}
loadEnvFile(join(HOME, ".hermes", ".env"));

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
// Per-turn capture (vs the fork's whole-session buffer): a single turn used
// to be 40-80 chars, so the fork's 80-char floor was lowered to 40. But short
// noisy turns (questions, requests, one-liners) slip through at 40 chars; the
// fork's own default of 80 filters more of them while still passing most
// substantive turns. Heuristic FP reduction (2026-09-10): back to 80.
process.env.OPENCODE_MEMORY_PRO_MIN_CAPTURE_CHARS ??= "80";
// LLM capture/digests via the shim (OpenRouter + minimax/minimax-m3).
// OPENROUTER_API_KEY is read from the environment (Hermes .env or export).
process.env.OPENCODE_MEMORY_PRO_CAPTURE_MODE ??= "llm";
process.env.OPENCODE_MEMORY_PRO_CAPTURE_LLM_PROVIDER ??= "openrouter";
process.env.OPENCODE_MEMORY_PRO_CAPTURE_LLM_MODEL ??= "minimax/minimax-m3";
// Recency boost: soften the 72h default half-life to 7 days so fresh captures
// don't bury the imported history (old memories decay toward the 0.5 floor
// either way, but days-old memories keep a fairer share of the boost).
process.env.OPENCODE_MEMORY_PRO_RECENCY_HALF_LIFE_HOURS ??= "168";
// Importance weight: 0.4 -> 1.0 so high-importance memories (profile 0.9,
// preferences 0.8) outrank the churn despite the recency floor. A profile
// memory now gets ~1.9x from importance vs 1.36x before.
process.env.OPENCODE_MEMORY_PRO_IMPORTANCE_WEIGHT ??= "1.0";

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
  // Fields the fork's tools read/write (vendor/dist/tools/*.js):
  consolidationInProgress: new Map(),
  lastRecall: null,
  // LLM shim: implements the SDK client surface (session.create/prompt/delete)
  // over OpenRouter so the fork's LLM capture/digest paths run unchanged.
  client: null,
  defaultScope: "global",
  ensureInitialized: async () => { await ensureInit(); },
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
      // LLM shim: OpenRouter-compatible client so capture.mode="llm" and
      // LLM digests work. Reads OPENROUTER_API_KEY from the environment.
      const apiKey = process.env.OPENROUTER_API_KEY;
      if (apiKey) {
        state.client = new LLMSessionClient({
          apiKey,
          model: resolved.capture?.llm?.model ?? "minimax/minimax-m3",
        });
        log("info", `LLM shim enabled: ${resolved.capture?.llm?.provider}/${resolved.capture?.llm?.model} (capture.mode=${resolved.capture?.mode})`);
      } else {
        log("warn", "OPENROUTER_API_KEY not set — LLM capture/digests disabled (heuristics only)");
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

// --- capture pipeline helpers (mirror vendor/dist/index.js) ---------------
async function storeCapturedMemory(opts) {
  let vector = [];
  try {
    vector = await state.embedder.embed(opts.text);
  } catch (error) {
    log("warn", `embedding unavailable during auto-capture: ${error}`);
    return { id: null, skipReason: "embedding-unavailable" };
  }
  if (vector.length === 0) {
    log("warn", "auto-capture skipped because embedding vector is empty");
    return { id: null, skipReason: "empty-embedding" };
  }
  let isPotentialDuplicate = false;
  let duplicateOf = null;
  if (state.config.dedup.enabled) {
    const similar = await state.store.findSimilarVectors(vector, opts.scope, 1);
    if (similar.length > 0 && similar[0].score >= state.config.dedup.writeThreshold) {
      isPotentialDuplicate = true;
      duplicateOf = similar[0].id;
    }
  }
  const memoryId = generateId();
  const now = Date.now();
  const graphEntities = state.config.graph?.enabled && state.graph?.enabled
    ? state.graph.extract(opts.text)
    : [];
  await state.store.put({
    id: memoryId,
    text: opts.text,
    vector,
    category: opts.category,
    scope: opts.scope,
    importance: opts.importance,
    timestamp: now,
    lastRecalled: 0,
    recallCount: 0,
    projectCount: 0,
    schemaVersion: SCHEMA_VERSION,
    embeddingModel: state.config.embedding.model,
    vectorDim: vector.length,
    metadataJson: JSON.stringify({
      source: opts.source ?? "auto-capture",
      sessionID: opts.sessionID,
      isPotentialDuplicate,
      duplicateOf,
      graphEntities: graphEntities.map((e) => e.name),
    }),
    citationSource: opts.source ?? "auto-capture",
    citationTimestamp: now,
    citationStatus: "pending",
  });
  if (state.config.graph?.enabled && state.graph?.enabled) {
    try {
      state.graph.indexMemory(memoryId, opts.text, now);
    } catch (error) {
      log("warn", `graph indexMemory failed: ${error}`);
    }
  }
  return { id: memoryId, skipReason: null };
}

async function recordCaptureEvent(input) {
  if (!state.initialized) return;
  try {
    await state.store.putEvent({
      id: generateId(),
      type: "capture",
      scope: input.scope,
      sessionID: input.sessionID ?? "",
      timestamp: Date.now(),
      memoryId: input.memoryId ?? "",
      text: input.text?.slice(0, 4000) ?? "",
      outcome: input.outcome ?? "",
      skipReason: input.skipReason ?? "",
      metadataJson: "{}",
    });
  } catch (error) {
    log("warn", `capture event write failed: ${error}`);
  }
}

// --- fork tool registry ------------------------------------------------------
// The fork's tools (vendor/dist/tools/*.js) are written against `state` and
// expose {description, args: {name: zodSchema}, execute}. We register them all
// under the lorekeeper_ prefix and dispatch via a generic /tool endpoint —
// no per-tool RPC needed.
let toolRegistry = null;

function getToolRegistry() {
  if (toolRegistry) return toolRegistry;
  const tools = {
    ...createMemoryTools(state),
    ...createFeedbackTools(state),
    ...createEpisodicTools(state),
  };
  toolRegistry = new Map();
  for (const [name, def] of Object.entries(tools)) {
    // Strip the fork's "memory_" prefix: lorekeeper_memory_search ->
    // lorekeeper_search (the fork's memory_* names are the full surface; our
    // earlier custom handlers get dropped in favor of these).
    const short = name.startsWith("memory_") ? name.slice("memory_".length) : name;
    toolRegistry.set(`lorekeeper_${short}`, { name, def });
  }
  return toolRegistry;
}

function zodToOpenAISchema(zodSchema) {
  // zod-to-json-schema handles optional/default/enum/array shapes; the
  // OpenAI function-calling contract wants a plain JSON schema object.
  const jsonSchema = zodToJsonSchema(zodSchema, { target: "jsonSchema7" });
  // Drop $schema/$defs noise; keep the core type/properties.
  delete jsonSchema.$schema;
  return jsonSchema;
}

function toolSchemas() {
  const registry = getToolRegistry();
  const out = [];
  for (const [lorekeeperName, { def }] of registry) {
    const properties = {};
    const required = [];
    for (const [argName, zodSchema] of Object.entries(def.args)) {
      properties[argName] = zodToOpenAISchema(zodSchema);
      if (!zodSchema.isOptional()) required.push(argName);
    }
    out.push({
      name: lorekeeperName,
      description: def.description,
      parameters: { type: "object", properties, required },
    });
  }
  return out;
}

async function runTool(name, args) {
  const registry = getToolRegistry();
  const entry = registry.get(name);
  if (!entry) throw new Error(`unknown tool: ${name}`);
  await ensureInit();
  const context = { directory: process.cwd(), worktree: process.cwd(), sessionID: args.sessionID ?? `service-${Date.now()}` };
  return await entry.def.execute(args ?? {}, context);
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
  async capture(args) {
    await ensureInit();
    const { sessionID, text, scope } = args;
    if (!text || !text.trim()) return { stored: false, skipReason: "empty-text" };
    const activeScope = scope ?? "global";
    const combined = text.trim();

    // LLM_CAPTURE: when capture.mode="llm" and the shim is available, run
    // structured extraction first; fall back to heuristics on any failure
    // (mirror vendor/dist/index.js _flushAutoCaptureGuarded).
    let candidates = null;
    if (state.config.capture?.mode === "llm" && state.client) {
      try {
        candidates = await requestLLMCapture(state.client, state.config.capture.llm, combined, sessionID ?? "");
      } catch (error) {
        log("warn", `[capture] llm extraction failed: ${error}`);
        candidates = null;
      }
      if (candidates && candidates.length > 0) {
        let storedCount = 0;
        let firstId = null;
        for (const cand of candidates) {
          const result = await storeCapturedMemory({
            sessionID: sessionID ?? "",
            scope: activeScope,
            text: cand.content,
            category: cand.type,
            importance: cand.importance,
            source: "llm-capture",
          });
          if (result.id) {
            storedCount += 1;
            if (firstId === null) firstId = result.id;
          }
        }
        await recordCaptureEvent({
          sessionID: sessionID ?? "",
          scope: activeScope,
          outcome: storedCount > 0 ? "stored" : "skipped",
          skipReason: storedCount > 0 ? undefined : "llm-no-storable",
          memoryId: firstId,
          text: combined,
        });
        if (storedCount > 0) {
          await state.store.pruneScope(activeScope, state.config.maxEntriesPerScope);
        }
        return {
          stored: storedCount > 0,
          id: firstId,
          count: storedCount,
          source: "llm-capture",
        };
      }
      if (candidates !== null) {
        // LLM ran fine but deliberately returned [] — a real verdict, not a
        // failure. Record and skip (mirror LLM_EMPTY_VERDICT).
        await recordCaptureEvent({
          sessionID: sessionID ?? "",
          scope: activeScope,
          outcome: "skipped",
          skipReason: "llm-empty-result",
          text: combined,
        });
        return { stored: false, skipReason: "llm-empty-result" };
      }
      await recordCaptureEvent({
        sessionID: sessionID ?? "",
        scope: activeScope,
        outcome: "llm-fallback",
        skipReason: "llm-unavailable",
        text: combined,
      });
    }

    // Heuristics fallback (offline path).
    const result = extractCaptureCandidate(combined, state.config.minCaptureChars);
    if (!result.candidate) {
      await recordCaptureEvent({
        sessionID: sessionID ?? "",
        scope: activeScope,
        outcome: "skipped",
        skipReason: result.skipReason,
        text: combined,
      });
      return { stored: false, skipReason: result.skipReason };
    }
    const stored = await storeCapturedMemory({
      sessionID: sessionID ?? "",
      scope: activeScope,
      text: result.candidate.text,
      category: result.candidate.category,
      importance: result.candidate.importance,
      source: "auto-capture",
    });
    if (!stored.id) {
      await recordCaptureEvent({
        sessionID: sessionID ?? "",
        scope: activeScope,
        outcome: "skipped",
        skipReason: stored.skipReason,
        text: combined,
      });
      return { stored: false, skipReason: stored.skipReason };
    }
    await recordCaptureEvent({
      sessionID: sessionID ?? "",
      scope: activeScope,
      outcome: "stored",
      memoryId: stored.id,
      text: result.candidate.text,
    });
    await state.store.pruneScope(activeScope, state.config.maxEntriesPerScope);
    return {
      stored: true,
      id: stored.id,
      category: result.candidate.category,
      importance: result.candidate.importance,
      text: result.candidate.text,
    };
  },
  async tools() {
    return { tools: toolSchemas() };
  },
  async tool(args) {
    const { name, toolArgs } = args;
    if (!name) throw new Error("name is required");
    const result = await runTool(name, toolArgs ?? {});
    // Normalize: tools return strings (formatted text) or objects/arrays.
    return typeof result === "string" ? { result } : { result };
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