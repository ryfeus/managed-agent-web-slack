import { beforeAll, describe, expect, it, vi } from "vitest";
import { generateKeyPair, exportJWK, createLocalJWKSet, SignJWT, type JWTPayload } from "jose";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import { Task, TaskState } from "@a2a-js/sdk";
import { createApp, type Runtime } from "../src/app.js";
import { jwtAuthenticator, AuthenticationError } from "../src/auth.js";
import { Conflict } from "@managed-agents/agent-client/repository";

const origin = "https://app.example.test", issuer = "https://issuer.example.test/pool";
const owner = "00000000-0000-4000-8000-000000000001", threadId = "00000000-0000-4000-8000-000000000003", taskId = "00000000-0000-4000-8000-000000000004";
const auth = { issuer, resource: origin + "/mcp", scope: origin + "/mcp/invoke", clientIds: ["codex", "claude"] };
let keys: Awaited<ReturnType<typeof generateKeyPair>>, authenticate: ReturnType<typeof jwtAuthenticator>;
beforeAll(async () => {
  keys = await generateKeyPair("RS256");
  authenticate = jwtAuthenticator(auth, createLocalJWKSet({ keys: [{ ...await exportJWK(keys.publicKey), alg: "RS256", kid: "test" }] }));
});
async function token(overrides: JWTPayload = {}) {
  return new SignJWT({ iss: issuer, aud: auth.resource, sub: "person", token_use: "access", client_id: "codex", scope: auth.scope,
    exp: Math.floor(Date.now() / 1000) + 300, ...overrides }).setProtectedHeader({ alg: "RS256", kid: "test" }).sign(keys.privateKey);
}
function fixture() {
  const row = { thread_id: threadId, principal_id: owner, archived_at: null };
  const task = Task.fromJSON({ id: taskId, status: { state: TaskState.TASK_STATE_COMPLETED }, history: [
    { messageId: "canonical", role: "ROLE_AGENT", parts: [{ text: "Public answer" }, { data: { private: "Never return" } }], metadata: { reasoning: "Never return" } },
  ], metadata: { providerSession: "Never return" } });
  const threads = { owned: vi.fn(async (id: string) => id === owner ? row : undefined), list: vi.fn(async () => [row]), create: vi.fn(async () => row) };
  const service = { get: vi.fn(async () => task), list: vi.fn(async () => [task]), bound: vi.fn(async () => ({})),
    send: vi.fn(async () => task), cancelTask: vi.fn(async () => task), respond: vi.fn() };
  const resolveIdentity = vi.fn(async ({ subject }: { subject: string }): Promise<string | undefined> => subject === "person" ? owner : undefined);
  const gateway = createApp({ threads, service, auth, authenticate, resolveIdentity, publicOrigin: origin } as unknown as Runtime);
  return { ...gateway, threads, service, resolveIdentity, task };
}
async function clientFor(gateway: ReturnType<typeof fixture>, claims: JWTPayload = {}) {
  const client = new Client({ name: "test-client", version: "1" });
  const transport = new StreamableHTTPClientTransport(new URL(origin + "/mcp"), {
    requestInit: { headers: { authorization: `Bearer ${await token(claims)}` } },
    fetch: (input, init) => Promise.resolve(gateway.app.request(new Request(input, init))),
  });
  await client.connect(transport);
  return client;
}

describe("MCP authentication and HTTP boundary", () => {
  it("accepts scoped access tokens and rejects invalid issuer, audience, client, type, expiry, subject and scope", async () => {
    expect(await authenticate(`Bearer ${await token()}`)).toEqual({ issuer, subject: "person" });
    for (const overrides of [{ iss: "https://evil.test" }, { aud: "other" }, { client_id: "other" }, { token_use: "id" }, { exp: 1 }, { sub: "" }, { scope: "openid" }]) {
      await expect(authenticate(`Bearer ${await token(overrides)}`)).rejects.toBeInstanceOf(AuthenticationError);
    }
    await expect(authenticate("Bearer malformed")).rejects.toBeInstanceOf(AuthenticationError);
  });
  it("publishes resource discovery and challenges before DB access; denies unlinked identities and hostile Origin", async () => {
    const g = fixture();
    try {
      expect(await (await g.app.request("/.well-known/oauth-protected-resource/mcp")).json()).toMatchObject({ resource: auth.resource, authorization_servers: [issuer] });
      const denied = await g.app.request("/mcp", { method: "POST", body: "{}" });
      expect(denied.status).toBe(401); expect(denied.headers.get("www-authenticate")).toContain("/.well-known/oauth-protected-resource/mcp");
      expect(denied.headers.get("cache-control")).toBe("no-store"); expect(g.resolveIdentity).not.toHaveBeenCalled();
      const unlinked = await g.app.request("/mcp", { headers: { authorization: `Bearer ${await token({ sub: "unlinked" })}` } });
      expect(unlinked.status).toBe(403);
      const hostile = await g.app.request("/mcp", { headers: { origin: "https://evil.test", authorization: `Bearer ${await token()}` } });
      expect(hostile.status).toBe(403); expect(hostile.headers.get("access-control-allow-origin")).toBeNull();
      expect(g.service.get).not.toHaveBeenCalled();
    } finally { await g.close(); }
  });
  it("serves real SDK discovery and canonical tool results without private Task metadata", async () => {
    const g = fixture(), client = await clientFor(g);
    try {
      const { tools } = await client.listTools();
      expect(tools.map(t => t.name).sort()).toEqual(["create_thread", "get_task", "list_tasks", "list_threads", "send_message", "stop_task", "wait_task"]);
      const result = await client.callTool({ name: "get_task", arguments: { threadId, taskId } });
      expect(JSON.stringify(result)).toContain("Public answer"); expect(JSON.stringify(result)).not.toContain("Never return");
      expect(result.structuredContent).toMatchObject({ threadId, taskId, state: "TASK_STATE_COMPLETED" });
      expect(g.threads.owned).toHaveBeenCalledWith(owner, threadId);
    } finally { await client.close(); await g.close(); }
  });
  it("preserves legacy initialize and stateless tools/list across different requests", async () => {
    const g = fixture();
    try {
      const headers = { authorization: `Bearer ${await token()}`, "content-type": "application/json", accept: "application/json, text/event-stream" };
      for (const [method, params] of [["initialize", { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "legacy", version: "1" } }], ["tools/list", {}]] as const) {
        const response = await g.app.request("/mcp", { method: "POST", headers, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
        expect(response.status).toBe(200); expect(response.headers.get("mcp-session-id")).toBeNull();
        expect(response.headers.get("cache-control")).toBe("no-store");
        const body = await response.text(); expect(body).toContain(method === "initialize" ? "serverInfo" : "send_message");
      }
      const get = await g.app.request("/mcp", { headers: { ...headers, "MCP-Protocol-Version": "2025-11-25" } });
      expect(get.status).toBe(405);
      expect(get.headers.get("cache-control")).toBe("no-store");
    } finally { await g.close(); }
  });
  it("keeps client identities separate and rechecks linking on each request", async () => {
    const g = fixture(), other = "00000000-0000-4000-8000-000000000002";
    g.resolveIdentity.mockImplementation(async ({ subject }) => subject === "person" ? owner : other);
    const first = await clientFor(g), second = await clientFor(g, { sub: "other" });
    try {
      await first.callTool({ name: "list_threads", arguments: {} });
      expect(g.threads.list).toHaveBeenLastCalledWith(owner);
      await second.callTool({ name: "list_threads", arguments: {} });
      expect(g.threads.list).toHaveBeenLastCalledWith(other);
      g.resolveIdentity.mockResolvedValue(undefined);
      await expect(first.callTool({ name: "list_threads", arguments: {} })).rejects.toThrow();
      expect(g.threads.list).toHaveBeenCalledTimes(2);
    } finally { await first.close(); await second.close(); await g.close(); }
  });
  it("denies foreign threads and wrong task binding, with no approval tool or payload-bearing errors", async () => {
    const g = fixture(), client = await clientFor(g);
    try {
      g.threads.owned.mockResolvedValueOnce(undefined);
      expect((await client.callTool({ name: "send_message", arguments: { threadId, clientRequestId: taskId, text: "private" } })).isError).toBe(true);
      expect(g.service.send).not.toHaveBeenCalled();
      g.service.get.mockRejectedValueOnce(new Conflict("sensitive body"));
      const wrong = await client.callTool({ name: "get_task", arguments: { threadId, taskId } });
      expect(wrong.isError).toBe(true); expect(JSON.stringify(wrong)).not.toContain("sensitive body");
      await expect(client.callTool({ name: "approve_tool", arguments: { threadId, taskId } })).rejects.toThrow("not found");
      expect(g.service.respond).not.toHaveBeenCalled();
    } finally { await client.close(); await g.close(); }
  });
  it("returns pending human input without ruling and cancels only with explicit stop_task", async () => {
    const g = fixture(), client = await clientFor(g);
    try {
      g.task.status!.state = TaskState.TASK_STATE_INPUT_REQUIRED;
      expect((await client.callTool({ name: "wait_task", arguments: { threadId, taskId } })).structuredContent).toMatchObject({ needsHumanInput: true, timedOut: false });
      expect(g.service.cancelTask).not.toHaveBeenCalled(); expect(g.service.respond).not.toHaveBeenCalled();
      await client.callTool({ name: "stop_task", arguments: { threadId, taskId } });
      expect(g.service.cancelTask).toHaveBeenCalledExactlyOnceWith(threadId, taskId);
    } finally { await client.close(); await g.close(); }
  });
  it("observes completion across polling without canceling execution", async () => {
    const g = fixture(), client = await clientFor(g);
    try {
      g.service.get.mockResolvedValueOnce(Task.fromJSON({ id: taskId, status: { state: TaskState.TASK_STATE_WORKING } }));
      expect((await client.callTool({ name: "wait_task", arguments: { threadId, taskId } })).structuredContent)
        .toMatchObject({ state: "TASK_STATE_COMPLETED", timedOut: false });
      expect(g.service.get).toHaveBeenCalledTimes(2);
      expect(g.service.cancelTask).not.toHaveBeenCalled();
    } finally { await client.close(); await g.close(); }
  });
  it("canceling an MCP observation never invokes A2A Stop", async () => {
    const g = fixture(), client = await clientFor(g), abort = new AbortController();
    try {
      g.task.status!.state = TaskState.TASK_STATE_WORKING;
      const pending = client.callTool({ name: "wait_task", arguments: { threadId, taskId } }, { signal: abort.signal });
      const canceled = expect(pending).rejects.toThrow();
      await vi.waitFor(() => expect(g.service.get).toHaveBeenCalled());
      abort.abort();
      await canceled;
      expect(g.service.cancelTask).not.toHaveBeenCalled();
    } finally { await client.close(); await g.close(); }
  });
});
