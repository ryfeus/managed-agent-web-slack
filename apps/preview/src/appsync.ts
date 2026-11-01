import WebSocket from "ws";
import { randomUUID } from "node:crypto";
import { SignatureV4 } from "@smithy/signature-v4";
import { Sha256 } from "@aws-crypto/sha256-js";
import { defaultProvider } from "@aws-sdk/credential-provider-node";
import { metric, parsePreview, type Preview } from "./contract.js";

export type PreviewSubscription = (taskId: string, receive: (p: Preview) => void, signal: AbortSignal) => Promise<void>;
export type PreviewPublisher = (events: Preview[], signal: AbortSignal) => Promise<void>;
export type AppSyncConfig = { httpUrl: string; websocketUrl: string; region: string; local?: boolean };

export function appSync(config: AppSyncConfig): { subscribe: PreviewSubscription; publish: PreviewPublisher } {
  const url = new URL(config.httpUrl);
  if (config.local && [url, new URL(config.websocketUrl)].some(u => !["127.0.0.1", "localhost"].includes(u.hostname))) throw new Error("Local preview must use loopback");
  const signer = new SignatureV4({ service: "appsync", region: config.region, credentials: defaultProvider(), sha256: Sha256 });
  async function authorization(body: string, signal: AbortSignal): Promise<Record<string, string>> {
    if (config.local) return { host: url.host, authorization: "local-test-only" };
    const deadline = AbortSignal.any([signal, AbortSignal.timeout(2000)]);
    deadline.throwIfAborted();
    let onAbort: () => void = () => {};
    try {
      const result = await Promise.race([signer.sign({ protocol: url.protocol, hostname: url.hostname, path: "/event", method: "POST", body,
        headers: { host: url.host, accept: "application/json, text/javascript", "content-encoding": "amz-1.0", "content-type": "application/json; charset=UTF-8" } }),
      new Promise<never>((_, reject) => { onAbort = () => reject(new Error("Preview authentication unavailable")); deadline.addEventListener("abort", onAbort, { once: true }); })]);
      return result.headers;
    } finally { deadline.removeEventListener("abort", onAbort); }
  }
  return {
    async publish(events, signal) {
      if (!events.length) return;
      const taskId = events[0].taskId;
      if (events.length > 5 || events.some(p => !parsePreview(p, taskId))) throw new Error("Invalid preview batch");
      const body = JSON.stringify({ channel: `/preview/${taskId}`, events: events.map(p => JSON.stringify(p)) });
      const response = await fetch(url, { method: "POST", headers: await authorization(body, signal), body,
        signal: AbortSignal.any([signal, AbortSignal.timeout(2000)]) });
      if (!response.ok) throw new Error("Preview publish unavailable");
      const result = await response.json() as { failed?: unknown[] };
      if (result.failed?.length) throw new Error("Preview batch rejected");
    },
    async subscribe(taskId, receive, signal) {
      if (signal.aborted) return;
      const auth = await authorization("{}", signal);
      if (signal.aborted) return;
      const socket = new WebSocket(config.websocketUrl, ["aws-appsync-event-ws", `header-${Buffer.from(JSON.stringify(auth)).toString("base64url")}`], { maxPayload: 65536, handshakeTimeout: 3000 });
      const id = randomUUID();
      await new Promise<void>((resolve) => {
        let subscribed = false;
        let closed = false;
        let keepAlive: ReturnType<typeof setTimeout>;
        const deadline = setTimeout(() => stop("preview_connect_timeout"), 3000);
        function stop(reason: string) {
          if (closed) return;
          closed = true;
          clearTimeout(deadline); clearTimeout(keepAlive);
          signal.removeEventListener("abort", abort);
          if (subscribed && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "unsubscribe", id }));
          socket.close();
          // Do not keep an unresponsive peer/socket alive past request cleanup.
          const kill = setTimeout(() => socket.terminate(), 100); kill.unref();
          metric(reason, { taskId }); resolve();
        }
        function abort() { stop("preview_disconnected"); }
        function heartbeat(ms = 300000) { clearTimeout(keepAlive); keepAlive = setTimeout(() => stop("preview_keepalive_timeout"), Math.min(ms, 300000)); }
        signal.addEventListener("abort", abort, { once: true });
        if (signal.aborted) { abort(); return; }
        socket.on("open", () => socket.send(JSON.stringify({ type: "connection_init" })));
        socket.on("error", () => stop("preview_socket_error"));
        socket.on("close", () => stop("preview_socket_closed"));
        socket.on("message", (raw) => {
          if (closed) return;
          try {
            const message = JSON.parse(raw.toString());
            if (message.type === "connection_ack") {
              heartbeat(message.connectionTimeoutMs);
              void authorization(JSON.stringify({ channel: `/preview/${taskId}` }), signal).then(a => {
                if (!closed && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "subscribe", id, channel: `/preview/${taskId}`, authorization: a }));
              }).catch(() => stop("preview_auth_error"));
            } else if (message.type === "subscribe_success" && message.id === id) {
              subscribed = true; clearTimeout(deadline); metric("preview_subscribed", { taskId });
            } else if (message.type === "ka") heartbeat();
            else if (message.type === "data" && message.id === id && subscribed) {
              // Live AppSync delivers one encoded event per frame; its protocol
              // documentation also describes batches of encoded events.
              const events = typeof message.event === "string" ? [message.event] : message.event;
              if (!Array.isArray(events) || events.length > 5 || events.some(e => typeof e !== "string")) throw new Error("Invalid preview frame");
              for (const encoded of events) {
                const p = parsePreview(JSON.parse(encoded), taskId);
                if (p) receive(p);
              }
            } else if (["connection_error", "subscribe_error", "broadcast_error"].includes(message.type)) stop("preview_subscription_error");
          } catch { stop("preview_invalid_frame"); }
        });
      });
    }
  };
}
