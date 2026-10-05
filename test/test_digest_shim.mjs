#!/usr/bin/env node
// Regression tests for B3 / DIGEST_TIMEOUT (0.2.5): the LLM shim's 60s hard
// timeout used to kill big digest prompts and the digest silently degraded to
// the extractive fallback (response looked identical).
//
// Asserts:
//   - requestLLMDigest gives the digest prompt a 300s alarm clock
//     (client.withTimeout duck-type), capture keeps the 60s default
//   - buildGroupDigest reports llm=true when the LLM answers, llm=false
//     (extractive) when it fails — the flag the summarize response surfaces
//     as digestMode
//
// Run:  node test/test_digest_shim.mjs

import assert from "node:assert";
import { LLMSessionClient } from "../server/llm_shim.js";
import { requestLLMDigest, requestLLMCapture } from "../vendor/dist/llm.js";
import { buildGroupDigest } from "../vendor/dist/tools/memory.js";

const results = [];
function check(name, ok, detail = "") {
  results.push(ok);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}: ${detail}`);
}

// Spy on AbortSignal.timeout to record the alarm clocks the shim sets.
const origTimeout = AbortSignal.timeout.bind(AbortSignal);
const timeoutCalls = [];
AbortSignal.timeout = (ms) => { timeoutCalls.push(ms); return origTimeout(ms); };

// Mock OpenRouter: returns a canned completion; failMode toggles errors.
let failMode = false;
let seenUrls = [];
const origFetch = globalThis.fetch;
globalThis.fetch = async (url, init) => {
  seenUrls.push(String(url));
  if (failMode) throw new Error("mock OpenRouter down");
  // capture prompts end with "Extract memories now." and need JSON replies;
  // digest prompts get plain text
  const bodyReq = JSON.parse(init?.body ?? "{}");
  const lastMsg = bodyReq.messages?.[bodyReq.messages.length - 1]?.content ?? "";
  const content = lastMsg.includes("Extract memories now") ? "[]" : "MOCK DIGEST TEXT";
  const body = JSON.stringify({ choices: [{ message: { content } }] });
  return { ok: true, json: async () => JSON.parse(body), text: async () => body };
};

const LLM_CFG = { provider: "openrouter", model: "mock-model" };

try {
  // 1. digest prompt gets a 300s alarm clock (was 60s -> silent fallback)
  timeoutCalls.length = 0;
  const client = new LLMSessionClient({ apiKey: "test-key", model: "mock-model" });
  const digest = await requestLLMDigest(client, LLM_CFG, ["memory one", "memory two"], 500, "fact");
  check("digest-succeeds", digest && digest.text === "MOCK DIGEST TEXT" && digest.sourceCount === 2,
    `text=${digest?.text} sourceCount=${digest?.sourceCount}`);
  check("digest-300s-timeout", timeoutCalls.includes(300000), `alarm clocks: ${JSON.stringify(timeoutCalls)}`);

  // 2. capture prompt keeps the 60s default (+ external abort is separate)
  timeoutCalls.length = 0;
  const cap = await requestLLMCapture(client, LLM_CFG, "some session text", "sess-1");
  check("capture-parses", cap !== null, `capture=${JSON.stringify(cap)}`);
  check("capture-60s-timeout", timeoutCalls.length > 0 && timeoutCalls.every((t) => t === 60000),
    `alarm clocks: ${JSON.stringify(timeoutCalls)}`);

  // 3. LLM answers -> buildGroupDigest says llm:true
  const stateLlm = { config: { capture: { mode: "llm", llm: LLM_CFG } }, client };
  const g1 = await buildGroupDigest(stateLlm, [{ text: "a" }, { text: "b" }, { text: "c" }], 500, "fact", new Set());
  check("group-digest-llm-mode", g1 && g1.llm === true && g1.text === "MOCK DIGEST TEXT",
    `llm=${g1?.llm} text=${g1?.text}`);

  // 4. LLM fails -> extractive fallback, flagged llm:false (NOT silent)
  failMode = true;
  const g2 = await buildGroupDigest(stateLlm, [{ text: "alpha beta gamma" }, { text: "delta epsilon" }, { text: "zeta" }], 500, "fact", new Set());
  check("group-digest-extractive-mode", g2 && g2.llm === false && g2.text && g2.text.length > 0,
    `llm=${g2?.llm} text=${(g2?.text ?? "").slice(0, 40)}`);
  failMode = false;

  // 5. no capture.mode=llm -> straight to extractive (llm:false), no LLM call
  seenUrls.length = 0;
  const stateHeur = { config: { capture: { mode: "heuristics" } }, client };
  const g3 = await buildGroupDigest(stateHeur, [{ text: "one two three" }, { text: "four five" }, { text: "six" }], 500, "fact", new Set());
  check("heuristics-no-llm-call", g3 && g3.llm === false && seenUrls.length === 0,
    `llm=${g3?.llm} llmCalls=${seenUrls.length}`);

  // 6. withTimeout is a scoped view: original client keeps its default
  const scoped = client.withTimeout(300000);
  check("withtimeout-scoped", client.timeoutMs === 60000 && scoped.timeoutMs === 300000,
    `base=${client.timeoutMs} scoped=${scoped.timeoutMs}`);
} finally {
  globalThis.fetch = origFetch;
  AbortSignal.timeout = origTimeout;
}

const failed = results.filter((ok) => !ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
