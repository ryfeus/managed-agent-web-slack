/** Loopback-only AppSync protocol fixture; never imported by production bundles. */
import { createServer } from "node:http";
import { WebSocketServer, WebSocket } from "ws";

export async function fakeAppSync(port = 0) {
  const subscriptions = new Map<WebSocket, { id: string; channel: string }>();
  const state = { publishes: 0, delivered: 0, connections: 0, failPublish: false, drop: false, duplicate: false, rejectSubscribe: false, batchFrames: false };
  const server = createServer(async (req, res) => {
    let body = ""; for await (const chunk of req) { body += chunk; if (body.length > 65536) { res.writeHead(413).end(); return; } }
    res.setHeader("content-type", "application/json");
    if (req.url === "/_test/preview/state") { res.end(JSON.stringify({ ...state, subscribers: subscriptions.size })); return; }
    if (req.url === "/_test/preview/config") {
      Object.assign(state, JSON.parse(body)); res.end("{}"); return;
    }
    if (req.url === "/event") {
      if (state.failPublish) { res.writeHead(503).end("{}"); return; }
      const { channel, events } = JSON.parse(body);
      state.publishes++;
      for (const [socket, sub] of subscriptions) if (!state.drop && sub.channel === channel && socket.readyState === WebSocket.OPEN) {
        for (const event of state.batchFrames ? [events] : events) {
          socket.send(JSON.stringify({ type: "data", id: sub.id, event })); state.delivered++;
          if (state.duplicate) socket.send(JSON.stringify({ type: "data", id: sub.id, event }));
        }
      }
      res.end('{"failed":[]}'); return;
    }
    res.writeHead(404).end("{}");
  });
  const ws = new WebSocketServer({ server, maxPayload: 65536, handleProtocols: p => p.has("aws-appsync-event-ws") ? "aws-appsync-event-ws" : false });
  ws.on("connection", (socket, req) => {
    state.connections++;
    const auth = (req.headers["sec-websocket-protocol"] || "").split(",").map(p => p.trim()).find(p => p.startsWith("header-"));
    if (!auth || JSON.parse(Buffer.from(auth.slice(7), "base64url").toString()).authorization !== "local-test-only") { socket.close(); return; }
    socket.on("message", raw => {
      const message = JSON.parse(raw.toString());
      if (message.type === "connection_init") socket.send(JSON.stringify({ type: "connection_ack", connectionTimeoutMs: 300000 }));
      if (message.type === "subscribe") {
        if (state.rejectSubscribe || !message.channel.startsWith("/preview/") || message.authorization.authorization !== "local-test-only") {
          socket.send(JSON.stringify({ type: "subscribe_error", id: message.id })); return;
        }
        subscriptions.set(socket, { id: message.id, channel: message.channel });
        socket.send(JSON.stringify({ type: "subscribe_success", id: message.id }));
      }
      if (message.type === "unsubscribe") subscriptions.delete(socket);
    });
    socket.on("close", () => subscriptions.delete(socket));
  });
  await new Promise<void>(resolve => server.listen(port, "127.0.0.1", resolve));
  const address = server.address() as { port: number };
  return { state, subscriptions, httpUrl: `http://127.0.0.1:${address.port}/event`, websocketUrl: `ws://127.0.0.1:${address.port}/event/realtime`,
    close: async () => { for (const socket of ws.clients) socket.terminate(); await new Promise<void>(resolve => ws.close(() => resolve())); await new Promise<void>(resolve => server.close(() => resolve())); } };
}
