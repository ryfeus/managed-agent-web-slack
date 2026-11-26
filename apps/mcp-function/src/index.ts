import { serve } from "@hono/node-server";
import { Hono } from "hono";
import { compose } from "./composition.js";
import { createApp } from "./app.js";

// Dependency diagnostics can contain wire payloads. Keep logs metadata-only.
console.error = () => console.info(JSON.stringify({ event: "mcp_library_error" }));
console.warn = () => console.info(JSON.stringify({ event: "mcp_library_warning" }));
const runtime = await compose(process.env);
const gateway = createApp(runtime);
const entry = new Hono();
if (runtime.localToken) entry.get("/_test/token", async c => {
  const allowed = new URL(runtime.publicOrigin);
  if (c.req.header("host") !== allowed.host || (c.req.header("origin") && c.req.header("origin") !== allowed.origin)) return c.json({ error: "Origin denied" }, 403);
  return c.json({ accessToken: await runtime.localToken!() }, 200, { "Cache-Control": "no-store" });
});
entry.route("/", gateway.app);
const endpoint = process.env.MCP_HTTP_ENDPOINT || process.env.AWS_LAMBDA_HTTP_ENDPOINT || "127.0.0.1:3004";
const address = new URL(endpoint.includes("://") ? endpoint : `http://${endpoint}`);
if (runtime.localToken && !["127.0.0.1", "localhost"].includes(address.hostname)) throw new Error("Local MCP must bind loopback");
const server = serve({ fetch: entry.fetch, hostname: address.hostname, port: Number(address.port) });
for (const event of ["SIGTERM", "SIGINT"] as const) process.once(event, () => {
  void gateway.close();
  server.close(() => { void runtime.close().then(() => process.exit(0)); });
  setTimeout(() => process.exit(0), 5000).unref();
});
