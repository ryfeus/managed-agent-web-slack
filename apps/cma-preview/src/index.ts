import type { SQSEvent, Context } from "aws-lambda";
import { metric } from "@managed-agents/preview";
import { compose } from "./composition.js";
import { observe, parseHint } from "./observer.js";

export async function handler(event: SQSEvent, context: Context) {
  if (process.env.PREVIEW_ENABLED !== "true") return;
  for (const record of event.Records) {
    const hint = parseHint(record.body);
    if (!hint) { metric("preview_hint_dropped"); continue; }
    // Expected P1 failures are acknowledged. Infrastructure crashes have bounded SQS redelivery.
    try { await observe(hint, await compose(process.env), undefined, Math.min(120000, Math.max(1, context.getRemainingTimeInMillis() - 5000))); }
    catch { metric("preview_unavailable", { taskId: hint.taskId }); }
  }
}
