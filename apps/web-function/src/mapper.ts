import { Role, TaskState, taskStateToJSON, type Task, type Message as A2AMessage } from "@a2a-js/sdk";
import { EventType as E, type BaseEvent, type Message, type Interrupt } from "@ag-ui/core";
import { z } from "zod";
import { HUMAN_INPUT } from "./a2a.js";
import type { ThreadService } from "./service.js";

const request = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("tool_approval"), requestId: z.string().min(1), tool: z.object({ name: z.string().min(1), arguments: z.record(z.string(), z.unknown()) }), allowedResponses: z.tuple([z.literal("allow"), z.literal("deny")]), message: z.string().nullable().optional() }),
  z.object({ kind: z.literal("clarification"), requestId: z.string().min(1), prompt: z.string().min(1), choices: z.array(z.string()).nullable().optional() })
]);
export function text(message: A2AMessage): string { return message.parts.filter(p => p.content?.$case === "text").map(p => p.content!.value).join("\n"); }
export function interrupts(task: Task): Interrupt[] {
  if (task.status?.state !== TaskState.TASK_STATE_INPUT_REQUIRED || !task.status.message?.extensions.includes(HUMAN_INPUT)) return [];
  const items = z.array(request).parse(task.status.message.metadata?.[HUMAN_INPUT]?.requests ?? []);
  return items.map(item => ({ id: item.requestId, reason: item.kind, message: item.kind === "tool_approval" ? item.message ?? undefined : item.prompt,
    ...(item.kind === "tool_approval" ? { toolCallId: item.requestId } : {}), metadata: item }));
}
export function pendingMessage(task: Task): Message | undefined {
  const pending = interrupts(task);
  if (!pending.length) return;
  const tools = pending.filter(i => i.reason === "tool_approval").map(i => ({ id: i.id, type: "function" as const,
    function: { name: i.metadata!.tool.name, arguments: JSON.stringify(i.metadata!.tool.arguments) } }));
  return { id: `pending-${task.id}`, role: "assistant", content: "", ...(tools.length ? { toolCalls: tools } : {}), metadata: { custom: { agui: { interrupts: pending } } } };
}
export function message(original: A2AMessage): Message | undefined {
  if (![Role.ROLE_USER, Role.ROLE_AGENT].includes(original.role)) return;
  return { id: original.messageId, role: original.role === Role.ROLE_USER ? "user" : "assistant", content: text(original) };
}
export async function history(service: ThreadService, id: string, signal?: AbortSignal) {
  const messages: Message[] = [], seen = new Set<string>();
  let active: Task | undefined;
  for (const listed of await service.list(id, signal)) {
    const task = await service.get(id, listed.id, signal);
    // Keep the resume signal consistent with the message snapshot. A second
    // state read can observe completion after this read missed the final text.
    if (!active && [TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_INPUT_REQUIRED].includes(task.status?.state ?? 0)) active = task;
    for (const original of task.history) {
      const converted = message(original);
      if (converted && !seen.has(converted.id)) { seen.add(converted.id); messages.push(converted); }
    }
  }
  if (active) { const pending = pendingMessage(active); if (pending) messages.push(pending); }
  return { threadId: id, messages, activeTaskId: active?.id ?? null,
    activeTaskState: active ? taskStateToJSON(active.status!.state) : null,
    hasPendingInterrupts: !!active && interrupts(active).length > 0,
    pendingRequestIds: active ? interrupts(active).map(i => i.id) : [] };
}
export function finish(task: Task, threadId: string, runId: string): BaseEvent[] | undefined {
  switch (task.status?.state) {
    case TaskState.TASK_STATE_COMPLETED: return [{ type: E.RUN_FINISHED, threadId, runId, outcome: { type: "success" } }];
    case TaskState.TASK_STATE_CANCELED: return [{ type: E.RUN_ERROR, message: "The task was cancelled.", code: "TASK_CANCELLED" }];
    case TaskState.TASK_STATE_FAILED: case TaskState.TASK_STATE_REJECTED:
      return [{ type: E.RUN_ERROR, message: "The task could not be completed." }];
    case TaskState.TASK_STATE_INPUT_REQUIRED: {
      const pending = interrupts(task);
      if (!pending.length) return [{ type: E.RUN_ERROR, message: "This task requires unsupported input." }];
      const events: BaseEvent[] = [];
      for (const item of pending) if (item.reason === "tool_approval") {
        events.push({ type: E.TOOL_CALL_START, toolCallId: item.id, toolCallName: item.metadata!.tool.name },
          { type: E.TOOL_CALL_ARGS, toolCallId: item.id, delta: JSON.stringify(item.metadata!.tool.arguments) },
          { type: E.TOOL_CALL_END, toolCallId: item.id });
      }
      events.push({ type: E.RUN_FINISHED, threadId, runId, outcome: { type: "interrupt", interrupts: pending } });
      return events;
    }
  }
}
