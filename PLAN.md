# Lorekeeper — Port Plan (Option A: Service Wrapper)

Self-contained fork of `opencode-memory-pro` v1.6.2. The Node store runs as a
standalone localhost HTTP service; a thin Hermes Python plugin talks to it.

## Architecture

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  Hermes (Python)            │        │  Lorekeeper service (Node)   │
│                             │        │                              │
│  lorekeeper/ provider       │  HTTP  │  server/index.js             │
│  (MemoryProvider impl)      │ ─────► │  wraps MemoryStore + graph   │
│                             │        │  + embedder + tools          │
│  tools: memory_search,      │        │                              │
│  memory_remember, ...       │        │  data: ~/.hermes/lorekeeper/ │
└─────────────────────────────┘        └──────────────────────────────┘
```

Same host, loopback only. The Node service owns LanceDB/sqlite/embedding;
the Python provider is a thin RPC client + tool registry.

## Why Option A

- The store layer (~80% of the value) is already OpenCode-agnostic.
- Keeps the battle-tested Node code as-is; no risky Python port of
  LanceDB/BM25/fuzzy/RRF.
- Hermes memory provider interface is proven (mem0 reference).

## Repo layout

```
Lorekeeper/
├── package.json          # Node service deps (@lancedb/lancedb, fuse.js, zod...)
├── server/
│   └── index.js          # HTTP service: init store, route RPC
├── provider/             # Hermes memory provider (Python)
│   ├── plugin.yaml       # name, version, pip deps
│   ├── __init__.py       # MemoryProvider impl + tools
│   └── _client.py        # HTTP client to server
├── vendor/               # forked dist/ from opencode-memory-pro 1.6.2
│   └── (store.js, embedder.js, graph.js, tools/, ...)
├── test/
├── README.md
└── LICENSE (MIT)
```

`vendor/` is the fork of the 1.6.2 dist — self-contained, so the repo builds
standalone. Attribution in README.

## Service API (v1)

| Method | Path | Body → Response |
|---|---|---|
| health | GET /health | `{ok, version, initialized, embedder, dbPath}` |
| init | POST /init | `{vectorDim?}` → `{ok, dim}` |
| search | POST /search | `{query, limit, scope}` → `{results:[{id,text,score,scope,...}]}` |
| remember | POST /remember | `{content, category?, importance?, scope?}` → `{id}` |
| delete | POST /delete | `{id, force?}` → `{ok}` |
| stats | POST /stats | `{}` → `{counts, index, degradedFlags}` |
| list | POST /list | `{scope, limit}` → `{results}` |
| export | POST /export | `{}` → `{memories:[...]}` |
| import | POST /import | `{memories, mode}` → `{count}` |

Auth: loopback + bearer token (in `lorekeeper.json`, like mem0's pattern).

## Hermes provider surface (from mem0 reference)

- `register(ctx)` → `ctx.register_memory_provider(LorekeeperProvider())`
- `MemoryProvider` methods: `initialize`, `system_prompt_block`,
  `prefetch`, `sync_turn`, `get_tool_schemas`, `handle_tool_call`,
  `is_available`, `save_config`, `get_config_schema`, `shutdown`
- Tools: `memory_search`, `memory_remember`, `memory_delete`,
  `memory_stats` (start with these four; add the rest from the fork later)
- Config: `$HERMES_HOME/lorekeeper.json` via `hermes memory setup`

## Phases

1. ✅ **Scaffold + service up** — repo layout, package.json, server/index.js
   with /health + /init + /remember + /search + /delete + /stats + /list +
   /export + /import. Node service runs (localhost:18777, bearer token in
   ~/.hermes/lorekeeper/token).
2. ✅ **Provider wired** — provider/ implements MemoryProvider (mem0 pattern),
   installed to ~/.hermes/plugins/lorekeeper/, activated via
   `hermes config set memory.provider lorekeeper`. Tools: lorekeeper_search,
   lorekeeper_remember, lorekeeper_delete, lorekeeper_stats. Verified
   end-to-end through the Hermes plugin loader.
3. ✅ **Full tool surface** — the fork's 34 tools registered via a generic
   `/tool` dispatcher (imports createMemoryTools/createFeedbackTools/
   createEpisodicTools directly; zod schemas → OpenAI JSON via
   zod-to-json-schema; `memory_` prefix stripped). Provider fetches schemas
   dynamically and dispatches generically — no per-tool RPC.
4. ✅ **Capture hooks** — service `/capture` endpoint (heuristics extraction →
   embed → dedup → store → graph → event → prune); provider `sync_turn`
   buffers each turn and flushes (per-turn + on_session_end / on_pre_compress /
   on_session_switch). minCaptureChars lowered to 40 for per-turn granularity.
5. ⏳ **Import old data** — `/root/.openclaw/memory/lancedb` → Lorekeeper
   (export from old store, import via /import).

### LLM capture/digests (Phase 4b, done)

- `server/llm_shim.js` — implements the OpenCode SDK client surface
  (`session.create/prompt/delete`) over OpenRouter's OpenAI-compatible API, so
  the fork's `requestLLMCapture`/`requestLLMDigest` run unchanged.
- Config: `capture.mode=llm`, provider `openrouter`, model `minimax/minimax-m3`
  (env `OPENCODE_MEMORY_PRO_CAPTURE_LLM_*`).
- `OPENROUTER_API_KEY` auto-loaded from `$HERMES_HOME/.env` by the service.
- `/capture` tries LLM extraction first; falls back to heuristics on any
  failure (mirrors fork's `_flushAutoCaptureGuarded` + `LLM_EMPTY_VERDICT`).
- Verified: one turn → 2 extracted memories (preference + fact), correct
  categories/importance, `llmHealth: healthy`.

## Open questions

- Embedding: default `ollama + nomic-embed-text` (matches fork default) —
  need Ollama reachable at 127.0.0.1:11434. Confirm.
- Capture mode: heuristics (offline) or LLM (needs a resolvable provider)?
  Start heuristics; LLM later.
- Scoping: `"global"` (single-user) — matches our single-user setup.