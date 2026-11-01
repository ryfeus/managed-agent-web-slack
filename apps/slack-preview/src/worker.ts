import { setTimeout as sleep } from "node:timers/promises";
import { metric, parsePreview, PreviewAccumulator, type PreviewSubscription } from "@managed-agents/preview";
import type { Projections, Projection } from "./repository.js";
import { draftTitle, type SlackTitleUpdater } from "./slack.js";

export type Hint = { v: 1; bindingId: string; taskId: string; createdAt: number };
export type Ports = { projections: Projections; slack: SlackTitleUpdater; subscribe: PreviewSubscription };
export function parseHint(body: string, now = Date.now()): Hint | undefined {
  if (Buffer.byteLength(body) > 1024) return;
  try {
    const h = JSON.parse(body);
    if (!h || Object.keys(h).sort().join() !== "bindingId,createdAt,taskId,v" || h.v !== 1
      || typeof h.bindingId !== "string" || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(h.bindingId)
      || typeof h.taskId !== "string" || !/^[a-zA-Z0-9_-]{1,128}$/.test(h.taskId)
      || !Number.isSafeInteger(h.createdAt) || now - h.createdAt > 30000 || h.createdAt > now + 1000) return;
    return h;
  } catch { return; }
}
function usable(row: Projection | undefined): row is Projection {
  if (!row || !["pending", "streaming"].includes(row.status) || !["SUBMITTED", "WORKING"].includes(row.last_task_state)
    || !/^[CDG][A-Z0-9]+:\d+\.\d+$/.test(row.external_thread_id) || !/^\d+\.\d+$/.test(row.slack_message_ts)) return false;
  const age = Date.now() - Number(row.slack_message_ts) * 1000;
  // Leave time for the HTTP call before Slack's native ten-minute finalization.
  return age >= -1000 && age < 595000;
}

export async function observe(hint: Hint, ports: Ports, parent?: AbortSignal, budgetMs = 120000) {
  const abort = new AbortController();
  const signal = AbortSignal.any([abort.signal, AbortSignal.timeout(Math.max(1, Math.min(budgetMs, 120000))), ...(parent ? [parent] : [])]);
  const started = Date.now(), accumulator = new PreviewAccumulator();
  let subscription: Promise<void> | undefined, title = "", displayed = "", updates = 0;
  try {
    const initial = await ports.projections.read(hint.bindingId, hint.taskId);
    if (!usable(initial) || signal.aborted) return;
    metric("slack_preview_started", { taskId: hint.taskId, active: 1 });
    subscription = ports.subscribe(hint.taskId, raw => {
      if (signal.aborted) return;
      const frame = parsePreview(raw, hint.taskId); if (!frame) return;
      const accepted = accumulator.accept(frame);
      if (accepted) title = draftTitle(accepted.text);
    }, signal).catch(() => { metric("slack_preview_subscription_error", { taskId: hint.taskId }); }).finally(() => abort.abort());
    while (!signal.aborted && updates < 40) {
      await sleep(2000, undefined, { signal });
      const row = await ports.projections.read(hint.bindingId, hint.taskId);
      if (!usable(row) || row.slack_message_ts !== initial.slack_message_ts || signal.aborted) break;
      if (!title || title === displayed) continue;
      const claimStarted = performance.now();
      const token = await ports.projections.claim(row);
      if (!token) { metric("slack_preview_claim_skipped", { taskId: hint.taskId }); continue; }
      try {
        // Includes query latency: a delayed claim response never authorizes a late HTTP write.
        if (signal.aborted || performance.now() - claimStarted > 2000) break;
        displayed = title; updates++;
        metric("slack_preview_update", { taskId: hint.taskId });
        const result = await ports.slack.update(row, displayed, signal);
        if (result !== "ok") { metric(`slack_preview_${result}`, { taskId: hint.taskId }); break; }
      } finally {
        if (!await ports.projections.release(row, token)) abort.abort();
      }
    }
  } catch { if (!signal.aborted) metric("slack_preview_error", { taskId: hint.taskId }); }
  finally {
    abort.abort(); await subscription;
    metric("slack_preview_stopped", { taskId: hint.taskId, updates, elapsedMs: Date.now() - started, active: 0 });
  }
}
