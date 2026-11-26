/** Explicit live harness. Configuration and tokens travel only through private stdin/stdout pipes. */
import assert from "node:assert/strict";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { setTimeout as sleep } from "node:timers/promises";
import { chromium } from "@playwright/test";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import { createRemoteJWKSet, jwtVerify } from "jose";

type Config = { phase: "oauth" | "tools"; url: string; issuer: string; clientId: string; redirectUri: string;
  username: string; password: string; accessToken: string; foreignThreadId: string; marker: string };
const config: Config = JSON.parse(await new Promise<string>(resolve => {
  let input = ""; process.stdin.setEncoding("utf8");
  process.stdin.on("data", chunk => { input += chunk; }); process.stdin.on("end", () => resolve(input));
}));
const emit = (value: Record<string, unknown>) => process.stdout.write(JSON.stringify(value) + "\n");
let stage = "configuration";
const check = (name: string) => emit({ event: "check", name });
async function oauth() {
  stage = "oauth_discovery";
  const metadata = await (await fetch(`${config.issuer}/.well-known/openid-configuration`)).json() as Record<string, string>;
  assert.equal(metadata.issuer, config.issuer);
  const verifier = randomBytes(32).toString("base64url"), state = randomUUID();
  const url = new URL(metadata.authorization_endpoint);
  url.search = new URLSearchParams({ response_type: "code", client_id: config.clientId, redirect_uri: config.redirectUri,
    scope: `openid ${config.url}/invoke`, resource: config.url, state, code_challenge_method: "S256",
    code_challenge: createHash("sha256").update(verifier).digest("base64url") }).toString();
  const browser = await chromium.launch({ headless: true });
  try {
    // This is a disposable application test identity, never the operator's AWS SSO login.
    const page = await browser.newPage();
    page.setDefaultTimeout(30000);
    let callback: URL | undefined;
    await page.route(`${config.redirectUri}**`, async route => {
      callback = new URL(route.request().url());
      await route.fulfill({ status: 200, contentType: "text/plain", body: "Test authorization received." });
    });
    stage = "cognito_test_login";
    await page.goto(url.toString());
    await page.locator('input[name="username"]:visible, input[type="email"]:visible').first().fill(config.username);
    if (!await page.locator('input[type="password"]:visible').count()) await page.getByRole("button", { name: /continue|next/i }).click();
    await page.locator('input[type="password"]:visible').first().fill(config.password);
    await page.getByRole("button", { name: /sign in|log in/i }).click();
    await page.waitForURL(current => current.origin + current.pathname === config.redirectUri, { timeout: 45000 });
    assert(callback); assert.equal(callback.searchParams.get("state"), state); assert(callback.searchParams.get("code"));
    stage = "oauth_code_exchange";
    const response = await fetch(metadata.token_endpoint, { method: "POST", body: new URLSearchParams({
      grant_type: "authorization_code", client_id: config.clientId, redirect_uri: config.redirectUri,
      code: callback.searchParams.get("code")!, code_verifier: verifier,
    }) });
    assert.equal(response.status, 200);
    const tokens = await response.json() as Record<string, string>;
    const keys = createRemoteJWKSet(new URL(metadata.jwks_uri));
    for (const token of [tokens.access_token]) {
      const { payload } = await jwtVerify(token, keys, { issuer: config.issuer, audience: config.url, algorithms: ["RS256"] });
      assert.equal(payload.token_use, "access"); assert.equal(payload.client_id, config.clientId);
      assert(String(payload.scope).split(" ").includes(`${config.url}/invoke`));
    }
    stage = "oauth_refresh";
    assert(tokens.refresh_token);
    const refreshed = await fetch(metadata.token_endpoint, { method: "POST", body: new URLSearchParams({
      grant_type: "refresh_token", client_id: config.clientId, refresh_token: tokens.refresh_token,
    }) });
    assert.equal(refreshed.status, 200);
    const next = await refreshed.json() as Record<string, string>;
    const { payload } = await jwtVerify(next.access_token, keys, { issuer: config.issuer, audience: config.url, algorithms: ["RS256"] });
    assert.equal(payload.token_use, "access");
    assert(String(payload.scope).split(" ").includes(`${config.url}/invoke`));
    // Parent captures this pipe in memory and never copies it to logs or reports.
    emit({ event: "tokens", accessToken: next.access_token, idToken: tokens.id_token,
      refreshToken: next.refresh_token || tokens.refresh_token, revokeUrl: new URL("/oauth2/revoke", metadata.token_endpoint).toString() });
  } finally { await browser.close(); }
}

async function tools() {
  const connect = async () => {
    const client = new Client({ name: "managed-agent-live-harness", version: "1" });
    await client.connect(new StreamableHTTPClientTransport(new URL(config.url), {
      requestInit: { headers: { authorization: `Bearer ${config.accessToken}` } },
    }));
    return client;
  };
  stage = "mcp_connect";
  let client = await connect();
  try {
    const call = async (name: string, args: Record<string, unknown>) => {
      stage = name;
      const result = await client.callTool({ name, arguments: args }, { timeout: 60000 });
      assert(!result.isError, `Tool failed: ${name}`); assert(result.structuredContent);
      return result.structuredContent as Record<string, any>;
    };
    const wait = async (threadId: string, taskId: string, expected: string) => {
      const deadline = Date.now() + 180000;
      while (Date.now() < deadline) {
        const result = await call("wait_task", { threadId, taskId });
        if (["TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"].includes(result.state)) continue;
        if (expected === "TASK_STATE_CANCELED" && result.state === "TASK_STATE_INPUT_REQUIRED") {
          await sleep(1000); continue;
        }
        assert.equal(result.state, expected);
        emit({ event: "state", threadId, taskId, state: result.state });
        return result;
      }
      throw new Error("Task did not settle within the harness deadline");
    };
    const listed = await client.listTools();
    assert.deepEqual(listed.tools.map(tool => tool.name).sort(), ["create_thread", "get_task", "list_tasks", "list_threads", "send_message", "stop_task", "wait_task"]);
    check("tool_discovery");
    const create = { clientRequestId: randomUUID(), title: `MCP live ${config.marker}` };
    const { thread } = await call("create_thread", create), threadId = thread.threadId as string;
    emit({ event: "thread", threadId });
    assert.equal((await call("create_thread", create)).thread.threadId, threadId);
    assert((await call("list_threads", {})).threads.some((row: any) => row.threadId === threadId));
    check("thread_creation_retry");
    for (const name of ["list_tasks", "send_message", "stop_task"]) {
      const args = name === "list_tasks" ? { threadId: config.foreignThreadId }
        : name === "send_message" ? { threadId: config.foreignThreadId, clientRequestId: randomUUID(), text: "Must be denied" }
        : { threadId: config.foreignThreadId, taskId: randomUUID() };
      assert.equal((await client.callTool({ name, arguments: args })).isError, true);
    }
    check("foreign_thread_denied");
    const send = { threadId, clientRequestId: randomUUID(), text: `Reply with exactly ${config.marker}. Do not use tools or contact anyone.` };
    const sent = await call("send_message", send), taskId = sent.taskId as string;
    emit({ event: "task", threadId, taskId, requestId: send.clientRequestId, state: sent.state });
    assert.equal((await call("send_message", send)).taskId, taskId);
    check("send_retry_same_task");
    await client.close(); client = await connect();
    await wait(threadId, taskId, "TASK_STATE_COMPLETED");
    check("disconnect_does_not_cancel");
    const canonical = await call("get_task", { threadId, taskId });
    assert(canonical.messages.some((message: any) => message.role === "assistant" && message.text.includes(config.marker)));
    assert((await call("list_tasks", { threadId })).tasks.some((task: any) => task.taskId === taskId));
    check("canonical_answer");
    assert.equal((await client.callTool({ name: "get_task", arguments: { threadId, taskId: randomUUID() } })).isError, true);
    check("task_substitution_denied");
    const stopInput = { threadId, clientRequestId: randomUUID(), text: "Use ask_user to ask me 'MCP harness clarification?' and wait for my answer." };
    const pending = await call("send_message", stopInput);
    emit({ event: "task", threadId, taskId: pending.taskId, requestId: stopInput.clientRequestId, state: pending.state });
    const offered = await wait(threadId, pending.taskId, "TASK_STATE_INPUT_REQUIRED");
    assert.equal(offered.needsHumanInput, true);
    assert.equal(offered.threadUrl, `${new URL(config.url).origin}/?thread=${threadId}`);
    check("human_input_link");
    const stopped = await call("stop_task", { threadId, taskId: pending.taskId });
    assert.equal(stopped.taskId, pending.taskId);
    await wait(threadId, pending.taskId, "TASK_STATE_CANCELED");
    check("explicit_stop");
  } finally { await client.close(); }
}

try {
  assert.equal(process.env.RUN_LIVE_MCP, "1");
  assert.equal(new URL(config.url).protocol, "https:");
  if (config.phase === "oauth") await oauth(); else await tools();
} catch {
  // Avoid protocol/Playwright exception bodies: they can contain tokens or callback URLs.
  emit({ event: "failure", stage }); process.exitCode = 1;
}
