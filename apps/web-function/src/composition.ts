import { database } from "@managed-agents/agent-client/database";
import { SecretsManagerClient, GetSecretValueCommand } from "@aws-sdk/client-secrets-manager";
import { appSync } from "@managed-agents/preview";
import { ThreadRepository } from "./repository.js";
import { ThreadService } from "./service.js";
import { ControllerClient } from "./a2a.js";
import type { Runtime } from "./app.js";

export async function compose(env: NodeJS.ProcessEnv): Promise<Runtime & { close: () => Promise<void> }> {
  const local = env.APP_ENV === "e2e";
  const secrets = local ? undefined : new SecretsManagerClient({ region: env.AWS_REGION || "us-west-2" });
  async function secret(name: string, arn: string) {
    if (local) return env[name] || "";
    const id = env[arn]; if (!id) throw new Error(`Missing ${arn}`);
    const result = await secrets!.send(new GetSecretValueCommand({ SecretId: id }));
    if (!result.SecretString) throw new Error(`Empty ${arn}`);
    return result.SecretString;
  }
  const region = env.AWS_REGION || "us-west-2";
  const origin = env.PUBLIC_APP_URL || "";
  if (!origin || new URL(origin).origin !== origin) throw new Error("PUBLIC_APP_URL must be an exact origin");
  if (!env.CMA_A2A_ENDPOINT || !env.A2A_EVENT_SINK_URL) throw new Error("Missing application A2A endpoints");
  const sql = database(env, env.DSQL_ROLE || "app_runtime");
  const threads = new ThreadRepository(sql);
  const [accessToken, cookieSecret] = await Promise.all([secret("WEB_ACCESS_TOKEN", "WEB_ACCESS_TOKEN_SECRET_ARN"), secret("WEB_COOKIE_SECRET", "WEB_COOKIE_SECRET_SECRET_ARN")]);
  if (!accessToken || cookieSecret.length < 32) throw new Error("Missing Web authentication configuration");
  const preview = env.PREVIEW_ENABLED === "true" && env.APPSYNC_HTTP_URL && env.APPSYNC_WEBSOCKET_URL
    ? appSync({ httpUrl: env.APPSYNC_HTTP_URL, websocketUrl: env.APPSYNC_WEBSOCKET_URL, region, local }) : undefined;
  return { config: { production: !local, accessToken, cookieSecret, origin, principalId: env.DEV_PRINCIPAL_ID || "00000000-0000-4000-8000-000000000001",
    ...(local && env.WEB_STREAM_DEADLINE_MS ? { deadlineMs: Number(env.WEB_STREAM_DEADLINE_MS) } : {}) }, threads,
    service: new ThreadService(threads, new ControllerClient(env.CMA_A2A_ENDPOINT), env.A2A_EVENT_SINK_URL), subscribe: preview?.subscribe,
    close: async () => { await sql.end({ timeout: 5 }); secrets?.destroy(); } };
}
