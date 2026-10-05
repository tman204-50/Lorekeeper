// LLM shim — implements the OpenCode SDK client surface the vendor's llm.js
// needs (session.create / session.prompt / session.delete) over an
// OpenAI-compatible HTTP API (OpenRouter). This lets the fork's LLM capture
// and digest paths run unchanged, with no OpenCode SDK.
//
// The "session" here is a lightweight in-memory conversation: create() opens
// one, prompt() appends the user message and streams a completion with the
// system prompt, delete() closes it. This mirrors the SDK's ephemeral-session
// round trip the vendor code expects.

import { randomUUID } from "node:crypto";

const OPENROUTER_URL = "https://openrouter.ai/api/v1";

export class LLMSessionClient {
  constructor({ apiKey, baseUrl, model, timeoutMs = 60000 }) {
    this.apiKey = apiKey;
    this.baseUrl = (baseUrl || OPENROUTER_URL).replace(/\/+$/, "");
    this.model = model;
    this.timeoutMs = timeoutMs;
    this.sessions = new Map(); // id -> [{role, content}]
    this._signal = undefined; // optional per-call abort (see withSignal)
  }

  // Scoped view of this client whose _chat aborts when `signal` fires (the
  // session create/prompt/delete surface is fixed by the vendor, so the
  // signal rides on a derived instance instead of each call).
  withSignal(signal) {
    return Object.create(this, { _signal: { value: signal, enumerable: false } });
  }

  // Scoped view with a different per-call hard timeout (DIGEST_TIMEOUT 0.2.5:
  // digest prompts over large groups can legitimately run minutes, so the
  // digest path calls client.withTimeout(300000); capture keeps the 60s
  // default plus its own external 90s abort). Duck-typed by vendor llm.js —
  // a client without withTimeout keeps its default behavior.
  withTimeout(timeoutMs) {
    return Object.create(this, { timeoutMs: { value: timeoutMs, enumerable: false } });
  }

  async _chat(messages, system) {
    const body = {
      model: this.model,
      messages: system ? [{ role: "system", content: system }, ...messages] : messages,
      temperature: 0.2,
    };
    const signals = [AbortSignal.timeout(this.timeoutMs)];
    if (this._signal) signals.push(this._signal);
    const resp = await fetch(`${this.baseUrl}/chat/completions`, {
      method: "POST",
      // Hard timeout + optional external abort: never let a hung OpenRouter
      // call wedge the event loop, and stop paying for generations the caller
      // already gave up on.
      signal: signals.length > 1 ? AbortSignal.any(signals) : signals[0],
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${this.apiKey}`,
        // OpenRouter app attribution (Hermes sends the same trio):
        "user-agent": `lorekeeper/1.0 (Hermes memory provider)`,
        "http-referer": "https://github.com/tman204-50/Lorekeeper",
        "x-title": "Lorekeeper",
      },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`OpenRouter chat/completions failed: HTTP ${resp.status}: ${text.slice(0, 300)}`);
    }
    const data = await resp.json();
    return data;
  }

  get session() {
    return {
      create: async ({ body }) => {
        const id = randomUUID();
        this.sessions.set(id, []);
        return { data: { id } };
      },
      prompt: async ({ path, body }) => {
        const { id } = path;
        const messages = this.sessions.get(id);
        if (!messages) throw new Error(`unknown session: ${id}`);
        const userText = body?.parts?.find?.((p) => p?.type === "text")?.text ?? "";
        messages.push({ role: "user", content: userText });
        const system = body?.system ?? "";
        const resp = await this._chat(messages, system);
        const choice = resp?.choices?.[0];
        const text = choice?.message?.content ?? choice?.text ?? "";
        // Match the shape extractAssistantText() expects: { data: { parts: [{type:"text", text}] } }
        return { data: { parts: [{ type: "text", text }] } };
      },
      delete: async ({ path }) => {
        this.sessions.delete(path.id);
        return {};
      },
    };
  }
}