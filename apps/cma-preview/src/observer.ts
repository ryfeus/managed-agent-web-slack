import { createHash } from "node:crypto";
import { setTimeout as sleep } from "node:timers/promises";
import { MAX_PREVIEW_BYTES, metric, type Preview, type PreviewPublisher } from "@managed-agents/preview";

export type Hint = { v: 1; taskId: string; contextId: string; sessionId: string; phaseId: string; createdAt: number };
export function parseHint(raw: string): Hint | undefined {
  if (Buffer.byteLength(raw) > 4096) return;
  try {
    const h = JSON.parse(raw) as Hint;
    if (h.v !== 1 || ![h.taskId, h.contextId, h.sessionId, h.phaseId].every(x => typeof x === "string" && x.length > 0 && x.length < 256)
      || !Number.isSafeInteger(h.createdAt) || h.createdAt > Date.now() + 5000 || Date.now() - h.createdAt > 30000) return;
    return h;
  } catch { return; }
}
export type ObserverPorts = {
  events: (sessionId: string, signal: AbortSignal) => AsyncIterable<unknown>;
  eligible: (hint: Hint, signal: AbortSignal) => Promise<boolean>;
  publish: PreviewPublisher;
};

/** Entirely disposable: this module cannot send, confirm, or write Task state. */
export async function observe(hint: Hint, ports: ObserverPorts, external?: AbortSignal, durationMs = 120000) {
  const abort = new AbortController();
  const signal = AbortSignal.any([abort.signal, AbortSignal.timeout(durationMs), ...(external ? [external] : [])]);
  const queue: Preview[] = [];
  const messages = new Map<string, { id: string; offset: number; pending: string; index: number; ended: boolean }>();
  let bytes = 0, frames = 0, starts = 0, deltas = 0, finalMessages = 0;
  function enqueue(p: Preview) {
    if (queue.length >= 32) { abort.abort(); return; }
    queue.push(p);
  }
  function flush() {
    for (const state of messages.values()) if (state.pending) {
      enqueue({ v: 1, taskId: hint.taskId, previewId: state.id, offset: state.offset++, type: "delta", text: state.pending }); state.pending = "";
    }
  }
  function end(state: { id: string; offset: number; pending: string; ended: boolean }) {
    if (state.ended) return;
    flush(); state.ended = true;
    enqueue({ v: 1, taskId: hint.taskId, previewId: state.id, offset: state.offset++, type: "end" });
  }
  let publisher: Promise<void> | undefined, monitor: Promise<void> | undefined;
  try {
    if (!await ports.eligible(hint, signal)) return;
    publisher = (async () => {
      while (!signal.aborted) {
        await sleep(100, undefined, { signal }); flush();
        while (queue.length && !signal.aborted) { const batch = queue.splice(0, 5); await ports.publish(batch, signal); frames += batch.length; }
      }
    })().catch(() => { if (!signal.aborted) metric("preview_publish_failed", { taskId: hint.taskId }); abort.abort(); });
    monitor = (async () => {
      while (!signal.aborted) {
        await sleep(2000, undefined, { signal });
        if (!await ports.eligible(hint, signal)) { abort.abort(); return; }
      }
    })().catch(() => abort.abort());
    for await (const original of ports.events(hint.sessionId, signal)) {
      if (signal.aborted) break;
      const event = original as any;
      if (event?.type === "event_start" && event.event?.type === "agent.message" && typeof event.event.id === "string") {
        starts++;
        if (messages.has(event.event.id)) continue;
        if (messages.size >= 32) break;
        const id = createHash("sha256").update(`${hint.taskId}\0${hint.phaseId}\0${event.event.id}`).digest("hex");
        messages.set(event.event.id, { id, offset: 1, pending: "", index: 0, ended: false });
        enqueue({ v: 1, taskId: hint.taskId, previewId: id, offset: 0, type: "start" });
      } else if (event?.type === "event_delta") {
        deltas++;
        const state = messages.get(event.event_id), delta = event.delta;
        if (!state || state.ended || delta?.type !== "content_delta" || delta.content?.type !== "text" || typeof delta.content.text !== "string") continue;
        // The official SDK permits omitted indexes for the first content block.
        const index = delta.index === undefined ? 0 : delta.index;
        if (!Number.isInteger(index) || index < state.index || index > 31) { end(state); continue; }
        state.index = index;
        // Split on code points, retaining valid UTF-8 while bounding serialized frames.
        for (const char of delta.content.text) {
          bytes += Buffer.byteLength(char);
          if (bytes > MAX_PREVIEW_BYTES) { abort.abort(); break; }
          state.pending += char;
          if (Buffer.byteLength(state.pending) >= 4000) flush();
        }
      } else if (event?.type === "agent.message") {
        finalMessages++;
        const state = messages.get(event.id); if (state) end(state);
      } else if (event?.type === "span.model_request_end") {
        for (const state of messages.values()) end(state);
      } else if (["session.status_idle", "session.status_terminated", "session.deleted", "session.error", "user.interrupt", "user.message"].includes(event?.type)) {
        break;
      }
    }
    // End markers are optional; bounded drain improves smoothness but has no P0 meaning.
    if (!signal.aborted) {
      for (const state of messages.values()) end(state);
      await sleep(150, undefined, { signal });
    }
  } catch { if (!signal.aborted) metric("preview_reader_failed", { taskId: hint.taskId }); }
  finally {
    abort.abort(); await Promise.allSettled([publisher, monitor]);
    metric("preview_reader_stopped", { taskId: hint.taskId, bytes, frames, starts, deltas, finalMessages });
  }
}
