import type { Projection } from "./repository.js";

export type UpdateResult = "ok" | "rate_limited" | "stream_closed" | "unavailable";
export interface SlackTitleUpdater {
  update(row: Projection, title: string, signal: AbortSignal): Promise<UpdateResult>;
}

/** Plain, bounded title only. Remove mention/markup syntax before taking a Unicode suffix. */
export function draftTitle(text: string): string {
  const plain = text.replace(/[\p{Cc}\p{Cf}]/gu, " ").replace(/[<>&@*_~`|]/g, " ").replace(/\s+/gu, " ").trim();
  return plain ? `Draft: …${Array.from(plain).slice(-140).join("")}` : "";
}

/** Deliberately no start/stop/post/update-message or session methods, and no retries. */
export function slackTitleUpdater(token: string, endpoint = "https://slack.com/api/chat.appendStream"): SlackTitleUpdater {
  if (endpoint !== "https://slack.com/api/chat.appendStream" && !["127.0.0.1", "localhost"].includes(new URL(endpoint).hostname)) throw new Error("Invalid Slack fixture endpoint");
  return {
    async update(row, title, signal) {
      try {
        const response = await fetch(endpoint, { method: "POST", redirect: "error",
          headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
          signal: AbortSignal.any([signal, AbortSignal.timeout(3000)]),
          body: JSON.stringify({ channel: row.external_thread_id.split(":")[0], ts: row.slack_message_ts,
            chunks: [{ type: "task_update", id: `request:${row.request_event_id || row.task_id}`, status: "in_progress", title }] }) });
        if (response.status === 429) { await response.body?.cancel(); return "rate_limited"; }
        if (!response.ok) { await response.body?.cancel(); return "unavailable"; }
        // The API returns a tiny acknowledgment; never retain/log a raw response body.
        const reader = response.body?.getReader();
        if (!reader) return "unavailable";
        let body = "";
        try {
          for (;;) {
            const chunk = await reader.read(); if (chunk.done) break;
            body += Buffer.from(chunk.value).toString("utf8");
            if (Buffer.byteLength(body) > 8192) return "unavailable";
          }
        } finally { await reader.cancel(); }
        const result = JSON.parse(body);
        if (result.ok === true) return "ok";
        if (["ratelimited", "rate_limited"].includes(result.error)) return "rate_limited";
        return result.error === "message_not_in_streaming_state" ? "stream_closed" : "unavailable";
      } catch { return "unavailable"; }
    }
  };
}
