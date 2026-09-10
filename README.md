# Lorekeeper

LanceDB-backed long-term memory for Hermes Agent — a self-contained fork of
[`opencode-memory-pro`](https://github.com/tman204-50/opencode-memory-pro)
v1.6.2 (MIT). The battle-tested Node store runs as a localhost HTTP service;
a thin Python plugin implements Hermes' `MemoryProvider` interface.

## Architecture

```
Hermes (Python)                    Lorekeeper service (Node, localhost:18777)
┌──────────────────────┐           ┌──────────────────────────────────────┐
│ provider/            │   HTTP    │ server/index.js                      │
│ MemoryProvider impl  │ ────────► │ MemoryStore (LanceDB) + graph +      │
│ lorekeeper_* tools   │  bearer   │ embedder (ollama/nomic-embed-text)   │
└──────────────────────┘           └──────────────────────────────────────┘
```

- `vendor/dist/` — forked store from opencode-memory-pro 1.6.2 (unchanged).
- `server/` — the HTTP service wrapper.
- `provider/` — the Hermes plugin (installed to `~/.hermes/plugins/lorekeeper/`).

## Quickstart

```bash
npm install

# 1. Start the service (loopback only, bearer token auto-generated)
npm start
#    token written to ~/.hermes/lorekeeper/token

# 2. Install the Hermes provider
cp -r provider/ ~/.hermes/plugins/lorekeeper/

# 3. Activate
hermes config set memory.provider lorekeeper
```

The provider reads `~/.hermes/lorekeeper/token` (or `LOREKEEPER_TOKEN` env).
Config: `~/.hermes/lorekeeper.json` via `hermes memory setup` (host field).

## Service API

| Method | Path | Body → Response |
|---|---|---|
| GET | `/health` | `{ok, version, initialized, dbPath, embedder}` |
| POST | `/init` | `{}` → `{ok, dim}` |
| POST | `/search` | `{query, limit?, scope?}` → `{results:[{id,text,score,...}], count}` |
| POST | `/remember` | `{content, category?, importance?, scope?}` → `{id}` |
| POST | `/capture` | `{sessionID?, text, scope?}` → `{stored, id?, category?, importance?, skipReason?}` |
| POST | `/tools` | `{}` → `{tools:[{name, description, parameters}]}` (34 fork tools) |
| POST | `/tool` | `{name, toolArgs?}` → `{result}` (generic dispatch) |
| POST | `/delete` | `{id, force?}` → `{ok, id}` |
| POST | `/stats` | `{}` → `{counts, index}` |
| POST | `/list` | `{scope?, limit?}` → `{results}` |
| POST | `/export` | `{}` → `{memories}` |
| POST | `/import` | `{memories, mode?}` → `{imported}` |

Auth: `Authorization: Bearer <token>` (token in `~/.hermes/lorekeeper/token`).

## Env knobs

- `LOREKEEPER_PORT` (default 18777), `LOREKEEPER_TOKEN`, `LOREKEEPER_DB_PATH`,
  `LOREKEEPER_GRAPH_PATH`, `LOREKEEPER_CONFIG`
- `OPENCODE_MEMORY_PRO_*` passthrough knobs (embedder, retrieval, graph, ...) —
  the vendor config resolver reads these.

## Data locations

- Memories: `~/.hermes/lorekeeper/lancedb` (LanceDB)
- Entity graph: `~/.hermes/lorekeeper/graph.db` (sqlite)
- Token: `~/.hermes/lorekeeper/token`

## Status

- [x] Phase 1: Node service (health/init/remember/search/delete/stats/list/export/import)
- [x] Phase 2: Hermes provider wired (lorekeeper_search/remember/delete/stats)
- [x] Phase 3: full tool surface (34 fork tools via generic /tool dispatcher)
- [x] Phase 4: auto-capture (service /capture + provider sync_turn/session hooks)
- [ ] Phase 5: import old data from `/root/.openclaw/memory/lancedb`

See `PLAN.md` for details.

## Notes

- `npm audit` reports 3 high-severity findings in `sharp` (transitive via
  `@lancedb/lancedb` → `@huggingface/transformers`). Localhost-only service,
  no external exposure. `npm audit fix --force` would downgrade lancedb
  (breaking) — not recommended.
- Requires Node ≥ 22 and Ollama (or an OpenAI-compatible embedder via env).

## License

MIT — fork of `opencode-memory-pro` (MIT, tman204-50).