import Anthropic from "@anthropic-ai/sdk";
import { ClientFactory, RestTransportFactory } from "@a2a-js/sdk/client";
import { GetTaskRequest, ListTasksRequest, TaskState } from "@a2a-js/sdk";
import { SecretsManagerClient, GetSecretValueCommand } from "@aws-sdk/client-secrets-manager";
import { appSync } from "@managed-agents/preview";
import type { ObserverPorts } from "./observer.js";

export async function compose(env: NodeJS.ProcessEnv): Promise<ObserverPorts> {
  const local = env.APP_ENV === "e2e";
  let key = "local-preview-only";
  if (!local) {
    const secrets = new SecretsManagerClient({ region: env.AWS_REGION });
    try { key = (await secrets.send(new GetSecretValueCommand({ SecretId: env.ANTHROPIC_API_KEY_SECRET_ARN! }))).SecretString!; }
    finally { secrets.destroy(); }
    if (!key) throw new Error("Missing observer credential");
  }
  if (local && (!env.LOCAL_CMA_URL || !["127.0.0.1", "localhost"].includes(new URL(env.LOCAL_CMA_URL).hostname))) throw new Error("Local CMA must use loopback");
  const provider = new Anthropic({ apiKey: key, maxRetries: 0, timeout: 125000, ...(local ? { baseURL: env.LOCAL_CMA_URL } : {}) });
  const client = await new ClientFactory({ transports: [new RestTransportFactory({ fetchImpl: (url, init) => fetch(url, { ...init, signal: init?.signal || AbortSignal.timeout(5000) }) })] }).createFromUrl(env.CMA_A2A_ENDPOINT!.replace(/\/$/, "") + "/.well-known/agent-card.json", "");
  const preview = appSync({ httpUrl: env.APPSYNC_HTTP_URL!, websocketUrl: env.APPSYNC_WEBSOCKET_URL!, region: env.AWS_REGION || "us-west-2", local });
  return {
    async *events(sessionId, signal) {
      const stream = await provider.beta.sessions.events.stream(sessionId, { event_deltas: ["agent.message"] }, { signal });
      try { yield* stream; } finally { stream.controller.abort(); }
    },
    async eligible(hint, signal) {
      const options = { signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]) };
      const task = await client.getTask(GetTaskRequest.fromJSON({ id: hint.taskId, historyLength: 0 }), options);
      if (task.contextId !== hint.contextId || task.status?.state !== TaskState.TASK_STATE_WORKING) return false;
      // Controller lists FIFO. Do not tail a reused provider session for a later Task.
      let pageToken = "";
      do {
        const page = await client.listTasks(ListTasksRequest.fromJSON({ contextId: hint.contextId, pageToken, pageSize: 50 }), options);
        const first = page.tasks.find(t => [TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_INPUT_REQUIRED].includes(t.status?.state ?? 0));
        if (first) return first.id === hint.taskId;
        pageToken = page.nextPageToken;
      } while (pageToken);
      return false;
    },
    publish: preview.publish
  };
}
