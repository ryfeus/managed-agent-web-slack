import { randomUUID } from "node:crypto";
import { setTimeout as sleep } from "node:timers/promises";
import type { Sql } from "postgres";

export type Thread = { thread_id: string; principal_id: string; agent_id: string; context_id: string | null;
  context_state: string; title: string | null; created_at: Date; updated_at: Date; archived_at: Date | null;
  last_task_id: string | null; last_task_state: string | null };
export type Binding = { thread_id: string; agent_id: string; task_id: string | null; client_message_id: string; submission_status: string };
export type Claim = { status: "acquired"; claimId: string } | { status: "ready"; contextId: string } | { status: "busy" };
export class Conflict extends Error {}
export interface Threads {
  owned(principalId: string, id: string): Promise<Thread | undefined>;
  get(id: string): Promise<Thread | undefined>;
  list(principalId: string): Promise<Thread[]>;
  create(principalId: string, requestId: string, title?: string): Promise<Thread>;
  archive(principalId: string, id: string, archived: boolean): Promise<void>;
  title(id: string, title: string): Promise<void>;
  logicalSend(id: string, agentId: string, messageId: string): Promise<void>;
  claim(id: string): Promise<Claim>;
  bindContext(id: string, claim: string, context: string): Promise<void>;
  release(id: string, claim: string): Promise<void>;
  bindTask(id: string, messageId: string, taskId: string): Promise<void>;
  binding(agentId: string, taskId: string): Promise<Binding | undefined>;
}
export async function retry<T>(fn: () => Promise<T>): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    try { return await fn(); }
    catch (error) {
      if (attempt >= 4 || !["40001", "OC000"].includes((error as { code?: string }).code || "")) throw error;
      await sleep(20 * 2 ** attempt * (0.5 + Math.random()));
    }
  }
}
export class ThreadRepository implements Threads {
  constructor(private sql: Sql) {}
  async owned(principalId: string, id: string) { return (await this.sql<Thread[]>`SELECT * FROM agent_threads WHERE principal_id=${principalId} AND thread_id=${id}`)[0]; }
  async get(id: string) { return (await this.sql<Thread[]>`SELECT * FROM agent_threads WHERE thread_id=${id}`)[0]; }
  async list(principalId: string) { return this.sql<Thread[]>`SELECT * FROM agent_threads WHERE principal_id=${principalId} AND archived_at IS NULL ORDER BY updated_at DESC, thread_id DESC`; }
  async create(principalId: string, requestId: string, title?: string) {
    return retry(async () => {
      const [row] = await this.sql<Thread[]>`INSERT INTO agent_threads (thread_id,principal_id,agent_id,creation_request_id,title)
        VALUES (${randomUUID()},${principalId},'cma',${requestId},${title ?? null})
        ON CONFLICT (principal_id,creation_request_id) DO UPDATE SET creation_request_id=EXCLUDED.creation_request_id RETURNING *`;
      if (row.agent_id !== "cma") throw new Conflict("Creation request conflict");
      return row;
    });
  }
  async archive(principalId: string, id: string, archived: boolean) {
    await retry(() => this.sql`UPDATE agent_threads SET archived_at=${archived ? new Date() : null},updated_at=CURRENT_TIMESTAMP WHERE thread_id=${id} AND principal_id=${principalId}`);
  }
  async title(id: string, title: string) {
    await retry(() => this.sql`UPDATE agent_threads SET title=${title.slice(0, 60)},updated_at=CURRENT_TIMESTAMP WHERE thread_id=${id} AND (title IS NULL OR title='')`);
  }
  async logicalSend(id: string, agentId: string, messageId: string) {
    await retry(() => this.sql.begin(async sql => {
      const [thread] = await sql`SELECT agent_id FROM agent_threads WHERE thread_id=${id}`;
      if (!thread || thread.agent_id !== agentId) throw new Conflict("Agent mismatch");
      const [row] = await sql`INSERT INTO agent_tasks (task_binding_id,thread_id,agent_id,client_message_id,submission_status)
        VALUES (${randomUUID()},${id},${agentId},${messageId},'pending') ON CONFLICT (thread_id,client_message_id)
        DO UPDATE SET client_message_id=EXCLUDED.client_message_id RETURNING *`;
      if (row.agent_id !== agentId) throw new Conflict("Logical send mismatch");
      await sql`UPDATE agent_threads SET updated_at=CURRENT_TIMESTAMP WHERE thread_id=${id}`;
    }));
  }
  async claim(id: string): Promise<Claim> {
    const claimId = randomUUID();
    return retry(async () => this.sql.begin(async sql => {
      const changed = await sql`UPDATE agent_threads SET context_state='initializing',context_init_claim_id=${claimId},
        context_init_lease_expires_at=CURRENT_TIMESTAMP + INTERVAL '30 seconds',updated_at=CURRENT_TIMESTAMP
        WHERE thread_id=${id} AND (context_state='uninitialized' OR (context_state='initializing'
        AND (context_init_lease_expires_at IS NULL OR context_init_lease_expires_at<=CURRENT_TIMESTAMP))) RETURNING thread_id`;
      if (changed.length) return { status: "acquired", claimId } as Claim;
      const [row] = await sql`SELECT context_state,context_id FROM agent_threads WHERE thread_id=${id}`;
      if (!row) throw new Conflict("Unknown thread");
      return row.context_state === "ready" ? { status: "ready", contextId: row.context_id } as Claim : { status: "busy" } as Claim;
    })) as Promise<Claim>;
  }
  async bindContext(id: string, claim: string, context: string) {
    await retry(() => this.sql.begin(async sql => {
      const changed = await sql`UPDATE agent_threads SET context_id=${context},context_state='ready',context_init_claim_id=NULL,
        context_init_lease_expires_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE thread_id=${id} AND context_state='initializing'
        AND context_init_claim_id=${claim} AND context_init_lease_expires_at>CURRENT_TIMESTAMP RETURNING thread_id`;
      if (changed.length) return;
      const [row] = await sql`SELECT context_state,context_id FROM agent_threads WHERE thread_id=${id}`;
      if (row?.context_state !== "ready" || row.context_id !== context) throw new Conflict("Stale context claim");
    }));
  }
  async release(id: string, claim: string) {
    await retry(() => this.sql`UPDATE agent_threads SET context_state='uninitialized',context_init_claim_id=NULL,
      context_init_lease_expires_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE thread_id=${id} AND context_state='initializing' AND context_init_claim_id=${claim}`);
  }
  async bindTask(id: string, messageId: string, taskId: string) {
    await retry(() => this.sql.begin(async sql => {
      const changed = await sql`UPDATE agent_tasks SET task_id=${taskId},submission_status='bound',updated_at=CURRENT_TIMESTAMP
        WHERE thread_id=${id} AND client_message_id=${messageId} AND submission_status='pending' AND task_id IS NULL RETURNING task_id`;
      if (changed.length) return;
      const [row] = await sql`SELECT task_id,submission_status FROM agent_tasks WHERE thread_id=${id} AND client_message_id=${messageId}`;
      if (row?.task_id !== taskId || row.submission_status !== "bound") throw new Conflict("Task binding mismatch");
    }));
  }
  async binding(agentId: string, taskId: string) { return (await this.sql<Binding[]>`SELECT * FROM agent_tasks WHERE agent_id=${agentId} AND task_id=${taskId}`)[0]; }
}
