#!/usr/bin/env node
/**
 * Recall eval runner — Phase 6a.
 *
 * Reads test/eval/recall_set.json (versioned query → expected-id pairs),
 * replays each case through the LIVE lorekeeper /search endpoint,
 * computes per-case rank + aggregate MRR + top-5 hit rate,
 * and writes/validates test/eval/baseline.json.
 *
 * Fast HTTP-only — tests the full ranking pipeline without a scratch store.
 * A future iteration may add a scratch-store mode for CI isolation.
 *
 * Usage:
 *   node test/recall_eval.mjs              # run, compare vs baseline, exit 1 on regression
 *   node test/recall_eval.mjs --baseline   # run and overwrite baseline
 *   node test/recall_eval.mjs --json       # run, print JSON results only
 */

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const REPO_DIR = resolve(join(dirname(__filename), ".."));
const SET_PATH = join(REPO_DIR, "test/eval/recall_set.json");
const BASELINE_PATH = join(REPO_DIR, "test/eval/baseline.json");
const LIVE_HOST = "http://127.0.0.1:18777";
// Try known token locations
function loadToken() {
  const candidates = [
    process.env.LOREKEEPER_TOKEN,
    ...cat(join(process.env.HOME, ".hermes/lorekeeper/token")),
    ...cat(join(process.env.HOME, ".hermes", "lorekeeper.json")).map(f => { try { return JSON.parse(f).token; } catch { return null; }}),
  ].filter(Boolean);
  if (candidates.length === 0) {
    console.error("FATAL: LOREKEEPER_TOKEN not found. Set env or ensure ~/.hermes/lorekeeper/token");
    process.exit(1);
  }
  return candidates[0];
}

function cat(path) {
  try { return [readFileSync(path, "utf-8").trim()]; }
  catch { return []; }
}

const TOKEN = loadToken();

// ── helpers ───────────────────────────────────────────────

function normalizeId(id) {
  return id.slice(0, 12).replace(/[^a-zA-Z0-9]/g, "");
}

function mrr(ranks) {
  if (ranks.length === 0) return 0.0;
  let sum = 0;
  for (const r of ranks) sum += r > 0 ? 1.0 / r : 0;
  return sum / ranks.length;
}

function hit5(ranks) {
  if (ranks.length === 0) return 0.0;
  let hit = 0;
  for (const r of ranks) if (r > 0 && r <= 5) hit++;
  return hit / ranks.length;
}

// ── run search ────────────────────────────────────────────

async function search(query, limit = 5) {
  const url = `${LIVE_HOST}/search`;
  const body = JSON.stringify({ query, limit, scope: "global" });
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${TOKEN}`,
    },
    body,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`/search returned ${resp.status}: ${text.slice(0, 500)}`);
  }
  const data = await resp.json();
  const items = data.results ?? data.data ?? [];
  return items.map(i => ({ id: i.id, score: i.score, text: (i.text ?? "").slice(0, 120) }));
}

// ── evaluate ──────────────────────────────────────────────

async function evaluate() {
  const setRaw = JSON.parse(readFileSync(SET_PATH, "utf-8"));
  const set = Array.isArray(setRaw.cases) ? setRaw.cases : (Array.isArray(setRaw) ? setRaw : []);
  if (setRaw.version) console.error(`Eval set v${setRaw.version}, ${setRaw.cases.length} cases`);

  const results = [];
  for (const c of set) {
    const q = c.q ?? c.query;
    const expects = c.expect ?? c.expects ?? [];
    const normExpects = expects.map(normalizeId);

    process.stderr.write(`  ${q}... `);

    let rank = 0;
    let found = false;
    let topScores = [];

    try {
      const hits = await search(q, 5);
      topScores = hits.map(h => ({ id: h.id, score: h.score, text: h.text }));

      for (let i = 0; i < hits.length; i++) {
        const hitNorm = normalizeId(hits[i].id);
        for (const ne of normExpects) {
          if (hitNorm === ne || hitNorm.startsWith(ne) || ne.startsWith(hitNorm)) {
            rank = i + 1;
            found = true;
            break;
          }
        }
        if (found) break;
      }
    } catch (e) {
      process.stderr.write(`[ERROR] ${e.message}\n`);
      results.push({ query: q, expects, rank: 0, found: false, error: e.message, topScores: [] });
      continue;
    }

    process.stderr.write(found ? `rank=${rank}\n` : `NOT FOUND (top: ${topScores.map(h => h.id.slice(0,12)).join(", ")})\n`);
    results.push({ query: q, expects, rank, found, topScores, error: null });
  }
  return results;
}

// ── aggregate ─────────────────────────────────────────────

function aggregate(results) {
  const ranks = results.map(r => r.found ? r.rank : 0);
  const meanRank = ranks.filter(r => r > 0).reduce((a, b) => a + b, 0) / Math.max(1, ranks.filter(r => r > 0).length);
  return {
    mrr: mrr(ranks),
    hit5: hit5(ranks),
    totalCases: results.length,
    found: ranks.filter(r => r > 0).length,
    notFound: ranks.filter(r => r === 0).length,
    meanRank: Math.round(meanRank * 100) / 100,
    byRank: { 1: ranks.filter(r => r === 1).length, 2: ranks.filter(r => r === 2).length,
              3: ranks.filter(r => r === 3).length, 4: ranks.filter(r => r === 4).length,
              5: ranks.filter(r => r === 5).length, "notFound": ranks.filter(r => r === 0).length },
    generatedAt: new Date().toISOString(),
  };
}

// ── main ──────────────────────────────────────────────────

async function main() {
  const writeBaseline = process.argv.includes("--baseline");
  const jsonOnly = process.argv.includes("--json");

  const results = await evaluate();
  const stats = aggregate(results);

  if (jsonOnly) {
    console.log(JSON.stringify({ stats, results }, null, 2));
    return;
  }

  // Report
  process.stderr.write("\n");
  process.stdout.write("-".repeat(50) + "\n");
  process.stdout.write(`MRR:        ${(stats.mrr * 100).toFixed(1)}%\n`);
  process.stdout.write(`Top-5 hit:  ${(stats.hit5 * 100).toFixed(1)}%\n`);
  process.stdout.write(`Found:      ${stats.found}/${stats.totalCases}\n`);
  process.stdout.write(`Not found:  ${stats.notFound}\n`);
  process.stdout.write(`Mean rank:  ${stats.meanRank}\n`);
  process.stdout.write(`By rank:    ${JSON.stringify(stats.byRank)}\n`);
  process.stdout.write(`Generated:  ${stats.generatedAt}\n`);
  process.stdout.write("-".repeat(50) + "\n");

  // Baseline comparison
  const baselineExists = existsSync(BASELINE_PATH);

  if (writeBaseline) {
    const baseline = { setVersion: 1, mrr: stats.mrr, hit5: stats.hit5,
                       totalCases: stats.totalCases, generatedAt: stats.generatedAt,
                       commit: "CURRENT" };
    writeFileSync(BASELINE_PATH, JSON.stringify(baseline, null, 2) + "\n", "utf-8");
    process.stdout.write(`\nBaseline written to test/eval/baseline.json\n`);
  } else if (baselineExists) {
    const bl = JSON.parse(readFileSync(BASELINE_PATH, "utf-8"));
    const mrrRegressed = stats.mrr < bl.mrr - 0.01;
    const hit5Regressed = stats.hit5 < bl.hit5 - 0.01;
    if (mrrRegressed || hit5Regressed) {
      process.stdout.write(`\n⚠  REGRESSION vs baseline (MRR ${bl.mrr.toFixed(3)} → ${stats.mrr.toFixed(3)})\n`);
      process.stdout.write(`   To update baseline: node test/recall_eval.mjs --baseline\n`);
      process.exitCode = 1;
    } else {
      process.stdout.write(`\n✓  Baseline holds.`);
      if (stats.mrr > bl.mrr) process.stdout.write(`  MRR improved: ${bl.mrr.toFixed(3)} → ${stats.mrr.toFixed(3)}`);
      if (stats.hit5 > bl.hit5) process.stdout.write(`  Hit5 improved: ${bl.hit5.toFixed(3)} → ${stats.hit5.toFixed(3)}`);
      process.stdout.write("\n");
    }
  }

  // Per-case details on stderr on failure
  if (stats.notFound > 0) {
    process.stderr.write("\nNot found:\n");
    for (const r of results) {
      if (!r.found) process.stderr.write(`  ${r.query}\n`);
    }
  }
}

main();