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
  }

  async _chat(messages, system) {
    const body = {
      model: this.model,
      messages: system ? [{ role: "system", content: system }, ...messages] : messages,
      temperature: 0.2,
    };
    const resp = await fetch(`${this.baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${this.apiKey}`,
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