import { ClientFactory, RestTransportFactory, type Client } from "@a2a-js/sdk/client";
import { SendMessageRequest, GetTaskRequest, ListTasksRequest, CancelTaskRequest, SubscribeToTaskRequest, TaskPushNotificationConfig, Message, type Task, type StreamResponse } from "@a2a-js/sdk";

export const PROFILE = "https://ryfeus.github.io/claude-managed-agents-ui-eda/a2a/extensions/async-copilot-controller/v1";
export const HUMAN_INPUT = "https://ryfeus.github.io/claude-managed-agents-ui-eda/a2a/extensions/human-input/v1";
export interface A2A {
  send(message: Message, human?: boolean): Promise<Task>;
  get(id: string, signal?: AbortSignal): Promise<Task>;
  list(contextId: string, signal?: AbortSignal): Promise<Task[]>;
  cancel(id: string): Promise<Task>;
  push(id: string, url: string): Promise<void>;
  subscribe(id: string, signal: AbortSignal): AsyncIterable<StreamResponse>;
}
export class ControllerClient implements A2A {
  private cached?: { client: Promise<Client>; expires: number };
  constructor(private endpoint: string) {}
  private async client() {
    if (!this.cached || this.cached.expires < Date.now()) {
      const client = new ClientFactory({ transports: [new RestTransportFactory({ fetchImpl: (url, init) => fetch(url, { ...init,
        signal: init?.signal || AbortSignal.timeout(20000) }) })] }).createFromUrl(this.endpoint.replace(/\/$/, "") + "/.well-known/agent-card.json", "").then(async client => {
        const card = await client.getAgentCard();
        if (!card.capabilities?.streaming || !card.capabilities.pushNotifications || !card.capabilities.extensions.some(e => e.uri === PROFILE)
          || !card.capabilities.extensions.some(e => e.uri === HUMAN_INPUT)
          || !card.supportedInterfaces.some(i => i.protocolBinding === "HTTP+JSON" && i.url.replace(/\/$/, "") === this.endpoint.replace(/\/$/, ""))) throw new Error("Unsupported controller profile");
        return client;
      });
      this.cached = { client, expires: Date.now() + 300000 };
      client.catch(() => { this.cached = undefined; });
    }
    return this.cached.client;
  }
  private options(signal?: AbortSignal) { return { signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(20000)]) : AbortSignal.timeout(20000), serviceParameters: { "A2A-Extensions": HUMAN_INPUT } }; }
  async send(message: Message, human = false) {
    const result = await (await this.client()).sendMessage(SendMessageRequest.fromJSON({ message: Message.toJSON(message), configuration: { returnImmediately: true } }),
      human ? this.options() : { signal: AbortSignal.timeout(20000) });
    if (!('status' in result)) throw new Error("Controller did not return Task");
    return result;
  }
  async get(id: string, signal?: AbortSignal) { return (await this.client()).getTask(GetTaskRequest.fromJSON({ id }), this.options(signal)); }
  async list(contextId: string, signal?: AbortSignal) {
    const client = await this.client(); const tasks: Task[] = []; let pageToken = "";
    do {
      const page = await client.listTasks(ListTasksRequest.fromJSON({ contextId, pageSize: 50, pageToken }), this.options(signal));
      tasks.push(...page.tasks); pageToken = page.nextPageToken;
    } while (pageToken);
    return tasks;
  }
  async cancel(id: string) { return (await this.client()).cancelTask(CancelTaskRequest.fromJSON({ id }), this.options()); }
  async push(id: string, url: string) { await (await this.client()).createTaskPushNotificationConfig(TaskPushNotificationConfig.fromJSON({ taskId: id, id: "app-control-plane", url }), this.options()); }
  async *subscribe(id: string, signal: AbortSignal) {
    yield* (await this.client()).resubscribeTask(SubscribeToTaskRequest.fromJSON({ id }), { signal, serviceParameters: { "A2A-Extensions": HUMAN_INPUT } });
  }
}
