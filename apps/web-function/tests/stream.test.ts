import { it, expect, vi } from "vitest";
import { setTimeout as sleep } from "node:timers/promises";
import { Task, TaskState as S, Role, Message } from "@a2a-js/sdk";
import { streamTask } from "../src/stream.js";
import { finish, history, text } from "../src/mapper.js";
import { ThreadService } from "../src/service.js";
import { HUMAN_INPUT, type A2A } from "../src/a2a.js";
import type { Threads } from "../src/repository.js";
import type { PreviewSubscription } from "../../preview/src/index.js";

function fixture() {
  const task = Task.fromJSON({ id: "task", contextId: "context", status: { state: S.TASK_STATE_WORKING }, history: [{ messageId: "user", role: Role.ROLE_USER, parts: [{ text: "question" }] }] });
  const calls: string[] = [];
  const a2a = { get: vi.fn(async () => structuredClone(task)), list: vi.fn(async () => [structuredClone(task)]), cancel: vi.fn(),
    async *subscribe(_id: string, signal: AbortSignal) { calls.push("subscribe"); try { await sleep(10000, undefined, { signal }); yield {} as any; } catch { calls.push("closed"); } } } as unknown as A2A;
  const threads = { get: async () => ({ thread_id: "thread", agent_id: "cma", context_id: "context" }), binding: async () => ({ thread_id: "thread" }) } as unknown as Threads;
  const service = new ThreadService(threads, a2a, "http://sink.test");
  return { task, calls, service, a2a };
}

it.each([false, true])("canonical terminal snapshot replaces previews (preview enabled %s)", async enabled => {
  const { task, calls, service, a2a } = fixture();
  const events: any[] = []; let cleaned = false;
  const subscribe: PreviewSubscription = async (taskId, receive, signal) => {
    const p = { v: 1 as const, taskId, previewId: "a".repeat(64) };
    receive({ ...p, type: "start", offset: 0 }); receive({ ...p, type: "delta", offset: 1, text: "wrong preview" });
    receive({ ...p, type: "delta", offset: 1, text: "duplicate" });
    receive({ ...p, type: "end", offset: 2 }); // Does not finish the Task.
    try { await sleep(10000, undefined, { signal }); } catch { cleaned = true; }
  };
  const running = streamTask({ service, threadId: "thread", taskId: "task", runId: "run", signal: new AbortController().signal,
    subscribe: enabled ? subscribe : undefined, write: async e => { if (typeof e !== "string") events.push(e); } });
  await sleep(250);
  expect(events.some(e => e.type === "RUN_FINISHED")).toBe(false);
  expect(events.filter(e => e.type === "TEXT_MESSAGE_CONTENT").map(e => e.delta)).toEqual(enabled ? ["wrong preview"] : []);
  task.history.push(Message.fromJSON({ messageId: "canonical", role: Role.ROLE_AGENT, parts: [{ text: "Exact answer" }] }));
  task.status!.state = S.TASK_STATE_COMPLETED;
  await running;
  expect(events[0]).toMatchObject({ type: "RUN_STARTED", runId: "run" });
  expect(events.at(-2)).toMatchObject({ type: "MESSAGES_SNAPSHOT", messages: [{ id: "user" }, { id: "canonical", content: "Exact answer" }] });
  expect(events.at(-1)).toMatchObject({ type: "RUN_FINISHED", outcome: { type: "success" } });
  expect(calls.at(-1)).toBe("closed"); expect(a2a.cancel).not.toHaveBeenCalled();
  if (enabled) expect(cleaned).toBe(true);
});

it("deadline and client abort clean up canonical watch without cancellation", async () => {
  for (const disconnect of [false, true]) {
    const { service, calls, a2a } = fixture(), events: any[] = [], abort = new AbortController();
    const running = streamTask({ service, threadId: "thread", taskId: "task", runId: "run", signal: abort.signal, deadlineMs: 60, write: async e => { events.push(e); } });
    if (disconnect) { await sleep(20); abort.abort(); }
    await running;
    expect(calls).toEqual(["subscribe", "closed"]); expect(a2a.cancel).not.toHaveBeenCalled();
    expect(events.some(e => e.type === "RUN_FINISHED")).toBe(false);
    if (!disconnect) expect(events.at(-1).code).toBe("STREAM_RECONNECT_REQUIRED");
  }
});

it("maps canonical lifecycle, multiline messages and earliest human input", async () => {
  const { service, task } = fixture();
  expect(text(Message.fromJSON({ parts: [{ text: "Hello" }, { text: "world" }] }))).toBe("Hello\nworld");
  task.status = { state: S.TASK_STATE_INPUT_REQUIRED, timestamp: undefined, message: Message.fromJSON({ extensions: [HUMAN_INPUT], metadata: { [HUMAN_INPUT]: { requests: [{ kind: "tool_approval", requestId: "review", tool: { name: "browser", arguments: { url: "https://example.test" } }, allowedResponses: ["allow", "deny"] }] } } }) };
  const result = await history(service, "thread");
  expect(result).toMatchObject({ activeTaskId: "task", activeTaskState: "TASK_STATE_INPUT_REQUIRED", pendingRequestIds: ["review"] });
  expect(result.messages.at(-1)?.id).toBe("pending-task");
  expect(finish(task, "thread", "run")?.at(-1)).toMatchObject({ outcome: { type: "interrupt" } });
  for (const state of [S.TASK_STATE_CANCELED, S.TASK_STATE_FAILED, S.TASK_STATE_REJECTED]) {
    task.status.state = state; expect(finish(task, "thread", "run")?.[0].type).toBe("RUN_ERROR");
  }
});

it("rejects task substitution and mismatched context before any cancel or answer", async () => {
  const { service, task, a2a } = fixture();
  vi.spyOn(service.threads, "binding").mockResolvedValue({ thread_id: "foreign" } as any);
  await expect(service.get("thread", "task")).rejects.toThrow("not bound"); expect(a2a.get).not.toHaveBeenCalled();
  vi.spyOn(service.threads, "binding").mockResolvedValue({ thread_id: "thread" } as any);
  task.contextId = "foreign";
  await expect(service.cancel("thread")).rejects.toThrow("context mismatch"); expect(a2a.cancel).not.toHaveBeenCalled();
});
