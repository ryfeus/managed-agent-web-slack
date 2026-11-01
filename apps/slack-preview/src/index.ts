import type { SQSEvent, Context } from "aws-lambda";
import { metric } from "@managed-agents/preview";
import { compose } from "./composition.js";
import { observe, parseHint } from "./worker.js";

export async function handler(event: SQSEvent, context: Context) {
  if (process.env.SLACK_PREVIEW_ENABLED !== "true" || process.env.PREVIEW_ENABLED !== "true") return;
  for (const record of event.Records) {
    const hint = parseHint(record.body);
    // FIFO group + deterministic send dedup serialize subscriptions; redelivery never restarts one.
    if (!hint || record.attributes.ApproximateReceiveCount !== "1" || record.attributes.MessageGroupId !== hint.bindingId) {
      metric("slack_preview_hint_dropped"); continue;
    }
    const budget = Math.min(120000, context.getRemainingTimeInMillis() - 5000);
    if (budget <= 0) return;
    const signal = AbortSignal.timeout(budget);
    let ports: Awaited<ReturnType<typeof compose>> | undefined;
    try { ports = await compose(process.env, signal); await observe(hint, ports, signal, budget); }
    catch { metric("slack_preview_unavailable", { taskId: hint.taskId }); }
    finally { try { await ports?.close(); } catch { metric("slack_preview_cleanup_error"); } }
  }
}
