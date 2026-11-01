import { it, expect } from "vitest";
import { setTimeout as sleep } from "node:timers/promises";
import { observe, parseHint, type Hint, type ObserverPorts } from "../src/observer.js";
import type { Preview } from "@managed-agents/preview";
const hint: Hint = { v: 1, taskId: "task", contextId: "context", phaseId: "phase", sessionId: "private-session", createdAt: Date.now() };
const start = { type: "event_start", event: { type: "agent.message", id: "private-event" } };
const delta = (text: string, indexed = true) => ({ type: "event_delta", event_id: "private-event", delta: { type: "content_delta", ...(indexed ? { index: 0 } : {}), content: { type: "text", text } } });
it.each([true, false])("batches real-shaped deltas (indexed %s), omits thinking/tools, and stops at requires_action", async indexed => {
  const frames: Preview[] = [];
  const ports: ObserverPorts = { eligible: async () => true, publish: async batch => { frames.push(...batch); }, async *events() {
    yield { type: "event_start", event: { type: "agent.thinking", id: "secret" } };
    yield { ...delta("hidden"), event_id: "secret" };
    yield { type: "agent.tool_use", input: { secret: "never" } };
    yield start; yield start; yield delta("hello ", indexed); yield delta("world", indexed);
    await sleep(120);
    yield { type: "agent.message", id: "private-event", content: [{ type: "text", text: "canonical corrected" }] };
    yield { type: "session.status_idle", stop_reason: { type: "requires_action" } };
    throw new Error("must not read beyond idle");
  } };
  await observe(hint, ports);
  expect(frames.map(f => f.text || "").join("")).toBe("hello world");
  expect(frames.map(f => f.offset)).toEqual([0, 1, 2]);
  expect(JSON.stringify(frames)).not.toMatch(/private|thinking|canonical|hidden|never/);
});
it("aborts the upstream reader on publish outage or deadline and does not retry execution", async () => {
  for (const failPublish of [false, true]) {
    let closed = 0, opened = 0;
    await observe(hint, { eligible: async () => true, publish: async () => { if (failPublish) throw new Error("outage body must not log"); }, async *events(_id, signal) {
      opened++;
      try { yield start; yield delta("preview"); await sleep(10000, undefined, { signal }); }
      finally { closed++; }
    } }, undefined, 250);
    expect(opened).toBe(1); expect(closed).toBe(1);
  }
});
it("drops expired/invalid SQS hints and stale Tasks without opening provider SSE", async () => {
  expect(parseHint(JSON.stringify({ ...hint, createdAt: Date.now() - 31000 }))).toBeUndefined();
  expect(parseHint("{")).toBeUndefined();
  let opened = false;
  await observe(hint, { eligible: async () => false, publish: async () => {}, async *events() { opened = true; yield start; } });
  expect(opened).toBe(false);
});

it("truncated streams are disposable and resumed phases have distinct opaque preview IDs", async () => {
  const frames: Preview[][] = [];
  for (const phaseId of ["before-review", "after-review"]) {
    const phase: Preview[] = []; frames.push(phase);
    await observe({ ...hint, phaseId }, { eligible: async () => true, publish: async batch => { phase.push(...batch); }, async *events() {
      yield delta("missing-start-is-discarded"); yield start; yield delta("partial"); await sleep(130);
      throw new Error("truncated upstream");
    } });
  }
  expect(frames[0].map(p => p.text || "").join("")).toBe("partial");
  expect(frames[0][0].previewId).not.toBe(frames[1][0].previewId);
});
