import { test, after } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import postgres from "postgres";
import { ThreadRepository } from "../src/repository.js";

const url = new URL(process.env.DATABASE_URL!);
assert.equal(process.env.APP_ENV, "e2e"); assert.equal(url.hostname, "127.0.0.1"); assert.equal(url.pathname, "/managed_agents_e2e");
const sql = postgres(url.toString(), { max: 5 });
const repository = new ThreadRepository(sql), owner = randomUUID();
await sql`INSERT INTO principals (principal_id) VALUES (${owner})`;
after(async () => { await sql`DELETE FROM agent_tasks WHERE thread_id IN (SELECT thread_id FROM agent_threads WHERE principal_id=${owner})`;
  await sql`DELETE FROM agent_threads WHERE principal_id=${owner}`; await sql`DELETE FROM principals WHERE principal_id=${owner}`; await sql.end(); });
async function python(expression: string, args: string[]) {
  const result = await promisify(execFile)("backend/.venv/bin/python", ["-c", `import json, sys, dataclasses
from managed_agents_app.config import load_config
from managed_agents_app.db.thread_repository import ThreadRepository
r=ThreadRepository(load_config())
print(json.dumps(${expression},default=str))`, ...args], { env: process.env });
  return JSON.parse(result.stdout);
}

test("concurrent thread creation is idempotent and ownership/archive are preserved", async () => {
  const request = randomUUID();
  const rows = await Promise.all(Array.from({ length: 6 }, () => repository.create(owner, request)));
  assert.equal(new Set(rows.map(r => r.thread_id)).size, 1);
  const id = rows[0].thread_id;
  assert.equal(await repository.owned(randomUUID(), id), undefined);
  await repository.archive(owner, id, true); assert.ok((await repository.get(id))?.archived_at);
  assert.ok(!(await repository.list(owner)).some(r => r.thread_id === id));
  await repository.archive(owner, id, false); assert.equal((await repository.get(id))?.archived_at, null);
});

test("Python and TypeScript contend for the same context fence; stale owner cannot bind", async () => {
  const row = await repository.create(owner, randomUUID());
  const [ts, py] = await Promise.all([repository.claim(row.thread_id), python('dataclasses.asdict(r.claim_context_initialization(sys.argv[1], 30))', [row.thread_id])]);
  assert.deepEqual([ts.status, py.status].sort(), ["acquired", "busy"]);
  const stale = ts.status === "acquired" ? ts.claimId : py.claim_id;
  await sql`UPDATE agent_threads SET context_init_lease_expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE thread_id=${row.thread_id}`;
  const next = await repository.claim(row.thread_id); assert.equal(next.status, "acquired");
  await assert.rejects(repository.bindContext(row.thread_id, stale, "wrong"), /Stale/);
  await repository.release(row.thread_id, stale);
  if (next.status !== "acquired") throw new Error("Expected new owner");
  await repository.bindContext(row.thread_id, next.claimId, randomUUID());
  const read = await python('dataclasses.asdict(r.claim_context_initialization(sys.argv[1], 30))', [row.thread_id]);
  assert.equal(read.status, "ready"); assert.equal(read.context_id, (await repository.get(row.thread_id))?.context_id);
});

test("cross-language logical-send retries bind one Task and reject task-ID substitution", async () => {
  const row = await repository.create(owner, randomUUID()), other = await repository.create(owner, randomUUID()), task = randomUUID();
  await Promise.all([repository.logicalSend(row.thread_id, "cma", "same-send"), python('r.record_logical_send(sys.argv[1], "cma", "same-send")', [row.thread_id])]);
  await Promise.all([repository.bindTask(row.thread_id, "same-send", task), python('r.bind_task_id(sys.argv[1], "same-send", sys.argv[2])', [row.thread_id, task])]);
  const bindings = await sql`SELECT * FROM agent_tasks WHERE thread_id=${row.thread_id}`;
  assert.equal(bindings.length, 1); assert.equal(bindings[0].task_id, task);
  await repository.logicalSend(other.thread_id, "cma", "substitute");
  await assert.rejects(repository.bindTask(other.thread_id, "substitute", task));
  assert.equal((await repository.binding("cma", task))?.thread_id, row.thread_id);
});
