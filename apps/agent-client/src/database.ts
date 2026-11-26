import postgres from "postgres";
import { auroraDSQLPostgres } from "@aws/aurora-dsql-postgresjs-connector";

/** Local compositions never fall back to AWS credentials or production databases. */
export function database(env: NodeJS.ProcessEnv, role = "app_runtime") {
  const options = { max: 5, connect_timeout: 10, idle_timeout: 30, max_lifetime: 300, prepare: false };
  if (env.APP_ENV === "e2e") {
    const url = new URL(env.DATABASE_URL || "");
    if (!["127.0.0.1", "localhost"].includes(url.hostname) || url.pathname !== "/managed_agents_e2e") throw new Error("E2E requires isolated loopback PostgreSQL");
    return postgres(url.toString(), options);
  }
  if (!env.DSQL_ENDPOINT) throw new Error("Missing DSQL endpoint");
  return auroraDSQLPostgres({ ...options, host: env.DSQL_ENDPOINT, region: env.AWS_REGION || "us-west-2",
    username: role, database: env.DSQL_DATABASE || "postgres", ssl: { rejectUnauthorized: true } });
}
