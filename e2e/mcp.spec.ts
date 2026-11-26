import { randomUUID } from "node:crypto";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import { test, expect, login } from "./helpers/fixtures";
import { control, drain, state } from "./helpers/api";

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
