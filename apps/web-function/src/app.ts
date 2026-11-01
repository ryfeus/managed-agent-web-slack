import { Hono } from "hono";
import { stream } from "hono/streaming";
import { HTTPException } from "hono/http-exception";
import { bodyLimit } from "hono/body-limit";
import { EventEncoder } from "@ag-ui/encoder";
import { EventType as E, type RunAgentInput } from "@ag-ui/core";
import { RunAgentInputSchema } from "@ag-ui/core/schemas";
import { z } from "zod";
import { constantEqual, principal, sessionCookie } from "./auth.js";
import { Conflict, type Threads, type Thread } from "./repository.js";
import { history, interrupts } from "./mapper.js";
import { streamTask } from "./stream.js";
import { type ThreadService } from "./service.js";
import { metric, type PreviewSubscription } from "@managed-agents/preview";

export type Config = { production: boolean; principalId: string; accessToken: string; cookieSecret: string; origin: string; deadlineMs?: number };
export type Runtime = { config: Config; threads: Threads; service: ThreadService; subscribe?: PreviewSubscription; shutdown?: AbortSignal };
const uuid = z.string().uuid();
function threadJson(row: Thread) { return { id: row.thread_id, agentId: row.agent_id, title: row.title, createdAt: row.created_at,
  updatedAt: row.updated_at, archivedAt: row.archived_at, lastTaskId: row.last_task_id, lastTaskState: row.last_task_state }; }

export function createApp(runtime: Runtime) {
  const { config, threads, service } = runtime;
  const app = new Hono<{ Variables: { principalId: string } }>();
  app.use("*", bodyLimit({ maxSize: 1048576 }));
  app.use("*", async (c, next) => {
    c.header("Cache-Control", "no-store");
    c.header("X-Content-Type-Options", "nosniff");
    const origin = c.req.header("origin");
    if (origin && origin !== config.origin) throw new HTTPException(403, { message: "Origin denied" });
    if (c.req.header("sec-fetch-site") === "cross-site") throw new HTTPException(403, { message: "Cross-site request denied" });
    if (origin === config.origin) {
      c.header("Access-Control-Allow-Origin", origin); c.header("Access-Control-Allow-Credentials", "true");
      c.header("Vary", "Origin");
    }
    if (c.req.method === "OPTIONS") {
      c.header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS"); c.header("Access-Control-Allow-Headers", "content-type");
      return c.body(null, 204);
    }
    // Node's HTTP listener supplies a stream even for a zero-byte POST.
    const emptyAction = c.req.method === "POST"
      && /^\/api\/(?:agui\/threads\/[^/]+\/cancel|threads\/[^/]+\/(?:archive|restore))$/.test(c.req.path)
      && (await c.req.text()).length === 0;
    if (["POST", "PUT", "PATCH"].includes(c.req.method) && !emptyAction && !c.req.header("content-type")?.startsWith("application/json")) throw new HTTPException(415, { message: "JSON required" });
    if (Number(c.req.header("content-length") || 0) > 1048576) throw new HTTPException(413);
    const id = principal(c.req.header("cookie"), config.cookieSecret);
    if (id) c.set("principalId", id);
    if (!id && c.req.path !== "/api/auth/session" && c.req.path !== "/health") throw new HTTPException(401, { message: "unauthorized" });
    await next();
  });
  app.onError((error, c) => {
    if (error instanceof HTTPException) return c.json({ error: error.message }, error.status);
    if (error instanceof Conflict) return c.json({ error: error.message }, 409);
    if (error instanceof z.ZodError || error.name === "ZodError" || error instanceof SyntaxError) return c.json({ error: "Invalid request" }, 400);
    metric("web_request_error", { errorType: error.name });
    return c.json({ error: "Internal server error" }, 500);
  });
  app.get("/health", c => c.json({ ok: true }));
  app.get("/api/auth/session", c => c.json({ authenticated: !!c.get("principalId") }, c.get("principalId") ? 200 : 401));
  app.post("/api/auth/session", async c => {
    const body = await c.req.json();
    if (!config.accessToken || typeof body?.accessToken !== "string" || !constantEqual(body.accessToken, config.accessToken)) throw new HTTPException(401, { message: "invalid access token" });
    c.header("Set-Cookie", sessionCookie(config.principalId, config.cookieSecret, config.production)); return c.json({ ok: true });
  });
  app.delete("/api/auth/session", c => { c.header("Set-Cookie", sessionCookie("", config.cookieSecret, config.production, true)); return c.json({ ok: true }); });
  async function owned(id: string, principalId: string, active = false) {
    if (!uuid.safeParse(id).success) throw new HTTPException(404, { message: "thread not found" });
    const thread = await threads.owned(principalId, id.toLowerCase());
    if (!thread || (active && thread.archived_at)) throw new HTTPException(404, { message: "thread not found" });
    return thread;
  }
  app.get("/api/threads", async c => c.json({ data: (await threads.list(c.get("principalId"))).map(threadJson) }));
  app.post("/api/threads", async c => {
    const body = z.object({ clientRequestId: uuid, title: z.string().min(1).max(60).optional() }).parse(await c.req.json());
    return c.json(threadJson(await threads.create(c.get("principalId"), body.clientRequestId.toLowerCase(), body.title)), 201);
  });
  app.get("/api/threads/:id", async c => c.json(threadJson(await owned(c.req.param("id"), c.get("principalId")))));
  for (const action of ["archive", "restore"]) app.post(`/api/threads/:id/${action}`, async c => {
    const row = await owned(c.req.param("id")!, c.get("principalId"));
    await threads.archive(c.get("principalId"), row.thread_id, action === "archive");
    return c.json(threadJson((await threads.get(row.thread_id))!));
  });
  app.get("/api/agui/threads/:id/history", async c => {
    const row = await owned(c.req.param("id"), c.get("principalId"), true);
    return c.json(await history(service, row.thread_id, c.req.raw.signal));
  });
  app.post("/api/agui/threads/:id/cancel", async c => {
    const row = await owned(c.req.param("id"), c.get("principalId"), true);
    const task = await service.cancel(row.thread_id); return c.json({ ok: true, taskId: task?.id ?? null });
  });
  const encode = new EventEncoder({ accept: "text/event-stream" });
  function response(c: any, threadId: string, taskId: string, runId: string, resumedIds?: string[]) {
    c.header("Content-Type", "text/event-stream"); c.header("Cache-Control", "no-cache, no-transform"); c.header("X-Accel-Buffering", "no");
    return stream(c, async output => {
      const abort = new AbortController(); output.onAbort(() => abort.abort());
      const signal = AbortSignal.any([abort.signal, c.req.raw.signal, ...(runtime.shutdown ? [runtime.shutdown] : [])]);
      const write = async (event: any) => {
        if (signal.aborted) throw new Error("Disconnected");
        let timer: ReturnType<typeof setTimeout>;
        try { await Promise.race([output.write(typeof event === "string" ? event : encode.encode(event)), new Promise<never>((_, reject) => { timer = setTimeout(() => { abort.abort(); reject(new Error("Slow consumer")); }, 5000); })]); }
        finally { clearTimeout(timer!); }
      };
      try { await streamTask({ service, threadId, taskId, runId, resumedIds, write, signal, subscribe: runtime.subscribe, deadlineMs: config.deadlineMs }); }
      catch { if (!signal.aborted) await write({ type: E.RUN_ERROR, code: "STREAM_RECONNECT_REQUIRED", message: "Reconnect to follow the task." }).catch(() => undefined); }
      finally { abort.abort(); }
    });
  }
  app.get("/api/agui/threads/:id/resume", async c => {
    const row = await owned(c.req.param("id"), c.get("principalId"), true);
    const task = await service.active(row.thread_id);
    if (!task) throw new HTTPException(409, { message: "No active task" });
    const { binding } = await service.bound(row.thread_id, task.id);
    const original = binding.client_message_id;
    return response(c, row.thread_id, task.id, original.startsWith("web:agui:") ? original.slice(9) : `reconnect:${task.id}`);
  });
  app.post("/api/agui", async c => {
    // Bound bodies even when a client uses chunked encoding.
    const raw = await c.req.text();
    if (Buffer.byteLength(raw) > 1048576) throw new HTTPException(413);
    const input = RunAgentInputSchema.parse(JSON.parse(raw)) as RunAgentInput;
    const row = await owned(input.threadId, c.get("principalId"), true);
    if (!input.runId || input.runId.length > 128) throw new HTTPException(400, { message: "Invalid run ID" });
    if (input.resume?.length) {
      const task = await service.active(row.thread_id);
      if (!task) throw new HTTPException(409, { message: "No active task" });
      const pending = interrupts(task), ids = pending.map(i => i.id);
      if (!ids.length || input.resume.length !== ids.length || new Set(input.resume.map(r => r.interruptId)).size !== ids.length || input.resume.some(r => !ids.includes(r.interruptId))) throw new HTTPException(409, { message: "Pending interrupts changed; reload" });
      const answers = input.resume.map(entry => {
        if (entry.status !== "resolved") throw new HTTPException(400, { message: "Explicit response required" });
        const item = pending.find(i => i.id === entry.interruptId)!;
        const payload = item.reason === "tool_approval"
          ? z.object({ decision: z.enum(["allow", "deny"]), reason: z.string().max(1000).optional() }).strict().refine(p => p.reason === undefined || p.decision === "deny").parse(entry.payload)
          : z.object({ answer: z.string().min(1).max(20000) }).strict().parse(entry.payload);
        return { requestId: item.id, kind: item.reason, ...payload };
      });
      for (const answer of answers) await service.respond(row.thread_id, task, input.runId, answer.requestId, answer);
      return response(c, row.thread_id, task.id, input.runId, ids);
    }
    const last = input.messages.at(-1);
    if (last?.role !== "user" || typeof last.content !== "string" || !last.content.trim() || last.content.length > 20000) throw new HTTPException(400, { message: "A nonempty text user message is required" });
    const task = await service.send(row.thread_id, input.runId, last.content.trim());
    return response(c, row.thread_id, task.id, input.runId);
  });
  return app;
}
