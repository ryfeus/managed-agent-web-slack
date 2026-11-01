import postgres from "postgres";
import { auroraDSQLPostgres } from "@aws/aurora-dsql-postgresjs-connector";
import { GetSecretValueCommand, SecretsManagerClient } from "@aws-sdk/client-secrets-manager";
import { appSync } from "@managed-agents/preview";
import { ProjectionRepository } from "./repository.js";
import { slackTitleUpdater } from "./slack.js";
import type { Ports } from "./worker.js";

export async function compose(env: NodeJS.ProcessEnv, signal: AbortSignal): Promise<Ports & { close(): Promise<void> }> {
  const local = env.APP_ENV === "e2e", region = env.AWS_REGION || "us-west-2";
  if (local) {
    const db = new URL(env.DATABASE_URL!);
    if (!["127.0.0.1", "localhost"].includes(db.hostname) || db.pathname !== "/managed_agents_e2e") throw new Error("Isolated database required");
    if (!env.LOCAL_BACKEND_URL || new URL(env.LOCAL_BACKEND_URL).hostname !== "127.0.0.1") throw new Error("Loopback recorder required");
  }
  let token = "local-slack-preview-only";
  if (!local) {
    const secrets = new SecretsManagerClient({ region, maxAttempts: 1 });
    try {
      token = (await secrets.send(new GetSecretValueCommand({ SecretId: env.SLACK_BOT_TOKEN_SECRET_ARN! }),
        { abortSignal: AbortSignal.any([signal, AbortSignal.timeout(2000)]) })).SecretString!;
      if (!token) throw new Error("Missing Slack credential");
    } finally { secrets.destroy(); }
  }
  signal.throwIfAborted();
  const preview = appSync({ httpUrl: env.APPSYNC_HTTP_URL!, websocketUrl: env.APPSYNC_WEBSOCKET_URL!, region, local });
  const slack = slackTitleUpdater(token, local ? `${env.LOCAL_BACKEND_URL}/_test/slack-preview-append` : undefined);
  const options = { max: 1, prepare: false, connect_timeout: 2, idle_timeout: 30, max_lifetime: 130, onnotice: () => {} };
  const sql = local ? postgres(env.DATABASE_URL!, options) : auroraDSQLPostgres({ ...options,
    host: env.DSQL_ENDPOINT!, database: env.DSQL_DATABASE || "postgres", username: env.DSQL_ROLE || "app_runtime", region,
    ssl: { rejectUnauthorized: true } });
  const close = async () => { await sql.end({ timeout: 1 }); };
  // Terminate pending DB I/O at the worker deadline, including a lost claim acknowledgment.
  const stop = () => { void close().catch(() => {}); };
  signal.addEventListener("abort", stop, { once: true });
  return { projections: new ProjectionRepository(sql), subscribe: preview.subscribe, slack,
    close: async () => { signal.removeEventListener("abort", stop); await close(); } };
}
