import { it, expect, vi, afterEach } from "vitest";
import { observe, parseHint, type Ports } from "../src/worker.js";
import { draftTitle, slackTitleUpdater } from "../src/slack.js";
import type { Projection } from "../src/repository.js";
import type { Preview } from "@managed-agents/preview";

// Node's promise timers retain native timers; use the same abort semantics with
// Vitest's clock so the real 2s cadence/40-write budget is exercised deterministically.
vi.mock("node:timers/promises", () => ({ setTimeout: (ms: number, _value: unknown, options: { signal: AbortSignal }) => new Promise<void>((resolve, reject) => {
  const abort = () => { clearTimeout(timer); reject(new Error("aborted")); };
  const timer = setTimeout(() => { options.signal.removeEventListener("abort", abort); resolve(); }, ms);
  options.signal.addEventListener("abort", abort, { once: true });
  if (options.signal.aborted) abort();
}) }));

const bindingId = "11111111-1111-4111-8111-111111111111", taskId = "task-1", previewId = "a".repeat(64);
const hint = () => ({ v: 1 as const, bindingId, taskId, createdAt: Date.now() });
const frame = (offset: number, text?: string): Preview => ({ v: 1, taskId, previewId, offset,
  type: offset === 0 ? "start" : text ? "delta" : "end", ...(text ? { text } : {}) });
function fixture() {
  const row: Projection = { binding_id: bindingId, task_id: taskId, slack_message_ts: `${Math.floor(Date.now() / 1000)}.000001`,
    external_thread_id: "C123:100.001", request_event_id: "Ev1", status: "pending", last_task_state: "WORKING" };
  const abort = new AbortController();
  let receive: (p: Preview) => void = () => {}, closed = false;
  const ports: Ports = {
    projections: { read: vi.fn(async () => row), claim: vi.fn(async () => "token"), release: vi.fn(async () => true) },
    slack: { update: vi.fn(async () => "ok" as const) },
    subscribe: vi.fn(async (_task, callback, signal) => {
      receive = callback;
      await new Promise<void>(resolve => signal.addEventListener("abort", () => { closed = true; resolve(); }, { once: true }));
    })
  };
  return { row, ports, abort, send: (p: Preview) => receive(p), closed: () => closed };
}
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("accepts only bounded, fresh identifier-only hints", () => {
  const valid = hint(); expect(parseHint(JSON.stringify(valid))).toEqual(valid);
  for (const value of [null, { ...valid, channel: "Cother" }, { ...valid, createdAt: Date.now() - 31000 },
    { ...valid, createdAt: Date.now() + 2000 }, { ...valid, taskId: "../elsewhere" }, { ...valid, bindingId: "bad" },
    { ...valid, body: "x".repeat(2000) }]) expect(parseHint(JSON.stringify(value))).toBeUndefined();
});
it("keeps Unicode suffixes plain, single-line and bounded without Slack mentions", () => {
  const title = draftTitle("\u0000\n<@U123> <!channel> & *bold*\u202e" + "😀é".repeat(200));
  expect(Array.from(title)).toHaveLength(148);
  expect(title).toBe("Draft: …" + "😀é".repeat(70));
  expect(draftTitle("<@U123>\n<!channel> @here <https://host|label> &\t*")).not.toMatch(/[<>&@\n\t*]/);
  expect(draftTitle("\n\u0000 ")).toBe("");
});
it("coalesces frames every two seconds, deduplicates and discards gaps/private fields", async () => {
  vi.useFakeTimers(); const f = fixture(), work = observe(hint(), f.ports, f.abort.signal);
  await vi.advanceTimersByTimeAsync(0);
  f.send(frame(0)); f.send(frame(1, "First")); f.send(frame(1, "First"));
  await vi.advanceTimersByTimeAsync(1999); expect(f.ports.slack.update).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1); expect(f.ports.slack.update).toHaveBeenCalledTimes(1);
  expect(vi.mocked(f.ports.slack.update).mock.calls[0][1]).toBe("Draft: …First");
  f.send({ ...frame(2, "SECRET"), thinking: true } as Preview);
  f.send(frame(4, "gap")); f.send(frame(5, "suffix"));
  await vi.advanceTimersByTimeAsync(4000); expect(f.ports.slack.update).toHaveBeenCalledTimes(1);
  f.abort.abort(); await work; expect(f.closed()).toBe(true);
  expect(f.ports.projections.release).toHaveBeenCalledWith(f.row, "token");
});
it.each(["rate_limited", "stream_closed", "unavailable"] as const)("stops on %s, releases the claim and never retries an ambiguous write", async result => {
  vi.useFakeTimers(); const f = fixture(); vi.mocked(f.ports.slack.update).mockResolvedValue(result);
  const work = observe(hint(), f.ports, f.abort.signal); await vi.advanceTimersByTimeAsync(0);
  f.send(frame(0)); f.send(frame(1, "optional")); await vi.advanceTimersByTimeAsync(2000); await work;
  expect(f.ports.slack.update).toHaveBeenCalledTimes(1); expect(f.ports.projections.release).toHaveBeenCalledTimes(1); expect(f.closed()).toBe(true);
});
it.each(["COMPLETED", "INPUT_REQUIRED", "CANCELED", "FAILED", "REJECTED"])("never appends after %s settlement", async state => {
  vi.useFakeTimers(); const f = fixture(), work = observe(hint(), f.ports, f.abort.signal);
  await vi.advanceTimersByTimeAsync(0); f.send(frame(0)); f.send(frame(1, "draft")); f.row.last_task_state = state;
  await vi.advanceTimersByTimeAsync(2000); await work;
  expect(f.ports.slack.update).not.toHaveBeenCalled(); expect(f.closed()).toBe(true);
});
it("skips busy claims, abandons lost claims and refuses delayed claim acknowledgments", async () => {
  vi.useFakeTimers(); const f = fixture();
  // A P0 push may briefly own the row immediately after the card-start hint.
  // Subscription is safe; writes still require the pending-only atomic claim.
  f.row.status = "streaming";
  vi.mocked(f.ports.projections.claim).mockResolvedValueOnce(undefined);
  vi.mocked(f.ports.projections.release).mockResolvedValue(false);
  const work = observe(hint(), f.ports, f.abort.signal); await vi.advanceTimersByTimeAsync(0);
  f.send(frame(0)); f.send(frame(1, "draft")); await vi.advanceTimersByTimeAsync(2000);
  expect(f.ports.slack.update).not.toHaveBeenCalled();
  f.row.status = "pending";
  await vi.advanceTimersByTimeAsync(2000); await work; expect(f.closed()).toBe(true);
  const slow = fixture();
  vi.mocked(slow.ports.projections.claim).mockImplementation(async () => { await new Promise(r => setTimeout(r, 2100)); return "late"; });
  const delayed = observe(hint(), slow.ports, slow.abort.signal); await vi.advanceTimersByTimeAsync(0);
  slow.send(frame(0)); slow.send(frame(1, "draft")); await vi.advanceTimersByTimeAsync(4100); await delayed;
  expect(slow.ports.slack.update).not.toHaveBeenCalled(); expect(slow.ports.projections.release).toHaveBeenCalledWith(slow.row, "late");
});
it("caps calls at forty even with continuously changing text", async () => {
  vi.useFakeTimers(); const f = fixture(), work = observe(hint(), f.ports, f.abort.signal);
  await vi.advanceTimersByTimeAsync(0); f.send(frame(0));
  for (let i = 1; i <= 40; i++) { f.send(frame(i, ` ${i}`)); await vi.advanceTimersByTimeAsync(2000); }
  await work; expect(f.ports.slack.update).toHaveBeenCalledTimes(40); expect(f.closed()).toBe(true);
});
it("rejects old/nonexistent streams and closes on AppSync loss or deadline", async () => {
  const old = fixture(); old.row.slack_message_ts = `${(Date.now() - 600000) / 1000}`;
  await observe(hint(), old.ports); expect(old.ports.subscribe).not.toHaveBeenCalled();
  const gone = fixture(); vi.mocked(gone.ports.projections.read).mockResolvedValue(undefined);
  await observe(hint(), gone.ports); expect(gone.ports.subscribe).not.toHaveBeenCalled();
  const lost = fixture(); vi.mocked(lost.ports.subscribe).mockResolvedValue();
  await observe(hint(), lost.ports); expect(lost.ports.slack.update).not.toHaveBeenCalled();
  const deadline = fixture(); await observe(hint(), deadline.ports, undefined, 10); expect(deadline.closed()).toBe(true);
});
it("HTTP gateway sends only an existing card title, uses a deadline, and never retries", async () => {
  const f = fixture(); const fetcher = vi.fn(async () => new Response('{"ok":true}'));
  vi.stubGlobal("fetch", fetcher);
  expect(await slackTitleUpdater("test").update(f.row, "Draft: …text", f.abort.signal)).toBe("ok");
  const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("https://slack.com/api/chat.appendStream"); expect(init.signal).toBeInstanceOf(AbortSignal);
  expect(JSON.parse(init.body as string)).toEqual({ channel: "C123", ts: f.row.slack_message_ts,
    chunks: [{ type: "task_update", id: "request:Ev1", status: "in_progress", title: "Draft: …text" }] });
  fetcher.mockResolvedValueOnce(new Response('', { status: 429 }));
  expect(await slackTitleUpdater("test").update(f.row, "draft", f.abort.signal)).toBe("rate_limited");
  fetcher.mockRejectedValueOnce(new Error("private response body"));
  expect(await slackTitleUpdater("test").update(f.row, "draft", f.abort.signal)).toBe("unavailable");
  expect(fetcher).toHaveBeenCalledTimes(3);
});
