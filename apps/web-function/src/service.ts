import { setTimeout as sleep } from "node:timers/promises";
import { Message, Role, TaskState, type Task } from "@a2a-js/sdk";
import { HUMAN_INPUT, type A2A } from "./a2a.js";
import { Conflict, type Thread, type Threads } from "./repository.js";

export const ACTIVE = new Set([TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_INPUT_REQUIRED]);
export class ThreadService {
  constructor(readonly threads: Threads, readonly a2a: A2A, private sinkUrl: string) {}
  private async thread(id: string): Promise<Thread> {
    const thread = await this.threads.get(id);
    if (!thread) throw new Conflict("Unknown thread");
    return thread;
  }
  async send(id: string, runId: string, text: string): Promise<Task> {
    const thread = await this.thread(id), messageId = `web:agui:${runId}`;
    await this.threads.logicalSend(id, thread.agent_id, messageId);
    let contextId = thread.context_id || "", claimId = "";
    if (!contextId) {
      for (let attempt = 0; attempt < 11; attempt++) {
        const claim = await this.threads.claim(id);
        if (claim.status === "ready") { contextId = claim.contextId; break; }
        if (claim.status === "acquired") { claimId = claim.claimId; break; }
        await sleep(200);
      }
      if (!claimId && !contextId) throw new Conflict("Context initialization busy; retry the same run");
    }
    let task: Task;
    try {
      task = await this.a2a.send(Message.fromJSON({ messageId, contextId, role: Role.ROLE_USER, parts: [{ text }] }));
    } catch (error) {
      // Transport failures can follow admission. Only definitive protocol rejection releases a claim.
      if (claimId && [-32600, -32601, -32602].includes((error as { code: number }).code)) await this.threads.release(id, claimId);
      throw error;
    }
    if (claimId) await this.threads.bindContext(id, claimId, task.contextId);
    else if (task.contextId !== contextId) throw new Conflict("Controller changed context");
    await this.threads.bindTask(id, messageId, task.id);
    await this.a2a.push(task.id, `${this.sinkUrl.replace(/\/$/, "")}/${thread.agent_id}`);
    await this.threads.title(id, text);
    return task;
  }
  async bound(id: string, taskId: string) {
    const thread = await this.thread(id);
    const binding = await this.threads.binding(thread.agent_id, taskId);
    if (!binding || binding.thread_id !== id) throw new Conflict("Task is not bound to thread");
    return { thread, binding };
  }
  async get(id: string, taskId: string, signal?: AbortSignal) {
    const { thread } = await this.bound(id, taskId);
    const task = await this.a2a.get(taskId, signal);
    if (task.contextId !== thread.context_id) throw new Conflict("Task context mismatch");
    return task;
  }
  async list(id: string, signal?: AbortSignal) {
    const thread = await this.thread(id);
    return thread.context_id ? this.a2a.list(thread.context_id, signal) : [];
  }
  async active(id: string, signal?: AbortSignal) {
    const task = (await this.list(id, signal)).find(t => ACTIVE.has(t.status?.state ?? 0));
    return task ? this.get(id, task.id, signal) : undefined;
  }
  async cancel(id: string) {
    const task = await this.active(id);
    return task ? this.a2a.cancel(task.id) : undefined;
  }
  async respond(id: string, task: Task, runId: string, requestId: string, response: Record<string, unknown>) {
    const { thread } = await this.bound(id, task.id);
    return this.a2a.send(Message.fromJSON({ messageId: `web:agui:${runId}:interrupt:${requestId}`, contextId: thread.context_id!,
      taskId: task.id, role: Role.ROLE_USER, extensions: [HUMAN_INPUT], metadata: { [HUMAN_INPUT]: { response } } }), true);
  }
}
