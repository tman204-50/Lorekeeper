---
name: lorekeeper-usage
description: "Use when working with Lorekeeper memory tools."
---

# Lorekeeper — daily usage

Lorekeeper is the LanceDB long-term memory backend (Node service on
localhost:18777 + Python provider exposing 34 `lorekeeper_*` tools). This
skill covers USING the tools day-to-day. For building/porting/deploying the
provider, see `hermes-memory-provider`.

## Memory architecture — 4 tiers, don't mix them

| Tier | Tool | Use for |
|---|---|---|
| **Long-term store** | `lorekeeper_remember` | Durable facts worth recalling across sessions — decisions, preferences, lessons, people, project state. Default synchronous store for ANY durable fact. |
| **Always-injected** | `memory` tool | Small budget store (~2200 chars) — who the user is, standing rules, environment facts. Profile/rules ONLY, never task data. |
| **Session logs** | `memory/YYYY-MM-DD.md` | Raw daily logs, ephemeral. |
| **Auto-capture** | (background) | Conversational facts extracted by minimax-m3 every turn — no action needed, happens automatically. |

**Rule:** if a fact is durable and worth recalling later, `lorekeeper_remember`
it explicitly — don't rely on auto-capture for anything important (it's
best-effort, pending-citation until validated).

## Core tools — when to use

- `lorekeeper_search(query, limit)` — BEFORE answering anything that could
depend on prior context. Hybrid vector+fuzzy search, ~550ms. Always search
first; don't answer from the chat window alone.
- `lorekeeper_remember(text, category?, importance?)` — explicit durable
writes. Categories: fact/decision/preference/learning/profile. Importance
0-1 (profile 0.9, preferences 0.8, facts 0.5-0.7).
- `lorekeeper_stats` — health check: embedder, graph, capture LLM, degraded
flags, timing. Run when asked "how is memory" or to verify a change landed.
- `lorekeeper_dashboard(days)` — weekly learning trends, memory counts by
category.
- `lorekeeper_kpi` — retry-to-success rate, memory lift (needs task episodes).
- `lorekeeper_what_did_you_learn(days)` — recent captures, useful for
check-ins.

## Recall & debugging

- `lorekeeper_why(id)` — why a specific memory was recalled.
- `lorekeeper_explain_recall` — factors behind the LAST recall in this
session.
- `lorekeeper_feedback_useful(id, helpful)` — mark a recalled memory as
helpful/not; feeds effectiveness metrics.
- `lorekeeper_feedback_missing(text)` — record a fact that SHOULD have been
stored but wasn't (trains capture).
- `lorekeeper_feedback_wrong(id, reason)` — record a memory that should NOT
have been stored.
- `lorekeeper_effectiveness` — capture recall + feedback metrics.

## Citations

- `lorekeeper_citation(id, status?)` — view or update citation info for a
memory.
- `lorekeeper_validate_citation(id)` — validate a citation and update its
status (pending → validated/rejected).

## Maintenance

- `lorekeeper_consolidate(scope)` — merge near-duplicates within a scope.
- `lorekeeper_consolidate_all` — global + project dedup (used by cron).
- `lorekeeper_expire(dryRun)` — retention sweep: fold old+unused into
digests. dryRun=true first, always.
- `lorekeeper_summarize` — digest old memories (LLM abstractive).
- `lorekeeper_reembed(dryRun)` — fix embedder-dimension mismatch; backs up
all memories first, then re-embeds under original ids.
- `lorekeeper_export(path)` / `lorekeeper_import(path, mode)` — backup /
restore. Export before any risky maintenance.
- `lorekeeper_delete(id)` / `lorekeeper_forget(id)` — remove an entry.
- `lorekeeper_clear(scope, confirm)` — wipe a scope. DANGEROUS — confirm=true
required.
- `lorekeeper_event_cleanup(dryRun, archivePath)` — purge expired
effectiveness events (older than retentionDays).

## Scopes

- `lorekeeper_scope_promote(id)` / `lorekeeper_scope_demote(id)` — move a
memory between project and global scope.
- `lorekeeper_global_list(query, filter)` — list global-scoped memories.
- Default scope is `global` (single-user setup).

## Task episodes (episodic learning)

- `lorekeeper_task_episode_create(taskId, description)` — start tracking a
task.
- `lorekeeper_task_episode_query(scope, state)` — find episodes.
- `lorekeeper_similar_task_recall(query, threshold)` — find past tasks by
keyword overlap.
- `lorekeeper_retry_budget_suggest(errorType)` — retry budget from history.
- `lorekeeper_recovery_strategy_suggest(taskId)` — recovery strategies after
failures. Use after a failed task before retrying.

## Infrastructure

- `lorekeeper_port_plan(project, services, rangeStart?, rangeEnd?)` — plan
non-conflicting host ports for compose services; optionally persist
reservations. Use when deploying docker-compose stacks that need stable
ports.

## Operational gotchas

- **Plugin loads at session start.** Changes to `plugins.enabled` in
config.yaml take effect NEXT session, not this one. A session that predates
a plugin fix keeps the old routing table — its tool calls fail with
'Unknown tool' regardless of server state. Restart the session.
- **Service must be up.** If lorekeeper tools hang/fail, check
`systemctl status lorekeeper` (Node service on 18777). Embedder needs
Ollama up too.
- **Config changes need service restart.** `server/index.js` sets
`process.env.X ??= value` defaults; edits there require
`systemctl restart lorekeeper`. Env vars set in the unit override the
`??=` defaults.
- **Adaptive injection is live:** floor 0.2, drop tolerance 0.15,
maxMemories 3. The cap (3) bites before the cliff usually does — don't
expect adaptive cliff-stopping to matter until maxMemories is raised.
- **Standing rule:** every lorekeeper code change → commit + push to GitHub
(github.com/tman204-50/Lorekeeper, branch main).
- **`recentCount` is a window metric, not a total.** Read the table directly
for a true count.

## Health-check pattern (when asked "how is memory")

1. `lorekeeper_stats` — look at embedderHealth, llmHealth, degradedFlags,
incompatibleVectors, dimensionMismatch.
2. If any degraded flag → investigate (service, Ollama, openrouter key).
3. If stats are clean, report: healthy, N memories, graph size, capture
status. Don't over-report — "healthy, no flags" is a complete answer.
