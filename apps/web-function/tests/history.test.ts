import { expect, it, vi } from "vitest";
import { Task, TaskState } from "@a2a-js/sdk";
import { history } from "../src/mapper.js";
import type { ThreadService } from "../src/service.js";

it("keeps reload resumable when completion follows an incomplete history snapshot", async () => {
  const working = Task.fromJSON({ id: "task", status: { state: TaskState.TASK_STATE_WORKING },
    history: [{ messageId: "user", role: "ROLE_USER", parts: [{ text: "question" }] }] });
  const complete = Task.fromJSON({ id: "task", status: { state: TaskState.TASK_STATE_COMPLETED },
    history: [{ messageId: "user", role: "ROLE_USER", parts: [{ text: "question" }] },
      { messageId: "answer", role: "ROLE_AGENT", parts: [{ text: "canonical answer" }] }] });
  const service = { list: vi.fn(async () => [working]), get: vi.fn().mockResolvedValueOnce(working).mockResolvedValue(complete),
    // By the end of the first history request, the controller is already done.
    active: vi.fn(async () => undefined) };
  const first = await history(service as unknown as ThreadService, "thread");
  expect(first.messages).toHaveLength(1);
  expect(first.activeTaskId).toBe("task");
  expect(first.activeTaskState).toBe("TASK_STATE_WORKING");
  // Resume may return 409; reloading canonical history then includes the answer.
  const second = await history(service as unknown as ThreadService, "thread");
  expect(second.activeTaskId).toBeNull();
  expect(second.messages.at(-1)?.content).toBe("canonical answer");
});
