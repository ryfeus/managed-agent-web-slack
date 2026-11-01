import { serve } from "@hono/node-server";
import { createApp } from "./app.js";
import { compose } from "./composition.js";
import { metadataOnlyLibraryLogs } from "./logging.js";

metadataOnlyLibraryLogs();
const runtime = await compose(process.env);
const shutdown = new AbortController();
const app = createApp({ ...runtime, shutdown: shutdown.signal });
// Test surfaces are reachable only in the credential-free loopback composition.
if (process.env.APP_ENV === "e2e" && process.env.LOCAL_BACKEND_URL) {
  const upstream = new URL(process.env.LOCAL_BACKEND_URL);
  if (!["127.0.0.1", "localhost"].includes(upstream.hostname)) throw new Error("Local upstream must be loopback");
  // Insert these routes before the Web authentication middleware using an outer app.
}
const { Hono } = await import("hono");
const entry = new Hono();
if (process.env.APP_ENV === "e2e" && process.env.LOCAL_PREVIEW_PORT) {
  entry.all("/_test/preview/*", async c => fetch(`http://127.0.0.1:${Number(process.env.LOCAL_PREVIEW_PORT)}${c.req.path}`, {
    method: c.req.method, body: c.req.method === "GET" ? undefined : await c.req.text(), signal: c.req.raw.signal
  }));
}
if (process.env.APP_ENV === "e2e" && process.env.LOCAL_BACKEND_URL) {
  for (const path of ["/_test/*", "/slack/*", "/anthropic/*"]) entry.all(path, async c => {
    const target = new URL(c.req.path + new URL(c.req.url).search, process.env.LOCAL_BACKEND_URL);
    return fetch(target, { method: c.req.method, headers: c.req.raw.headers,
      body: ["GET", "HEAD"].includes(c.req.method) ? undefined : await c.req.arrayBuffer(), signal: c.req.raw.signal, redirect: "manual" });
  });
}
entry.route("/", app);
const endpoint = process.env.AWS_LAMBDA_HTTP_ENDPOINT || "127.0.0.1:3001";
const address = new URL(endpoint.includes("://") ? endpoint : `http://${endpoint}`);
const server = serve({ fetch: entry.fetch, hostname: address.hostname, port: Number(address.port) });
for (const event of ["SIGTERM", "SIGINT"] as const) process.once(event, () => {
  shutdown.abort();
  server.close(() => { void runtime.close().then(() => process.exit(0)); });
  setTimeout(() => process.exit(0), 5000).unref();
});
