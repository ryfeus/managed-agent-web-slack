import { describe, it, expect, vi } from "vitest";
import { createApp, type Runtime } from "../src/app.js";
import { principal, sessionCookie } from "../src/auth.js";
import { Task, TaskState } from "@a2a-js/sdk";
import { HUMAN_INPUT } from "../src/a2a.js";

const secret = "local-test-cookie-secret-at-least-32-bytes";
const owner = "00000000-0000-4000-8000-000000000001";
const threadId = "00000000-0000-4000-8000-000000000003";
const origin = "https://app.example.test";
function fixture(owned = true) {
  const service = { active: vi.fn(), send: vi.fn(), cancel: vi.fn(), respond: vi.fn() };
  const threads = { owned: vi.fn(async () => owned ? { thread_id: threadId, principal_id: owner, archived_at: null } : undefined), list: vi.fn(async () => []) };
  const app = createApp({ service, threads, config: { production: true, cookieSecret: secret, accessToken: "local-access", principalId: owner, origin } } as unknown as Runtime);
  return { app, service, threads };
}
describe("Web trust boundary", () => {
  it("preserves the Python HMAC cookie vector, expiry, tamper checks and clearing", () => {
    // Captured from main's Python implementation with time.time() = 1000.
    const pythonCookie = "managed_agent_session=eyJwcmluY2lwYWxJZCI6IjAwMDAwMDAwLTAwMDAtNDAwMC04MDAwLTAwMDAwMDAwMDAwMSIsImV4cGlyZXNBdCI6NDQyMDAwMDB9.r_JY6TMWvC5dlvlG9ksjd2p-pDb56eB3sJFO1a9fHeY; Path=/; HttpOnly; SameSite=Strict; Secure; Max-Age=43200";
    const now = vi.spyOn(Date, "now").mockReturnValue(1000000);
    try {
      expect(sessionCookie(owner, secret, true)).toBe(pythonCookie);
      expect(principal(pythonCookie, secret)).toBe(owner);
      expect(principal(pythonCookie, "wrong")).toBeUndefined();
      expect(principal(pythonCookie, secret, 44200000)).toBeUndefined();
      expect(principal(pythonCookie.replace("=", "=tampered"), secret)).toBeUndefined();
      expect(sessionCookie(owner, secret, false, true)).toBe("managed_agent_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0");
    } finally { now.mockRestore(); }
  });
  it("denies missing, malformed and foreign ownership before any A2A call", async () => {
    for (const path of [`/api/agui/threads/${threadId}/history`, `/api/agui/threads/${threadId}/resume`, `/api/agui/threads/${threadId}/cancel`]) {
      const { app, service, threads } = fixture(false);
      const method = path.endsWith("cancel") ? "POST" : "GET";
      expect((await app.request(path, { method, headers: { "content-type": "application/json" } })).status).toBe(401);
      expect(threads.owned).not.toHaveBeenCalled();
      expect((await app.request(path, { method, headers: { "content-type": "application/json", cookie: sessionCookie(owner, secret, true) } })).status).toBe(404);
      for (const spy of Object.values(service)) expect(spy).not.toHaveBeenCalled();
    }
  });
  it("rejects cross-origin login/writes without reflecting the hostile origin", async () => {
    const { app } = fixture();
    const response = await app.request("/api/auth/session", { method: "POST", headers: { "content-type": "application/json", origin: "https://evil.test" }, body: JSON.stringify({ accessToken: "local-access" }) });
    expect(response.status).toBe(403); expect(response.headers.get("access-control-allow-origin")).toBeNull();
    const good = await app.request("/api/auth/session", { method: "POST", headers: { "content-type": "application/json", origin }, body: JSON.stringify({ accessToken: "local-access" }) });
    expect(good.status).toBe(200); expect(good.headers.get("set-cookie")).toMatch(/HttpOnly; SameSite=Strict; Secure/);
    expect(good.headers.get("cache-control")).toBe("no-store");
  });
  it("preserves bodyless Stop while enforcing cookie, ownership and origin checks", async () => {
    const { app, service } = fixture();
    const path = `/api/agui/threads/${threadId}/cancel`;
    expect((await app.request(path, { method: "POST" })).status).toBe(401);
    const cookie = sessionCookie(owner, secret, true);
    expect((await app.request(path, { method: "POST", headers: { cookie, origin: "https://evil.test" } })).status).toBe(403);
    expect(service.cancel).not.toHaveBeenCalled();
    expect((await app.request(path, { method: "POST", headers: { cookie }, body: new ReadableStream({ start(controller) { controller.close(); } }), duplex: "half" } as RequestInit)).status).toBe(200);
    expect(service.cancel).toHaveBeenCalledExactlyOnceWith(threadId);
    expect((await fixture(false).app.request(path, { method: "POST", headers: { cookie } })).status).toBe(404);
  });
  it("rejects forged resume IDs and unsupported attachments before mutation", async () => {
    const { app, service } = fixture();
    const base = { threadId, runId: "run", state: {}, tools: [], context: [], forwardedProps: {}, messages: [{ id: "u", role: "user", content: [{ type: "text", text: "ignored" }] }] };
    const response = await app.request("/api/agui", { method: "POST", headers: { "content-type": "application/json", cookie: sessionCookie(owner, secret, true) }, body: JSON.stringify(base) });
    expect(response.status).toBe(400); expect(service.send).not.toHaveBeenCalled();
    service.active.mockResolvedValue(Task.fromJSON({ id: "task", status: { state: TaskState.TASK_STATE_INPUT_REQUIRED,
      message: { extensions: [HUMAN_INPUT], metadata: { [HUMAN_INPUT]: { requests: [{ kind: "tool_approval", requestId: "review",
        tool: { name: "browser", arguments: {} }, allowedResponses: ["allow", "deny"] }] } } } } }));
    for (const [interruptId, decision, status] of [["forged", "allow", 409], ["review", "unsupported", 400]] as const) {
      const rejected = await app.request("/api/agui", { method: "POST", headers: { "content-type": "application/json", cookie: sessionCookie(owner, secret, true) },
        body: JSON.stringify({ ...base, messages: [], resume: [{ interruptId, status: "resolved", payload: { decision } }] }) });
      expect(rejected.status).toBe(status); expect(service.respond).not.toHaveBeenCalled();
    }
  });
});
