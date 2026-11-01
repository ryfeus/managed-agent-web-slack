import { EventType as E, type BaseEvent, type Message } from "@ag-ui/core";
import { Role, TaskState } from "@a2a-js/sdk";
import { PreviewAccumulator, metric, type Preview, type PreviewSubscription } from "@managed-agents/preview";
import { finish, history, interrupts, text } from "./mapper.js";
import type { ThreadService } from "./service.js";

export async function streamTask(options: {
  service: ThreadService; threadId: string; taskId: string; runId: string;
  write: (event: BaseEvent | string) => Promise<void>; signal: AbortSignal;
  subscribe?: PreviewSubscription; deadlineMs?: number; resumedIds?: string[];
}) {
  const { service, threadId, taskId, runId, write } = options;
  const lifetime = new AbortController();
  const deadlineMs = Math.min(options.deadlineMs ?? 840000, 840000);
  const expiry = AbortSignal.timeout(deadlineMs);
  const signal = AbortSignal.any([options.signal, lifetime.signal, expiry]);
  const previewAbort = new AbortController();
  const previewSignal = AbortSignal.any([signal, previewAbort.signal]);
  const deadline = Date.now() + deadlineMs;
  const previews: Preview[] = [], accumulator = new PreviewAccumulator();
  let wake: (() => void) | undefined, changed = true, lastGet = 0, lastHeartbeat = 0, failures = 0;
  let provisional = false, canonicalKey = "", acceptedResume = false;
  let lastCanonical: Message[] = [];
  const startedAt = Date.now();
  const seen = new Set<string>();
  const notify = () => { wake?.(); wake = undefined; };
  const stopPreview = () => { previews.length = 0; previewAbort.abort(); };
  const snapshot = async (messages: Message[]) => { lastCanonical = messages; await write({ type: E.MESSAGES_SNAPSHOT, messages }); };
  const reconnect = async () => {
    stopPreview(); await snapshot(lastCanonical);
    await write({ type: E.RUN_ERROR, code: "STREAM_RECONNECT_REQUIRED", message: "Reconnect to continue following this task." });
  };
  let previewWork: Promise<void> | undefined;
  const startPreview = () => options.subscribe?.(taskId, p => {
    if (previews.length >= 32) { stopPreview(); changed = true; }
    else previews.push(p);
    notify();
  }, previewSignal).catch(() => { metric("preview_unavailable", { taskId }); });
  // A notification only wakes a canonical read. It never commits UI terminal state.
  let watch = Promise.resolve();
  const startWatch = async () => {
    while (!signal.aborted) {
      try {
        for await (const notification of service.a2a.subscribe(taskId, signal)) { if (notification) { changed = true; notify(); } }
      } catch { /* Reconcile closure and reconnect; a stream is not history. */ }
      changed = true; notify();
      await delay(500, signal);
    }
  };
  async function delay(ms: number, abort: AbortSignal) {
    if (abort.aborted) return;
    await new Promise<void>(resolve => {
      const done = () => { clearTimeout(timer); abort.removeEventListener("abort", done); resolve(); };
      const timer = setTimeout(done, ms); abort.addEventListener("abort", done, { once: true });
    });
  }
  try {
    await write({ type: E.RUN_STARTED, threadId, runId });
    // History precedes subscriptions' observable output, including on ordinary sends.
    const initial = await history(service, threadId, signal);
    const initialMessages = options.resumedIds?.length ? initial.messages.filter(m => m.id !== `pending-${taskId}`) : initial.messages;
    await snapshot(initialMessages);
    for (const m of initialMessages) if (m.role === "assistant") seen.add(m.id);
    watch = startWatch();
    previewWork = startPreview();
    while (!signal.aborted) {
      if (Date.now() >= deadline) {
        await reconnect();
        return;
      }
      if ((changed && Date.now() - lastGet >= 200) || Date.now() - lastGet >= 2000) {
        changed = false; lastGet = Date.now();
        try {
          const task = await service.get(threadId, taskId, signal);
          failures = 0;
          const pendingIds = interrupts(task).map(i => i.id);
          const waitingResume = options.resumedIds?.length && !acceptedResume && task.status?.state === TaskState.TASK_STATE_INPUT_REQUIRED
            && pendingIds.every(id => options.resumedIds!.includes(id));
          if (!waitingResume) acceptedResume = true;
          const terminal = waitingResume ? undefined : finish(task, threadId, runId);
          const key = JSON.stringify([task.history.map(m => [m.messageId, text(m)]), task.status?.state, pendingIds]);
          if (key !== canonicalKey || terminal) {
            canonicalKey = key;
            // Canonical output replaces all provisional content, never appends to it.
            if (provisional || terminal) {
              provisional = false;
              await snapshot((await history(service, threadId, signal)).messages.filter(m => !waitingResume || m.id !== `pending-${taskId}`));
            } else {
              for (const m of task.history) if (m.role === Role.ROLE_AGENT && !seen.has(m.messageId) && text(m)) {
                await write({ type: E.TEXT_MESSAGE_START, messageId: m.messageId, role: "assistant" });
                await write({ type: E.TEXT_MESSAGE_CONTENT, messageId: m.messageId, delta: text(m) });
                await write({ type: E.TEXT_MESSAGE_END, messageId: m.messageId });
              }
            }
            for (const m of task.history) seen.add(m.messageId);
          }
          if (terminal) {
            stopPreview();
            for (const event of terminal) await write(event);
            return;
          }
          if (waitingResume && Date.now() - startedAt > 20000) {
            await reconnect(); return;
          }
        } catch (error) {
          if (signal.aborted) break;
          metric("web_canonical_read_failed", { taskId, errorType: error instanceof Error ? error.name : "unknown" });
          if (++failures >= 5) {
            await reconnect(); return;
          }
        }
      }
      for (const p of previews.splice(0)) {
        const update = accumulator.accept(p);
        if (!update) continue;
        provisional = true;
        if (update.start) await write({ type: E.TEXT_MESSAGE_START, messageId: update.id, role: "assistant" });
        if (p.type === "delta") await write({ type: E.TEXT_MESSAGE_CONTENT, messageId: update.id, delta: p.text });
        if (update.end) await write({ type: E.TEXT_MESSAGE_END, messageId: update.id });
      }
      if (Date.now() - lastHeartbeat >= 15000) { await write(": heartbeat\n\n"); lastHeartbeat = Date.now(); }
      await Promise.race([new Promise<void>(resolve => { wake = resolve; }), delay(200, signal)]);
      wake = undefined;
    }
    if (expiry.aborted && !options.signal.aborted) await reconnect();
  } finally {
    lifetime.abort(); stopPreview(); notify();
    await Promise.allSettled([watch, previewWork]);
  }
}
