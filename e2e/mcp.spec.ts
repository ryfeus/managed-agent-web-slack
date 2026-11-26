import { randomUUID } from "node:crypto";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import type { APIRequestContext, Page } from "@playwright/test";
import { test as baseTest, expect, login } from "./helpers/fixtures";
import { backend, control, drain, state } from "./helpers/api";
import { COOKIE, sessionCookie } from "../apps/web-function/src/auth";

const test = baseTest.extend<{ mcp: Client }>({
  mcp: async ({ request }, use) => {
    const base = process.env.MCP_PUBLIC_ORIGIN!;
    const { accessToken } = await (await request.get(`${base}/_test/token`)).json();
    const client = new Client({ name: "local-handoff", version: "1" });
    await client.connect(new StreamableHTTPClientTransport(new URL(`${base}/mcp`), {
      requestInit: { headers: { authorization: `Bearer ${accessToken}` } },
    }));
    try { await use(client); } finally { await client.close(); }
  },
});

async function call(client: Client, name: string, args: Record<string, unknown>) {
  const result = await client.callTool({ name, arguments: args }, { timeout: 25000 });
  expect(result.isError).not.toBe(true);
  expect(result.structuredContent).toBeDefined();
  return result.structuredContent as Record<string, any>;
}

async function waitForTask(client: Client, request: APIRequestContext, threadId: string, taskId: string, expected: string) {
  let result: Record<string, any> = {};
  // Each MCP wait is bounded to 20 seconds; repeat while the scheduler settles.
  await expect.poll(async () => {
    await drain(request);
    result = await call(client, "wait_task", { threadId, taskId });
    return result.state;
  }, { timeout: 30000 }).toBe(expected);
  expect(result).toMatchObject({ threadId, taskId, needsHumanInput: expected === "TASK_STATE_INPUT_REQUIRED", timedOut: false });
  return result;
}

async function reviewScript(request: APIRequestContext) {
  await control(request, "policy", { fixture: "human-review" });
  await control(request, "a2a/script", { prompt: "MCP review search", events: [
    { id: "mcp-review-call", type: "agent.tool_use", name: "web_search", input: { query: "specific MCP human review" } },
    { type: "session.status_idle", stop_reason: { type: "requires_action", event_ids: ["mcp-review-call"] } },
  ] });
}

async function sharedTranscript(client: Client, page: Page, threadId: string, taskId: string) {
  const canonical = await call(client, "get_task", { threadId, taskId });
  expect(canonical).toMatchObject({ state: "TASK_STATE_COMPLETED", needsHumanInput: false });
  expect(canonical.messages.filter((message: any) => message.role === "assistant")).toEqual([
    expect.objectContaining({ text: "Done" }),
  ]);
  await expect(page.locator(".interrupt-panel")).toHaveCount(0);
  await expect(page.locator(".assistant-message")).toHaveCount(1);
  await expect(page.locator(".assistant-message")).toContainText("Done");
  const response = await page.request.get(`${backend}/api/agui/threads/${threadId}/history`);
  expect(response.status()).toBe(200);
  const history = await response.json();
  expect(history).toMatchObject({ activeTaskId: null, hasPendingInterrupts: false, pendingRequestIds: [] });
  expect(history.messages).toEqual(canonical.messages.map((message: any) => ({
    id: message.messageId, role: message.role, content: message.text,
  })));
  await page.reload();
  await expect(page.locator(".assistant-message")).toHaveCount(1);
  await expect(page.locator(".assistant-message")).toContainText("Done");
}

for (const ruling of ["allow", "deny"] as const) {
  test(`MCP native review: Web owner ${ruling} resumes the Task and shares canonical history`, async ({ mcp, request, page, baseURL }) => {
    await reviewScript(request);
    const { thread } = await call(mcp, "create_thread", { clientRequestId: randomUUID(), title: "MCP human review" });
    const threadId = thread.threadId as string;
    // An idle tab has no subscription to Tasks subsequently started over MCP.
    const loaded = page.waitForResponse(response => response.url().endsWith(`/api/agui/threads/${threadId}/history`));
    await login(page, threadId);
    expect((await (await loaded).json()).activeTaskId).toBeNull();
    const { taskId } = await call(mcp, "send_message", { threadId, clientRequestId: randomUUID(), text: "MCP review search" });
    const offered = await waitForTask(mcp, request, threadId, taskId, "TASK_STATE_INPUT_REQUIRED");
    expect(offered.threadUrl).toBe(`${baseURL}/?thread=${threadId}`);
    const discovered = await mcp.listTools();
    expect(discovered.tools.map(tool => tool.name).sort()).toEqual([
      "create_thread", "get_task", "list_tasks", "list_threads", "send_message", "stop_task", "wait_task",
    ]);
    // The submission schema cannot smuggle a response through send_message.
    expect((await mcp.callTool({ name: "send_message", arguments: {
      threadId, clientRequestId: randomUUID(), text: "allow", decision: "allow", requestId: "mcp-review-call",
    } })).isError).toBe(true);
    await expect(page.locator(".interrupt-panel")).toHaveCount(0);
    await expect(page.getByText("MCP review search", { exact: true })).toHaveCount(0);
    await page.goto(offered.threadUrl);
    const panel = page.locator(".interrupt-panel");
    await expect(panel.locator("code")).toHaveText("web_search");
    await expect(panel.locator("pre")).toHaveText(JSON.stringify({ query: "specific MCP human review" }, null, 2));
    await expect(panel).toContainText("user-approval");
    const history = await (await page.request.get(`${backend}/api/agui/threads/${threadId}/history`)).json();
    const review = history.messages.flatMap((message: any) => message.metadata?.custom?.agui?.interrupts ?? [])[0];
    expect(review.reason).toBe("tool_approval");
    await expect(panel.locator("p").first()).toHaveText(review.message);
    expect((await state(request)).a2a.confirm_calls).toBe(0);
    await page.getByRole("button", { name: ruling === "allow" ? "Allow" : "Deny", exact: true }).click();
    const accepted = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/api/agui"));
    await page.getByRole("button", { name: "Submit responses" }).click();
    expect((await accepted).status()).toBe(200);
    // Denying the call still completes this fake-CMA turn; it must never become an allow.
    await waitForTask(mcp, request, threadId, taskId, "TASK_STATE_COMPLETED");
    await sharedTranscript(mcp, page, threadId, taskId);
    // A late opposite ruling uses the real Web endpoint and cannot change the winner.
    const late = await page.request.post(`${backend}/api/agui`, { data: {
      threadId, runId: randomUUID(), messages: [], state: {}, tools: [], context: [], forwardedProps: {},
      resume: [{ interruptId: review.id, status: "resolved", payload: { decision: ruling === "allow" ? "deny" : "allow" } }],
    } });
    expect(late.status()).toBe(409);
    await drain(request);
    const current = await state(request);
    expect(current.database.agent_tasks).toHaveLength(1);
    expect(current.database.cma_input_requests).toEqual([expect.objectContaining({
      task_id: taskId, human_ruling: ruling, decision: ruling, status: "resolved",
    })]);
    expect(current.a2a.confirm_calls).toBe(1);
    expect(Object.values(current.a2a.events).flat().filter((event: any) => event.type === "user.tool_confirmation")).toEqual([
      expect.objectContaining({ tool_use_id: "mcp-review-call", result: ruling }),
    ]);
    expect(JSON.stringify(current.database)).not.toContain("specific MCP human review");
  });
}

test("MCP clarification: Web owner answers and the caller observes completion", async ({ mcp, request, page, baseURL }) => {
  await control(request, "a2a/script", { prompt: "MCP clarify task", events: [
    { id: "mcp-ask-one", type: "agent.custom_tool_use", name: "ask_user", input: {
      prompt: "Which environment?", choices: ["staging", "production"],
    } },
    { type: "session.status_idle", stop_reason: { type: "requires_action", event_ids: ["mcp-ask-one"] } },
  ] });
  const { thread } = await call(mcp, "create_thread", { clientRequestId: randomUUID() });
  const threadId = thread.threadId as string;
  const { taskId } = await call(mcp, "send_message", { threadId, clientRequestId: randomUUID(), text: "MCP clarify task" });
  const offered = await waitForTask(mcp, request, threadId, taskId, "TASK_STATE_INPUT_REQUIRED");
  expect(offered.threadUrl).toBe(`${baseURL}/?thread=${threadId}`);
  await login(page);
  await page.goto(offered.threadUrl);
  await expect(page.locator(".interrupt-panel")).toContainText("Which environment?");
  await page.getByRole("textbox", { name: "Clarification answer" }).fill("staging");
  const accepted = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/api/agui"));
  await page.getByRole("button", { name: "Submit responses" }).click();
  expect((await accepted).status()).toBe(200);
  await waitForTask(mcp, request, threadId, taskId, "TASK_STATE_COMPLETED");
  await sharedTranscript(mcp, page, threadId, taskId);
  const current = await state(request);
  expect(current.database.agent_tasks).toHaveLength(1);
  expect(current.database.cma_clarification_requests).toEqual([expect.objectContaining({ task_id: taskId, status: "resolved" })]);
  expect(current.a2a.custom_result_calls).toBe(1);
  expect(Object.values(current.a2a.events).flat().filter((event: any) => event.type === "user.custom_tool_result")).toEqual([
    expect.objectContaining({ custom_tool_use_id: "mcp-ask-one", is_error: false, content: [{ type: "text", text: "staging" }] }),
  ]);
  expect(JSON.stringify(current.database)).not.toContain("staging");
});

test("MCP review: another signed-in Web principal cannot read the thread or rule", async ({ mcp, request, page }) => {
  await reviewScript(request);
  const { thread } = await call(mcp, "create_thread", { clientRequestId: randomUUID(), title: "Private MCP review" });
  const threadId = thread.threadId as string;
  const { taskId } = await call(mcp, "send_message", { threadId, clientRequestId: randomUUID(), text: "MCP review search" });
  const offered = await waitForTask(mcp, request, threadId, taskId, "TASK_STATE_INPUT_REQUIRED");
  const pending = (await state(request)).database.cma_input_requests[0];
  // Local-only signed session for the existing second fixture principal. No auth route is added.
  const cookie = sessionCookie("00000000-0000-4000-8000-000000000002", "e2e-cookie-secret-at-least-32-bytes", false);
  await page.context().addCookies([{ name: COOKIE, value: cookie.split(";")[0].slice(COOKIE.length + 1), url: backend, httpOnly: true, sameSite: "Strict" }]);
  await page.goto(offered.threadUrl);
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  expect((await page.request.get(`${backend}/api/auth/session`)).status()).toBe(200);
  await expect(page.locator(".interrupt-panel")).toHaveCount(0);
  await expect(page.getByText("MCP review search", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Private MCP review", { exact: true })).toHaveCount(0);
  for (const path of [`/api/threads/${threadId}`, `/api/agui/threads/${threadId}/history`, `/api/agui/threads/${threadId}/resume`]) {
    const denied = await page.request.get(backend + path);
    expect(denied.status()).toBe(404);
    expect(await denied.json()).toEqual({ error: "thread not found" });
  }
  for (const decision of ["allow", "deny"]) {
    const denied = await page.request.post(`${backend}/api/agui`, { data: {
      threadId, runId: randomUUID(), messages: [], state: {}, tools: [], context: [], forwardedProps: {},
      resume: [{ interruptId: pending.request_id, status: "resolved", payload: { decision } }],
    } });
    expect(denied.status()).toBe(404);
  }
  await waitForTask(mcp, request, threadId, taskId, "TASK_STATE_INPUT_REQUIRED");
  const current = await state(request);
  expect(current.a2a.confirm_calls).toBe(0);
  expect(current.database.cma_input_requests[0]).toMatchObject({ status: "pending", human_ruling: null, decision: null });
  await call(mcp, "stop_task", { threadId, taskId });
  await waitForTask(mcp, request, threadId, taskId, "TASK_STATE_CANCELED");
});

test("MCP delegates one durable Task across retries and Web sees its canonical answer", async ({ request, page }) => {
  const base = process.env.MCP_PUBLIC_ORIGIN!;
  const { accessToken } = await (await request.get(`${base}/_test/token`)).json();
  const client = new Client({ name: "local-acceptance", version: "1" });
  await client.connect(new StreamableHTTPClientTransport(new URL(`${base}/mcp`), { requestInit: { headers: { authorization: `Bearer ${accessToken}` } } }));
  try {
    await control(request, "a2a/script", { prompt: "MCP acceptance", response: "Canonical MCP answer" });
    const created = await client.callTool({ name: "create_thread", arguments: { clientRequestId: randomUUID(), title: "MCP conversation" } });
    const threadId = (created.structuredContent as { thread: { threadId: string } }).thread.threadId;
    const arguments_ = { threadId, clientRequestId: randomUUID(), text: "MCP acceptance" };
    const sent = await client.callTool({ name: "send_message", arguments: arguments_ });
    expect(sent.isError).not.toBe(true);
    const taskId = (sent.structuredContent as { taskId: string }).taskId;
    const repeated = await client.callTool({ name: "send_message", arguments: arguments_ });
    expect((repeated.structuredContent as { taskId: string }).taskId).toBe(taskId);
    // Closing the client must not cancel the accepted task.
    await client.close();
    await expect.poll(async () => {
      await drain(request);
      return (await state(request)).database.cma_tasks[0]?.a2a_state;
    }).toBe("COMPLETED");
    expect((await state(request)).database.agent_tasks).toHaveLength(1);
    await login(page, threadId);
    await expect(page.getByText("Canonical MCP answer", { exact: true })).toBeVisible();
  } finally { await client.close(); }
});
