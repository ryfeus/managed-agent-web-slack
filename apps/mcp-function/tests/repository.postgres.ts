import { test, after } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import postgres from "postgres";
import { ThreadRepository } from "@managed-agents/agent-client/repository";
import { ThreadService } from "@managed-agents/agent-client/service";
import type { A2A } from "@managed-agents/agent-client/a2a";
import { Task, TaskState } from "@a2a-js/sdk";

const url = new URL(process.env.DATABASE_URL!);
assert.equal(process.env.APP_ENV, "e2e"); assert.equal(url.hostname, "127.0.0.1"); assert.equal(url.pathname, "/managed_agents_e2e");
const sql = postgres(url.toString(), { max: 5 }), threads = new ThreadRepository(sql), owner = randomUUID();
await sql`INSERT INTO principals (principal_id) VALUES (${owner})`;
after(async () => {
  await sql`DELETE FROM agent_tasks WHERE thread_id IN (SELECT thread_id FROM agent_threads WHERE principal_id=${owner})`;
  await sql`DELETE FROM agent_threads WHERE principal_id=${owner}`; await sql`DELETE FROM principals WHERE principal_id=${owner}`; await sql.end();
});
test("MCP retries retain admission and use an independent message namespace", async () => {
  const row = await threads.create(owner, randomUUID()), requestId = randomUUID(), contextId = randomUUID();
  const task = Task.fromJSON({ id: randomUUID(), contextId, status: { state: TaskState.TASK_STATE_WORKING } });
  let sends = 0, failPush = true;
  const seen = new Set<string>();
  const a2a = {
    send: async (message: { messageId: string }) => { sends++; seen.add(message.messageId); return task; },
    push: async () => { if (failPush) { failPush = false; throw new Error("lost push setup response"); } },
    cancel: async () => task,
  } as unknown as A2A;
  const service = new ThreadService(threads, a2a, "http://sink.local", "mcp");
  await assert.rejects(service.send(row.thread_id, requestId, "private prompt"));
  await service.send(row.thread_id, requestId, "private prompt");
  assert.equal(sends, 2); assert.deepEqual([...seen], [`mcp:${requestId}`]);
  assert.equal((await threads.get(row.thread_id))?.title, null);
  assert.equal((await sql`SELECT * FROM agent_tasks WHERE thread_id=${row.thread_id}`).length, 1);
  await threads.logicalSend(row.thread_id, "cma", `web:agui:${requestId}`);
  assert.equal((await sql`SELECT * FROM agent_tasks WHERE thread_id=${row.thread_id}`).length, 2);
  const foreign = await threads.create(owner, randomUUID());
  await assert.rejects(service.cancelTask(foreign.thread_id, task.id), /not bound/);
});
