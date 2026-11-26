import { Hono } from "hono";
import { createMcpHandler } from "@modelcontextprotocol/server";
import { AuthenticationError, type Authenticate, type AuthConfig, type Identity } from "./auth.js";
import { toolServer, type ToolRuntime } from "./tools.js";

export type Runtime = ToolRuntime & { webOrigin?: string; auth: AuthConfig; authenticate: Authenticate; resolveIdentity(identity: Identity): Promise<string | undefined> };

export function createApp(runtime: Runtime) {
  const app = new Hono();
  const mcp = createMcpHandler(ctx => {
    const principal = ctx.authInfo?.extra?.principalId;
    if (typeof principal !== "string") throw new AuthenticationError();
    return toolServer({ ...runtime, publicOrigin: runtime.webOrigin || runtime.publicOrigin }, principal);
  }, { responseMode: "json", maxRequestBodySize: 1048576, maxSubscriptions: 0, onerror: () => console.info(JSON.stringify({ event: "mcp_protocol_error" })) });
  app.use("*", async (c, next) => {
    c.header("Cache-Control", "no-store"); c.header("X-Content-Type-Options", "nosniff");
    const origin = c.req.header("origin");
    if (origin && origin !== runtime.publicOrigin) return c.json({ error: "Origin denied" }, 403);
    if (origin) { c.header("Access-Control-Allow-Origin", origin); c.header("Vary", "Origin"); }
    if (c.req.method === "OPTIONS") {
      c.header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS");
      c.header("Access-Control-Allow-Headers", "Authorization,Content-Type,Accept,MCP-Protocol-Version,MCP-Method,MCP-Name,MCP-Session-Id");
      c.header("Access-Control-Expose-Headers", "WWW-Authenticate,MCP-Protocol-Version");
      return c.body(null, 204);
    }
    await next();
    // The SDK supplies its own Response headers, including legacy no-cache.
    // Enforce the gateway policy after dispatch for success and protocol errors.
    c.header("Cache-Control", "no-store");
    c.header("X-Content-Type-Options", "nosniff");
  });
  app.onError(() => {
    console.info(JSON.stringify({ event: "mcp_request_error" }));
    return new Response(JSON.stringify({ error: "Service unavailable" }), { status: 503, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
  });
  app.get("/health", c => c.json({ ok: true }));
  app.get("/.well-known/oauth-protected-resource/mcp", c => c.json({ resource: runtime.auth.resource,
    authorization_servers: [runtime.auth.issuer], scopes_supported: [runtime.auth.scope], bearer_methods_supported: ["header"] }));
  app.all("/mcp", async c => {
    let identity: Identity;
    try { identity = await runtime.authenticate(c.req.header("authorization")); }
    catch (error) {
      if (!(error instanceof AuthenticationError)) throw error;
      c.header("WWW-Authenticate", `Bearer resource_metadata="${runtime.publicOrigin}/.well-known/oauth-protected-resource/mcp", scope="${runtime.auth.scope}"`);
      return c.json({ error: "Unauthorized" }, 401);
    }
    const principalId = await runtime.resolveIdentity(identity);
    if (!principalId) return c.json({ error: "Identity is not linked" }, 403);
    return mcp.fetch(c.req.raw, { authInfo: { token: "verified-at-boundary", clientId: "managed-agent", scopes: [runtime.auth.scope], extra: { principalId } } });
  });
  return { app, close: () => mcp.close() };
}
