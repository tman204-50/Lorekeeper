# Lorekeeper — Port Plan (Option A: Service Wrapper)

Self-contained fork of `lorekeeper` v1.6.2. The Node store runs as a
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
├── vendor/               # forked dist/ from lorekeeper 1.6.2
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
5. ✅ **Import old data** — `/root/.openclaw/memory/lancedb` → Lorekeeper.
   Imported the **gold categories only** (memory/fact/preference/learning/
   profile = 908 rows, 23 test-junk dropped → 885 imported) via
   `import_old_data.mjs` (re-embeds with nomic-embed-text, preserves ids/
   timestamps, maps old `memory` → `general`). Skipped the ~37k noise rows
   (resource/event/conversation/daily-log) — they'd wreck recall. Store now:
   888 memories (885 imported + 3 prior).

### LLM capture/digests (Phase 4b, done)

- `server/llm_shim.js` — implements the OpenCode SDK client surface
  (`session.create/prompt/delete`) over OpenRouter's OpenAI-compatible API, so
  the fork's `requestLLMCapture`/`requestLLMDigest` run unchanged.
- Config: `capture.mode=llm`, provider `openrouter`, model `minimax/minimax-m3`
  (env `LOREKEEPER_CAPTURE_LLM_*`).
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

## Phase 6: Closed-loop self-tuning recall

Goal: the store stops being hand-tuned. Effectiveness telemetry it already
collects feeds back into ranking and retention, and every ranking change is
gated by a reproducible eval number instead of vibes.

**Key finding (2026-10-05):** the vendor store already has a `feedbackWeight`
channel at default 0.3 — it tracks manual `helpful`/`unhelpful`/`wrong` per
memory, computes a `feedbackFactor` (0.5–2.0 range), and applies it as a
score multiplier (`1 + feedbackWeight * (feedbackFactor - 1)`). Our baseline
MRR of 72.8% already includes this operating. Phase 6 scope corrects for what
ACTUALLY needs building.

Design decisions (Todd, 2026-10-05) remain authoritative — they define what
the behavior contract is, even if some are already satisfied by the existing
code.

### D1–D8 standing (all DECIDED, not all need code)

- **D1 — Helpfulness signal:** both manual flags + inferred signals, inferred
  weighted higher. → **Needs new work** (inferred signal pipeline from
  task_episodes).
- **D2 — Boost ceiling:** relevance always wins; boost is tie-breaker only.
  → **Already satisfied** by `feedbackWeight=0.3` cap (max multiplier ~1.3×
  when feedbackFactor=2.0; cannot flip relevance order).
- **D3 — Digest eligibility:** never-recalled AND low-importance only, with
  category allowlist. → **Needs verification/update** — the store's
  `protectedCategories` already exists, but need to confirm the expire-sweep
  candidate picker respects all three conditions (recall-age, importance,
  allowlist).
- **D4 — Error attribution:** `validate_citation` arbitrates. → **Deferred**
  — the `wrong` feedback type exists, but citation validation as a mechanism
  does not. Needs the citation system built first.
- **D5 — Transparency:** `/metrics` counters only. → **Needs new work** —
  add `store.search.signals` counters showing feedbackFactor distribution,
  count of records boosted/penalized, etc.
- **D6 — Eval set refresh:** quarterly auto-refresh, Todd confirms baseline.
  → **Already designed into the runner** (--baseline flag + git commit
  workflow). No code needed until first refresh.
- **D7 — Live tripwire:** degradation → scripts/notify + auto-revert.
  → **Needs new work** — cron-wrapper that runs the eval runner against the
  live store and triggers on regression.
- **D8 — Capture boundary:** read-path loop only for Phase 6; capture future.
  → **Already honored.** No changes to capture path.

### 6a. Recall eval harness (✅ DONE — committed c8a574d + a0f7a04)

- [x] `test/eval/recall_set.json` — 40 query→expected-id pairs (exact hits,
      R1 typo regressions, paraphrases, cross-category, negatives).
- [x] `test/recall_eval.mjs` — hits live /search, computes MRR + top-5 hit
      rate, compares vs `test/eval/baseline.json`, exits 1 on regression.
- [x] Baseline captured: MRR **72.8%**, top-5 hit **100%**, 40/40 found.
- [x] Standing rule in play: every ranking change ships with an MRR number
      in the commit message or it doesn't ship.

### 6b. Effectiveness visibility (new scope — what actually needs building)

**No `relevance_signals.js` module.** The existing `store.js` scoring pipeline
already handles the feedback factor. All five items below were built in
commits d37ca71, 33254c4, 786713b, a7b5364 (Phase 6, Oct 5-6 2026):

1. ✅ **Metrics counters (D5).** `searchSignals` object in /metrics tracking
   boosted/penalized/neutral per search call. The feedbackWeight channel
   (at 0.3 since 1.6) now has visibility.

2. ✅ **Inferred signal pipeline (D1).** `getInferredFeedbackForScopes()` in
   store.js queries successful zero-retry task episodes, cross-references
   recall events by sessionId in the effectiveness_events table, and merges
   inferred `{helpful: 1}` signals into the feedback factor. Gated by
   `LOREKEEPER_INFERRED_FEEDBACK_ENABLED` (ON in production).

3. ✅ **Category allowlist + importance gate (D3).** `retentionCandidates()`
   gained `maxImportanceForExpiry` parameter (default-off). When > 0, only
   memories at or below that importance are digest-eligible. Default
   `protectedCategories` expanded to include `"profile"`.

4. ✅ **D7 tripwire.** `lorekeeper eval-check` runs recall_eval.mjs against
   the live store, fires `scripts/notify` on regression, exits 1.

5. ✅ **D4 citation arbitration.** `getMemoryFeedbackStatsMap()` checks each
   memory's citationStatus before applying penalties. Memories with
   `citationStatus=verified` that received negative feedback have those
   penalties zeroed (memory was correct — agent error). Always-on, no toggle.

### 6c. Acceptance criteria (all ✅ — Phase delivered Oct 6, 2026)

- [x] Eval runner + seed set + baseline committed; runs <30s on 2.5k rows.
- [x] MRR + hit5 reported per ranking change in commit messages.
- [x] Metrics counters visible in /metrics showing feedback channel activity.
- [x] Inferred signals: task-episode success rate feeds into feedback stats
      (default-on flag in production).
- [x] Category allowlist + importance gate verified in expire sweep.
- [x] D7 tripwire script exists and fires scripts/notify on regression.
- [x] D4 citation arbitration — memories with verified citations are not
      penalized for negative feedback (agent-error attribution).
- [ ] Live-store dry measurement (scratch instance, export/import round-trip)
      before any non-default flag flips on lorekeeper.service.
- [ ] Version bump in all three surfaces (provider/_version.py,
      server SERVICE_VERSION, provider/plugin.yaml) + install.sh
      --plugin-only verification, per standing rule.

### 6d. Deferred (parked, requires 6a eval data or separate decision)

- **ANN index for the scan.** O(n) search at ~20ms is fine at 2.5k rows;
      becomes the wall at ~20k. LanceDB native ANN (VARCH/IVF-PQ) once
      eval set proves the quality delta.
- **Cross-path search cache (S3).** Remains parked; eval set gives it a
      testbed if http.search counts ever justify reviving.

## Phase 7: Dynamic self-tuning (spec, not started — needs Todd's design decisions)

**Core constraint (Todd, 2026-10-05):** no external scripts or cron jobs
tweaking variables. The tuning must be built INTO the store system itself —
the store observes its own performance and adjusts its parameters at runtime
without an outside orchestrator.

### What this changes vs the initial Phase 7 draft

The old draft assumed an external `bin/lorekeeper param-explore` cron script
that grids parameters on a scratch store and promotes winners. That's dead.
Instead, the store gains an internal feedback loop:

```
Every search call →
  accumulate metrics (MRR proxy, feedback stats, recency distribution) →
  when enough signal accumulates →
  run an in-process parameter trial →
  if winner improves the eval score →
  adopt it live
```

All inside the same Node process, no external scripts, no cron, no file edits.

### Design decisions (Todd, 2026-10-05)

**Q1 — Trigger.** After every N searches (default 500). Discrete lookback
window; the store counts `searchSignals.calls` since the last trial and
fires when it crosses the threshold.

**Q2 — Fitness.** The Phase 6 eval set (40 query→expected-id pairs). Same
harness — compute MRR over the live store. No implicit metrics; only the
eval set determines "better."

**Q3 — Exploration strategy.** Grid search over fixed parameter combos.
Enumerate the candidate space, score each against the eval set, pick the
one with the highest MRR. Simpler to implement and audit than gradient or
random sampling, and the eval runner already exists.

**Q4 — Trial location.** Live store, same process. The eval runs against
the production LanceDB (brief latency spike during the ~30s eval). No temp
stores, no exports, no separate workers. The latency cost is bounded by the
eval runner's existing runtime (~5s for 40 queries at ~20ms each).

**Q5 — Promotion threshold.** Auto-accept if MRR improves ≥0.5% over the
stored baseline. Smaller improvements are logged but not promoted.

**Q6 — Termination.** Never stops. Even at plateau, random-walk every N
searches (nudge one random weight by a small delta, eval, keep if better,
revert if worse). Prevents lock-in to a local maximum.

**Q7 — Safety bounds.** Per-parameter max delta: no weight can change by
more than ±0.15 per trial. Hard min/max ranges per parameter also enforced:
- `vectorWeight`: [0.3, 0.9]
- `bm25Weight`: [0.1, 0.7]
- `fuzzyWeight`: [0.0, 0.5]
- `rrfK`: [10, 120]
- `feedbackWeight`: [0.0, 0.5]
- `recencyHalfLifeHours`: [24, 336]
- `importanceWeight`: [0.0, 1.0]

### Build plan (updated for in-store self-tuning)

**Step 1 — Parameter registry.** A static config in `store.js` that lists
tunable parameters with their current value, min, max, delta-limit, and a
`trialWeight` flag (0 = never trial this one). Replaces the current
env-var-only resolution with a runtime-mutable source of truth.

**Step 2 — Trial scheduler.** In `_search`, after every N calls (gated by
`searchSignals.calls` counter), schedule a trial:
  1. Take a snapshot of current parameter values.
  2. Generate candidate combos within the delta limits.
  3. For each candidate: set parameters, run eval set queries, compute MRR.
  4. If any candidate beats baseline by ≥0.5%, adopt the winning combo.
  5. At plateau: random-walk one parameter by one step.
  6. Restore parameters to live values after the trial (the eval queries
     are read-only — no data changes).

**Step 3 — Safety tripwire.** If the adopted parameter set later causes the
D7 regression check to fire, the store auto-rolls back to the previous
parameter set and doubles the trial interval (N × 2, so 500 → 1,000 → 2,000) before the next attempt.

### Parameters out of scope for Phase 7
- Capture thresholds (minCaptureChars, dedup writeThreshold) — deferred
  behind D8, needs its own decision round.
- Consolidation threshold (0.95) — has a false-positive rate from feedback
  that could tune it, but the feedback signal is still thin (~50 events).

### Acceptance criteria

- [ ] Parameter registry live in store.js with all tunable values.
- [ ] Every ~500 search calls, a trial runs automatically (verify by
      watching `searchSignals` + eval set replay in logs).
- [ ] Trial runs complete in <30s on the live store (no perceptible
      degradation in concurrent search latency).
- [ ] Grid search enumerates at least 9 combos across the candidate space.
- [ ] ≥0.5% MRR improvement auto-promotes; <0.5% logs and skips.
- [ ] Random walk on plateau: a different parameter nudged each trial.
- [ ] Safety rollback fires on D7 regression.
- [ ] No new external processes, scripts, or cron jobs required.