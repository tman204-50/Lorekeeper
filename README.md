<p align="center">
  <img src="assets/logo.png" alt="Lorekeeper logo" width="240">
</p>

# Lorekeeper

LanceDB-backed long-term memory for Hermes Agent — a fork of
[`opencode-memory-pro`](https://github.com/tman204-50/opencode-memory-pro)
v1.6.2 (MIT) with substantial custom work on top (client efficiency, recall
latency, safety, observability — see [Fork enhancements](#fork-enhancements)).
The battle-tested Node store runs as a localhost HTTP service; a thin Python
plugin implements Hermes' `MemoryProvider` interface.

## Architecture

```
Hermes (Python)                    Lorekeeper service (Node, localhost:18777)
┌──────────────────────┐           ┌──────────────────────────────────────┐
│ provider/            │   HTTP    │ server/index.js                      │
│ MemoryProvider impl  │ ────────► │ MemoryStore (LanceDB) + graph +      │
│ lorekeeper_* tools   │  bearer   │ embedder (ollama/nomic-embed-text)   │
└──────────────────────┘           └──────────────────────────────────────┘
```

- `vendor/dist/` — forked store from opencode-memory-pro 1.6.2 **plus local
  fork fixes** (trigram fuzzy index, consolidate dryRun, merge text stash,
  digest feedback events, cache work — all marked with tag comments like
  `CONSOLIDATE_DRYRUN (0.2.4)`).
- `server/` — the HTTP service wrapper + LLM shim (`llm_shim.js`).
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

For iterating on an installed box, `./install.sh --plugin-only` skips the
full install: it copies `provider/*.py` + `plugin.yaml` into the Hermes
plugin dir, restarts the lorekeeper service, verifies all four version
surfaces agree (`provider`, `server`, installed plugin, `/health`), and runs
the **staleness guard** — hermes-serve is auto-restarted if it predates the
install (it imports the plugin once and caches it; a stale serve process was
the root cause of mysterious desktop-side tool timeouts), and a stale
hermes-gateway is flagged with the exact restart command (never restarted
automatically — that kills active hermes turns). Verification hook after any
update: `grep "lorekeeper.client v" /root/.hermes/logs/agent.log | tail -1`
must show the new version.

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
| POST | `/tools` | `{}` → `{tools:[{name, description, parameters}]}` (35 fork tools) |
| POST | `/tool` | `{name, toolArgs?}` → `{result}` (generic dispatch) |
| POST | `/delete` | `{id, force?}` → `{ok, id}` |
| POST | `/stats` | `{}` → `{counts, index}` |
| POST | `/list` | `{scope?, limit?}` → `{results}` |
| POST | `/exportAll` | `{}` → `{memories}` |
| POST | `/import` | `{memories, mode?}` → `{imported}` |
| POST | `/consolidate` | `{scope?, dryRun?}` → `{ok, scope, result}` (dryRun = fast read-only duplicate estimate) |
| POST | `/metrics` | `{}` → `{timing:[{op,count,avgMs,lastMs,lastExtra}], scopeCache}`; `{"reset":true}` clears |

Auth: `Authorization: Bearer <token>` (token in `~/.hermes/lorekeeper/token`).

## Fork enhancements

What this fork adds over upstream 1.6.2 (all regression-tested; see `test/`):

**Client (`provider/_client.py`)**
- Per-tool timeout table — heavy tools (`summarize`, `consolidate*`,
  `reembed`, `import`) get 300s instead of the 10s default; capture 120s.
- Keep-alive connection + process-wide shared client + `/tools` schema cache.
- TTL search cache with in-flight call coalescing; failure-type-aware
  retries (transient vs schema vs auth).
- Threshold capture flush (long transcripts flush at 1200 chars) and
  prefetch query shaping (400-char head) at both entry points.
- Debug logging: `LOREKEEPER_CLIENT_DEBUG=1`, and every client logs
  `lorekeeper.client vN (first request)` — the staleness verification hook.

**Recall latency (vendor store)**
- Trigram fuzzy channel replaces Fuse.js (warm `store.search` ~426ms → ~20ms;
  88% top-5 overlap with the old channel, typo tolerance preserved).
- Incremental scope-cache patch on write (no full cache rebuild per put).
- `pruneScope` reuses the warm scope cache (post-capture cost ~241ms → ~0.1ms).

**Safety / correctness**
- `consolidate` gained a real `dryRun` (upstream silently stripped it and
  ran a real merge): fast read-only estimate via cache-first row read +
  exact in-memory cosine + greedy simulation (2.3k rows: 43s → ~3s), no
  writes, no confirm required. Real runs still require `confirm=true`.
- `task_episode_delete` tool (by episodeId or taskId, confirm-gated,
  scope-limited) — episodes previously accumulated forever.
- `global_list` `filter` is a strict enum (`unused` | `disabled`);
  `filter="disabled"` lists soft-deleted rows marked `[DISABLED]` so a
  forget stays auditable instead of invisible.
- Consolidation survivors stash absorbed text in
  `metadata.mergedTexts [{id, text ≤800c, mergedAt}]` (last 10) — near-dup
  wording no longer leaves recall unrecoverably.
- `memory_export`/`memory_import` refuse `/tmp`, `/var/tmp`, `/dev/shm`
  paths (0.2.9): the service runs under systemd `PrivateTmp=yes`, so files
  written there land in a private mount namespace the agent can't see — a
  silent split that made exports look "fabricated". Rejected with an
  actionable error; use a stable path (e.g. `/root/.hermes/workspace/...`).
- Tool results that fail *inside* a 200 (vendor tools returning
  `{error: ...}`) are surfaced as `Lorekeeper tool failed: ...` instead of a
  clean-looking result (0.2.9).

**Digests / observability**
- Digest LLM prompts get a 300s budget via `LLMSessionClient.withTimeout`
  (was a 60s cap that silently degraded big digests to extractive);
  `summarize` responses report `digestMode: llm | extractive` per digest
  plus `digestModes` counts.
- Digest creation emits a capture event (`outcome: "digest"`) counted as
  `capture.digests` by `memory_effectiveness` and the KPI weekly view —
  memory folding is no longer silent.
- `/metrics` timing spans (`store.*`, `embedder.*`, `llm.*`, `http.*`),
  scope-cache stats, aborted LLM captures (`withSignal`, 90s controller
  abort), and HTTP `/consolidate` (so long consolidations survive gateway
  tool timeouts).

**Self-tuning recall (Phase 6-7, v0.2.12)**
- Eval harness: `test/recall_eval.mjs` replays 40 query→expected-id pairs
  through the live store, computes MRR + top-5 hit rate, exits 1 on
  regression. Baseline captured at `test/eval/baseline.json`.
- Metrics counters (`searchSignals` in /metrics) — the feedbackWeight
  channel ran blind since 1.6; now visible as boosted/penalized/neutral.
- D4 citation arbitration — memories with verified citations that receive
  negative feedback are not penalized (agent-error attribution).
- Inferred feedback (D1) — successful zero-retry task episodes
  cross-reference recall events by sessionId to auto-boost helpful memories.
  Gated by `OPENCODE_MEMORY_PRO_INFERRED_FEEDBACK_ENABLED`.
- D3 expire sweep — `retentionCandidates()` respects max importance
  thresholds and a protected-category allowlist.
- Tripwire: `lorekeeper eval-check` runs the eval against live store, fires
  `scripts/notify` on regression, exits 1.
- **Dynamic self-tuning (Phase 7):** the store observes its own recall
  quality and adjusts 7 retrieval parameters at runtime — no external
  scripts or cron:
  - Parameter registry: `vectorWeight`, `bm25Weight`, `fuzzyWeight`, `rrfK`,
    `feedbackWeight`, `recencyHalfLifeHours`, `importanceWeight` — each with
    min, max, delta bounds.
  - Every ~500 search calls, the store runs an in-process trial: grid-search
    ±delta per enabled dimension against the eval set. Winner auto-promotes
    if MRR improves ≥0.5%. On plateau, random-walks one parameter.
  - Promoted values persist to `~/.hermes/lorekeeper/tuning.json` and
    survive service restarts. `LOREKEEPER_TUNING_PATH` env to override.
  - Safety rollback: `rollbackTuning()` deletes the tuning file; next boot
    falls back to factory defaults. D7 regression triggers auto-rollback.

**Tool count note:** the service serves **35** tools (the 34 upstream fork
tools + `lorekeeper_task_episode_delete`).

## Two-store topology gotcha

The same tool names exist in **two different stores**, and rows written in
one are invisible to the other:

| Store | Path | Serves |
|---|---|---|
| Lorekeeper **service** store | `~/.hermes/lorekeeper/lancedb` | `lorekeeper_*` prefixed tools (Hermes sessions, via HTTP `/tool`) |
| opencode **plugin** store | `~/.opencode/memory/lancedb` | bare-name tools (`memory_*`, `task_episode_*`) in opencode sessions |

The opencode plugin store also auto-creates `session-ses_*` tracking
episodes (stuck in `running`) — housekeeping concern of the opencode plugin,
not the Hermes-facing service.

## Tests

```bash
# python (HTTP-level, boots scratch services on dedicated ports)
python3 test/test_client.py test_shared.py test_fuzzy_recall.py \
        test_capture_flush.py test_tools_cache.py test_consolidate_dryrun.py \
        test_prefetch_query.py test_v029_client.py

# node (store-level, temp LanceDB dirs, mock embedders / mock OpenRouter)
node test/test_digest_shim.mjs test/test_f234_fixes.mjs test/test_gap12_f1.mjs \
     test/test_v029_guard.mjs
```

Every fork change ships with a regression test in this suite.

## CLI (`bin/lorekeeper`)

The npm package ships a small CLI (also usable from a checkout via
`node bin/lorekeeper`):

```bash
lorekeeper status    # service + DB health (exit 1 if not running)
lorekeeper init      # initialize the store (idempotent)
lorekeeper install   # copy provider/ -> $HERMES_HOME/plugins/lorekeeper/
lorekeeper serve     # run the service in the foreground
lorekeeper eval-check  # run eval set against live store, exit 1 on regression
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
- `LOREKEEPER_CLIENT_DEBUG=1` — per-request debug lines from the Python client.
- `LOREKEEPER_HOST` — bind address (default `127.0.0.1`; see deployment notes).
- `LOREKEEPER_TUNING_PATH` — path to the persisted tuning override file
  (default `~/.hermes/lorekeeper/tuning.json`).

## Data locations

- Memories: `~/.hermes/lorekeeper/lancedb` (LanceDB)
- Entity graph: `~/.hermes/lorekeeper/graph.db` (sqlite)
- Token: `~/.hermes/lorekeeper/token`

## Status

- [x] Phase 1: Node service (health/init/remember/search/delete/stats/list/export/import)
- [x] Phase 2: Hermes provider wired (lorekeeper_search/remember/delete/stats)
- [x] Phase 3: full tool surface (35 fork tools via generic /tool dispatcher)
- [x] Phase 4: auto-capture (service /capture + provider sync_turn/session hooks)
- [x] Phase 4b: LLM capture/digests via OpenRouter shim (minimax/minimax-m3)
- [x] Phase 5: imported old OpenClaw gold memories (885, via import_old_data.mjs)
- [x] Phase 7: in-process self-tuning (parameter registry, trial loop every
      500 searches, grid-search ± delta per dimension, auto-promote ≥0.5%
      MRR improvement, random walk on plateau, tuning.json persistence,
      safety rollback on regression).

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
agent.log shows `Memory provider 'lorekeeper' registered (35 tools)`; and the
agent can actually invoke `lorekeeper_stats` and `lorekeeper_remember` (test on
a fresh session, then delete the test memory — fresh rows outrank old history).

**Gateway/api_server deployments need an explicit toolset entry.** Hermes
classifies a memory-provider plugin `kind=exclusive` and skips it during
plugin discovery, so the `provides_tools` / `tools.py` plugin-toolset path
never engages for this plugin — the toolset key never lands in
`plugin_toolset_keys.json` (the gateway rewrites that cache from discovery
on every boot, dropping hand-added keys). The tools DO register into the
model_tools registry via the memory-provider path (`Memory provider
'lorekeeper' registered (35 tools)`), but sessions on the OpenAI-HTTP/A2A
gateway (`platform: api_server`) resolve their toolset scope from
`platform_toolsets` — and `api_server` is absent from that map by default,
so the 35 tools sit in the registry, out of scope, and every call fails
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