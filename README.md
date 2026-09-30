<p align="center">
  <img src="assets/logo.png" alt="Lorekeeper logo" width="240">
</p>

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

## One-command install

```bash
curl -fsSL https://raw.githubusercontent.com/tman204-50/Lorekeeper/main/install.sh | bash
```

What it does:
1. Clones the repo to `~/.local/share/lorekeeper` + `npm install` (LanceDB native deps)
2. Creates `~/.hermes/lorekeeper/` data dir + auto-generates the bearer token
3. Installs a `lorekeeper.service` systemd unit (user-level, auto-start)
4. Copies the Hermes provider to `$HERMES_HOME/plugins/lorekeeper/`
5. Copies the usage skill to `$HERMES_HOME/skills/lorekeeper-usage/`
6. Sets `memory.provider = lorekeeper`
7. Writes `$HERMES_HOME/lorekeeper.json` with host + token

Requirements: node >= 22, npm, git, hermes CLI. Optional: ollama with
`nomic-embed-text` (falls back to OpenAI embedder).

Env overrides: `LOREKEEPER_REPO_URL`, `LOREKEEPER_REPO_REF`,
`LOREKEEPER_INSTALL_DIR`, `LOREKEEPER_DATA_DIR`, `LOREKEEPER_PORT`,
`HERMES_HOME`.

## Manual quickstart

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

## CLI (`bin/lorekeeper`)

The npm package ships a small CLI (also usable from a checkout via
`node bin/lorekeeper`):

```bash
lorekeeper status    # service + DB health (exit 1 if not running)
lorekeeper init      # initialize the store (idempotent)
lorekeeper install   # copy provider/ -> $HERMES_HOME/plugins/lorekeeper/
lorekeeper serve     # run the service in the foreground
```

Env: `LOREKEEPER_PORT`, `LOREKEEPER_TOKEN`, `LOREKEEPER_DB_PATH`,
`LOREKEEPER_GRAPH_PATH`, `HERMES_HOME`.

## Env knobs

- `LOREKEEPER_PORT` (default 18777), `LOREKEEPER_TOKEN`, `LOREKEEPER_DB_PATH`,
  `LOREKEEPER_GRAPH_PATH`, `LOREKEEPER_CONFIG`
- `OPENCODE_MEMORY_PRO_*` passthrough knobs (embedder, retrieval, graph, ...) —
  the vendor config resolver reads these.
- `OPENROUTER_API_KEY` — loaded from `$HERMES_HOME/.env` automatically; enables
  LLM capture/digests via the shim (`server/llm_shim.js`).
- `OPENCODE_MEMORY_PRO_CAPTURE_LLM_MODEL` — default `minimax/minimax-m3`.

## Data locations

- Memories: `~/.hermes/lorekeeper/lancedb` (LanceDB)
- Entity graph: `~/.hermes/lorekeeper/graph.db` (sqlite)
- Token: `~/.hermes/lorekeeper/token`

## Status

- [x] Phase 1: Node service (health/init/remember/search/delete/stats/list/export/import)
- [x] Phase 2: Hermes provider wired (lorekeeper_search/remember/delete/stats)
- [x] Phase 3: full tool surface (34 fork tools via generic /tool dispatcher)
- [x] Phase 4: auto-capture (service /capture + provider sync_turn/session hooks)
- [x] Phase 4b: LLM capture/digests via OpenRouter shim (minimax/minimax-m3)
- [x] Phase 5: imported old OpenClaw gold memories (885, via import_old_data.mjs)

See `PLAN.md` for details.

## Deploying for a second/remote/containerized Hermes (the 3-hour pitfalls)

A second Hermes install (another box, a Docker container, or a peer agent like
Adriana) should get **its own Lorekeeper service and store** — not a shared one.
Auto-captured stores are ~95% operational noise; sharing wrecked recall in
testing. One service per HERMES_HOME, one store each. Battle-tested procedure
from the Janus/Adriana deployment (2026-09-29):

1. **Prereqs on the target host**: Node ≥ 22, git, Ollama + `ollama pull
   nomic-embed-text` (CPU-only is fine for embeddings). If the host's apt mirror
   is dead, install Node from the official tarball and fix the mirror separately.
2. **Bind address**: the service hard-binds `127.0.0.1` by default. For a
   containerized Hermes on the same host, set `LOREKEEPER_HOST=0.0.0.0` in the
   unit and point the client at the host's LAN IP (`LOREKEEPER_HOST` env /
   `lorekeeper.json` `host`). Token auth is mandatory whenever the port leaves
   loopback.
3. **One container only.** If the target runs Docker, verify there is exactly
   ONE Hermes container and that its published ports are the ones your clients
   actually hit (`docker ps` — an old duplicate container serving the real ports
   cost us hours: every restart hit the wrong container). Put the service under
   systemd/s6 inside that container's host with `Restart=on-failure`.
4. **Stale gateway lock**: after recreating containers, `~/.local/state/hermes/
   gateway-locks/host-gateway.json` can pin a PID that no longer exists; every
   new gateway then refuses to start ("Refusing to start a second gateway").
   `rm` the lock + `gateway_state.json` and restart the gateway service. Note
   `docker restart` may NOT bounce the in-container gateway — stop/start the
   gateway service itself.
5. **Plugin file permissions**: the container typically runs as uid 10000 while
   files written from the host are root:root 600. `chown 10000:10000` the
   plugin dir, `lorekeeper.json`, `lorekeeper/token`, and the usage skill.
6. **Config keys that must all be true at once** (any one missing = tools
   silently absent):
   - `memory.provider: lorekeeper` and `memory.memory_enabled: true`
   - plugin files in `$HERMES_HOME/plugins/lorekeeper/` (including `tools.py`)
   - `plugins.enabled: [lorekeeper]` — provider registration alone does NOT
     expose tools
   - `known_plugin_toolsets.<platform>` must NOT list `lorekeeper` unless the
     platform's `platform_toolsets` list also includes it (known-but-absent =
     disabled)
   - the session's platform toolset must include the `memory` toolset (or the
     plugin toolset) — the provider tool injection is gated on it
7. **Session pinning**: sessions freeze their model AND their tool array at
   creation (`sessions.tool_names` pin); config changes never reach an existing
   session. `session_reset.mode: none` makes this permanent. After changing
   config, close the open sessions (`sessions.ended_at`) / start a new session —
   a gateway restart alone does not do it.
8. **Models**: sessions pin the model at creation; a config model switch only
   applies to new sessions.

Verification sequence (all must pass): `curl :18777/health` from inside the
container namespace; `hermes config get memory.provider` inside the container;
agent.log shows `Memory provider 'lorekeeper' registered (34 tools)`; and the
agent can actually invoke `lorekeeper_stats` and `lorekeeper_remember` (test on
a fresh session, then delete the test memory — fresh rows outrank old history).

**Gateway/api_server deployments need an explicit toolset entry.** Hermes
classifies a memory-provider plugin `kind=exclusive` and skips it during
plugin discovery, so the `provides_tools` / `tools.py` plugin-toolset path
never engages for this plugin — the toolset key never lands in
`plugin_toolset_keys.json` (the gateway rewrites that cache from discovery
on every boot, dropping hand-added keys). The tools DO register into the
model_tools registry via the memory-provider path (`Memory provider
'lorekeeper' registered (34 tools)`), but sessions on the OpenAI-HTTP/A2A
gateway (`platform: api_server`) resolve their toolset scope from
`platform_toolsets` — and `api_server` is absent from that map by default,
so the 34 tools sit in the registry, out of scope, and every call fails
with "'lorekeeper_*' is not available in this session". The durable fix is
an explicit entry (verified on Janus/Adriana 2026-09-29, survives container
restarts and cache rewrites):

```yaml
platform_toolsets:
  api_server:
    - browser
    - code_execution
    - connections
    - cronjob
    - delegation
    - file
    - image_gen
    - memory
    - lorekeeper        # <- required for gateway sessions
    - session_search
    - skills
    - terminal
    - todo
    - vision
    - web
```

(That list is the stock `api_server` default plus `lorekeeper`; saving it
makes the list authoritative, so include the defaults. The `memory` entry
gates the provider tool injection — keep it.) `provider/tools.py` still
ships: harmless where it cannot load, load-bearing where a future Hermes
build stops classifying providers as exclusive.

## Notes

- `npm audit` reports 3 high-severity findings in `sharp` (transitive via
  `@lancedb/lancedb` → `@huggingface/transformers`). Localhost-only service,
  no external exposure. `npm audit fix --force` would downgrade lancedb
  (breaking) — not recommended.
- Requires Node ≥ 22 and Ollama (or an OpenAI-compatible embedder via env).

## License

MIT — fork of `opencode-memory-pro` (MIT, tman204-50).