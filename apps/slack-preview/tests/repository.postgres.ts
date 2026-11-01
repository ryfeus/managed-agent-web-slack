import { test, after } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import postgres from "postgres";
import { ProjectionRepository } from "../src/repository.js";

const url = new URL(process.env.DATABASE_URL!);
assert.equal(process.env.APP_ENV, "e2e"); assert.equal(url.hostname, "127.0.0.1"); assert.equal(url.pathname, "/managed_agents_e2e");
const sql = postgres(url.toString(), { max: 5 }), repo = new ProjectionRepository(sql), owner = randomUUID();
await sql`INSERT INTO principals (principal_id) VALUES (${owner})`;
after(async () => {
  await sql`DELETE FROM slack_task_projections WHERE binding_id IN (SELECT binding_id FROM thread_surface_bindings WHERE thread_id IN (SELECT thread_id FROM agent_threads WHERE principal_id=${owner}))`;
  await sql`DELETE FROM thread_surface_bindings WHERE thread_id IN (SELECT thread_id FROM agent_threads WHERE principal_id=${owner})`;
  await sql`DELETE FROM agent_tasks WHERE thread_id IN (SELECT thread_id FROM agent_threads WHERE principal_id=${owner})`;
  await sql`DELETE FROM agent_threads WHERE principal_id=${owner}`; await sql`DELETE FROM principals WHERE principal_id=${owner}`; await sql.end();
});
async function fixture() {
  const thread = randomUUID(), binding = randomUUID(), task = randomUUID();
  await sql`INSERT INTO agent_threads (thread_id,principal_id,agent_id) VALUES (${thread},${owner},'cma')`;
  await sql`INSERT INTO thread_surface_bindings (binding_id,thread_id,surface,tenant_id,external_thread_id) VALUES (${binding},${thread},'slack','T001',${'C001:' + Date.now()})`;
  await sql`INSERT INTO agent_tasks (task_binding_id,thread_id,agent_id,client_message_id,task_id,submission_status) VALUES (${randomUUID()},${thread},'cma',${randomUUID()},${task},'bound')`;
  await sql`INSERT INTO slack_task_projections (binding_id,task_id,status,last_task_state,slack_message_ts,initiator_user_id,request_event_id)
    VALUES (${binding},${task},'pending','WORKING',${Date.now() / 1000},'U001','Ev1')`;
  const row = await repo.read(binding, task); assert.ok(row); return row;
}
async function python(code: string, args: string[]) {
  const result = await promisify(execFile)("backend/.venv/bin/python", ["-c", `import json, sys, dataclasses
from managed_agents_app.config import load_config
from managed_agents_app.db.slack_a2a_repository import SlackA2ARepository
r=SlackA2ARepository(load_config())
${code}`, ...args], { env: process.env });
  return JSON.parse(result.stdout);
}
test("P1 append and P0 stop contend for the same row; finalization wins after release", async () => {
  const row = await fixture();
  const [p1, p0] = await Promise.all([repo.claim(row), python('print(json.dumps(dataclasses.asdict(r.claim_task_stream(sys.argv[1],sys.argv[2])),default=str))', [row.binding_id, row.task_id])]);
  assert.equal(Number(!!p1) + Number(p0.status === "acquired"), 1);
  if (p1) {
    assert.equal(await repo.release(row, randomUUID()), false);
    assert.equal(await repo.release(row, p1), true);
  } else {
    await python('r.finish_stream(sys.argv[1],sys.argv[2],sys.argv[3],fallback=False,state="WORKING")\nprint("null")', [row.binding_id, row.task_id, p0.token]);
  }
  // Simulate P1 append in flight: P0 cannot acquire until its short lease is released.
  const token = await repo.claim(row); assert.ok(token);
  const busy = await python('print(json.dumps(dataclasses.asdict(r.claim_task_stream(sys.argv[1],sys.argv[2])),default=str))', [row.binding_id, row.task_id]);
  assert.equal(busy.status, "busy"); await repo.release(row, token);
  await python('c=r.claim_task_stream(sys.argv[1],sys.argv[2])\nassert c.status == "acquired"\nr.finish_stream(sys.argv[1],sys.argv[2],c.token,fallback=False,state="COMPLETED")\nprint("null")', [row.binding_id, row.task_id]);
  assert.equal(await repo.claim(row), undefined); assert.equal(await repo.read(row.binding_id, row.task_id), undefined);
});
test("expired P1 claims are recoverable by P0; stale release cannot undo INPUT_REQUIRED", async () => {
  const row = await fixture(), token = await repo.claim(row); assert.ok(token);
  await sql`UPDATE slack_task_projections SET lease_expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE binding_id=${row.binding_id}`;
  // P1 never forcibly steals even its own expired claim.
  assert.equal(await repo.claim(row), undefined);
  await python('c=r.claim_task_stream(sys.argv[1],sys.argv[2])\nassert c.status == "acquired"\nr.finish_stream(sys.argv[1],sys.argv[2],c.token,fallback=False,state="INPUT_REQUIRED")\nprint("null")', [row.binding_id, row.task_id]);
  assert.equal(await repo.release(row, token), false); assert.equal(await repo.claim(row), undefined);
  const [final] = await sql`SELECT * FROM slack_task_projections WHERE binding_id=${row.binding_id}`;
  assert.equal(final.last_task_state, "INPUT_REQUIRED"); assert.equal(final.status, "completed"); assert.equal(final.slack_message_ts, null);
  assert.equal(final.attempts, 1); // Only P0 increments receipts/attempts.
});
test("binding/task substitution, foreign surfaces, fallback and settled rows are ineligible", async () => {
  const row = await fixture(), other = await fixture();
  assert.equal(await repo.read(row.binding_id, other.task_id), undefined);
  await sql`UPDATE thread_surface_bindings SET surface='web' WHERE binding_id=${row.binding_id}`;
  assert.equal(await repo.read(row.binding_id, row.task_id), undefined); assert.equal(await repo.claim(row), undefined);
  await sql`UPDATE thread_surface_bindings SET surface='slack',thread_id=(SELECT thread_id FROM thread_surface_bindings WHERE binding_id=${other.binding_id}) WHERE binding_id=${row.binding_id}`;
  assert.equal(await repo.read(row.binding_id, row.task_id), undefined); assert.equal(await repo.claim(row), undefined);
  for (const state of ['INPUT_REQUIRED', 'COMPLETED', 'CANCELED', 'FAILED', 'REJECTED']) {
    await sql`UPDATE slack_task_projections SET last_task_state=${state} WHERE binding_id=${other.binding_id}`;
    assert.equal(await repo.read(other.binding_id, other.task_id), undefined); assert.equal(await repo.claim(other), undefined);
  }
  await sql`UPDATE slack_task_projections SET last_task_state='WORKING',status='fallback' WHERE binding_id=${other.binding_id}`;
  assert.equal(await repo.read(other.binding_id, other.task_id), undefined); assert.equal(await repo.claim(other), undefined);
});
test("P1 release preserves stream identity, state, attempts, errors and P0 receipts", async () => {
  const row = await fixture();
  const [before] = await sql`SELECT * FROM slack_task_projections WHERE binding_id=${row.binding_id}`;
  const token = await repo.claim(row); assert.ok(token); await repo.release(row, token);
  const [after] = await sql`SELECT * FROM slack_task_projections WHERE binding_id=${row.binding_id}`;
  assert.deepEqual(after, before);
});
