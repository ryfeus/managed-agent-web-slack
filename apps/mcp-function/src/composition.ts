import { createLocalJWKSet, exportJWK, generateKeyPair, SignJWT } from "jose";
import { database } from "@managed-agents/agent-client/database";
import { ThreadRepository } from "@managed-agents/agent-client/repository";
import { ControllerClient } from "@managed-agents/agent-client/a2a";
import { ThreadService } from "@managed-agents/agent-client/service";
import { jwtAuthenticator, type AuthConfig } from "./auth.js";
import type { Runtime } from "./app.js";

export async function compose(env: NodeJS.ProcessEnv): Promise<Runtime & { close(): Promise<void>; localToken?: () => Promise<string> }> {
  const local = env.APP_ENV === "e2e";
  const publicOrigin = env.MCP_PUBLIC_ORIGIN || "";
  const origin = new URL(publicOrigin);
  if (origin.origin !== publicOrigin || (local ? !["127.0.0.1", "localhost"].includes(origin.hostname) : origin.protocol !== "https:")) throw new Error("Invalid MCP public origin");
  const issuer = env.MCP_OAUTH_ISSUER || "";
  if (!local && !/^https:\/\/cognito-idp\.[a-z0-9-]+\.amazonaws\.com\/[a-z0-9-]+_[A-Za-z0-9]+$/.test(issuer)) throw new Error("Invalid Cognito issuer");
  const auth: AuthConfig = { issuer, resource: `${publicOrigin}/mcp`, scope: `${publicOrigin}/mcp/invoke`, clientIds: (env.MCP_CLIENT_IDS || "").split(",").filter(Boolean) };
  if (!auth.clientIds.length || !env.CMA_A2A_ENDPOINT || !env.A2A_EVENT_SINK_URL) throw new Error("Missing MCP configuration");
  const sql = database(env, "mcp_runtime");
  const threads = new ThreadRepository(sql);
  let authenticate = jwtAuthenticator(auth);
  let localToken: (() => Promise<string>) | undefined;
  if (local) {
    if (!env.DEV_PRINCIPAL_ID) throw new Error("Missing local principal");
    const { privateKey, publicKey } = await generateKeyPair("RS256");
    authenticate = jwtAuthenticator(auth, createLocalJWKSet({ keys: [{ ...await exportJWK(publicKey), kid: "local", alg: "RS256" }] }));
    localToken = () => new SignJWT({ token_use: "access", client_id: auth.clientIds[0], scope: auth.scope }).setProtectedHeader({ alg: "RS256", kid: "local" })
      .setIssuer(issuer).setAudience(auth.resource).setSubject("local-user").setIssuedAt().setExpirationTime("5m").sign(privateKey);
  }
  return { auth, authenticate, publicOrigin, webOrigin: env.PUBLIC_APP_URL || publicOrigin, threads,
    service: new ThreadService(threads, new ControllerClient(env.CMA_A2A_ENDPOINT), env.A2A_EVENT_SINK_URL, "mcp"),
    async resolveIdentity(identity) {
      const [row] = await sql`SELECT principal_id FROM external_identities WHERE provider='cognito' AND tenant_id=${identity.issuer} AND external_user_id=${identity.subject}`;
      return row?.principal_id as string | undefined;
    }, close: () => sql.end({ timeout: 5 }), localToken };
}
