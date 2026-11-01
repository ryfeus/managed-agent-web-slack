import { randomUUID } from "node:crypto";
import type { Sql } from "postgres";

export type Projection = { binding_id: string; task_id: string; slack_message_ts: string;
  request_event_id: string | null; external_thread_id: string; status: string; last_task_state: string };
export interface Projections {
  read(bindingId: string, taskId: string): Promise<Projection | undefined>;
  claim(row: Projection): Promise<string | undefined>;
  release(row: Projection, token: string): Promise<boolean>;
}

async function bounded<T>(query: PromiseLike<T> & { cancel(): void }): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  try {
    return await Promise.race([query, new Promise<never>((_, reject) => {
      timer = setTimeout(() => { query.cancel(); reject(new Error("Projection timeout")); }, 1500);
    })]);
  } finally { clearTimeout(timer!); }
}

/** No transcript, P0 receipts or controller tables. An OCC conflict drops P1. */
export class ProjectionRepository implements Projections {
  constructor(private sql: Sql) {}
  async read(bindingId: string, taskId: string) {
    return (await bounded(this.sql<Projection[]>`
      SELECT p.binding_id, p.task_id, p.slack_message_ts, p.request_event_id,
             p.status, p.last_task_state, b.external_thread_id
      FROM slack_task_projections p
      JOIN thread_surface_bindings b ON b.binding_id=p.binding_id
      JOIN agent_tasks t ON t.thread_id=b.thread_id AND t.task_id=p.task_id
      WHERE p.binding_id=${bindingId} AND p.task_id=${taskId} AND b.surface='slack'
        AND t.agent_id='cma' AND t.submission_status='bound'
        AND p.initiator_user_id IS NOT NULL AND p.slack_message_ts IS NOT NULL
        AND p.last_task_state IN ('SUBMITTED','WORKING') AND p.status IN ('pending','streaming')`))[0];
  }
  async claim(row: Projection) {
    const token = randomUUID();
    const changed = await bounded(this.sql`
      UPDATE slack_task_projections SET status='streaming', claim_id=${token},
        lease_expires_at=CURRENT_TIMESTAMP + INTERVAL '8 seconds'
      WHERE binding_id=${row.binding_id} AND task_id=${row.task_id} AND status='pending'
        AND claim_id IS NULL AND last_task_state IN ('SUBMITTED','WORKING')
        AND slack_message_ts=${row.slack_message_ts}
        AND EXISTS (SELECT 1 FROM thread_surface_bindings b JOIN agent_tasks t ON t.thread_id=b.thread_id
          WHERE b.binding_id=${row.binding_id} AND b.surface='slack'
            AND b.external_thread_id=${row.external_thread_id} AND t.task_id=${row.task_id}
            AND t.agent_id='cma' AND t.submission_status='bound')
      RETURNING claim_id`);
    return changed.length ? token : undefined;
  }
  async release(row: Projection, token: string) {
    const changed = await bounded(this.sql`
      UPDATE slack_task_projections SET status='pending', claim_id=NULL, lease_expires_at=NULL
      WHERE binding_id=${row.binding_id} AND task_id=${row.task_id}
        AND status='streaming' AND claim_id=${token} RETURNING binding_id`);
    return changed.length === 1;
  }
}
