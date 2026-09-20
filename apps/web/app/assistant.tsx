"use client";

import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessageLike,
  type ToolCallMessagePartProps
} from "@assistant-ui/react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  applyEvent,
  initialProjection,
  project,
  type ManagedEvent,
  type Projection
} from "@/lib/managed-agent-reducer";

type Session = {
  id: string;
  title: string | null;
  status: string;
  createdAt: string;
};

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "";

async function api(path: string, init: RequestInit = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: { "content-type": "application/json", ...init.headers }
  });
  if (!response.ok) throw new Error((await response.json().catch(() => null))?.error || `Request failed (${response.status})`);
  return response.json();
}

export function Assistant() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);

  const refreshSessions = useCallback(async () => {
    const result = await api("/api/sessions");
    setSessions(result.data);
    const linked = new URLSearchParams(window.location.search).get("session");
    setSessionId((current) =>
      current || result.data.find((item: Session) => item.id === linked)?.id || result.data[0]?.id || null
    );
  }, []);

  useEffect(() => {
    api("/api/auth/session")
      .then(() => {
        setAuthenticated(true);
        return refreshSessions();
      })
      .catch(() => setAuthenticated(false));
  }, [refreshSessions]);

  if (authenticated === null) return <div className="center-card">Checking session…</div>;
  if (!authenticated) {
    return (
      <Login
        onAuthenticated={() => {
          setAuthenticated(true);
          void refreshSessions();
        }}
      />
    );
  }

  return (
    <main className="shell">
      <aside className="sidebar">
        <div>
          <p className="eyebrow">Managed Agents</p>
          <h1>Claude Sessions</h1>
        </div>
        <button className="new-button" onClick={() => setSessionId(null)}>+ New conversation</button>
        <nav className="session-list">
          {sessions.map((session) => (
            <button
              key={session.id}
              className={session.id === sessionId ? "session active" : "session"}
              onClick={() => setSessionId(session.id)}
            >
              <span>{session.title || "Untitled session"}</span>
              <small>{session.status}</small>
            </button>
          ))}
        </nav>
        <button
          className="logout"
          onClick={() => api("/api/auth/session", { method: "DELETE" }).then(() => setAuthenticated(false))}
        >
          Sign out
        </button>
      </aside>
      <section className="workspace">
        <header className="topbar">
          <div>
            <strong>{sessionId ? sessions.find((item) => item.id === sessionId)?.title || "Conversation" : "New conversation"}</strong>
            <p>Claude owns the transcript · DSQL owns access and routing</p>
          </div>
          {sessionId && (
            <button
              className="link-button"
              onClick={() => navigator.clipboard.writeText(`${window.location.origin}/?session=${sessionId}`)}
            >
              Copy session link
            </button>
          )}
        </header>
        <SessionRuntime
          key={sessionId || "new"}
          initialSessionId={sessionId}
          onSessionCreated={(id) => {
            setSessionId(id);
            void refreshSessions();
          }}
        />
      </section>
    </main>
  );
}

function Login({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  return (
    <div className="login-page">
      <form
        className="login-card"
        onSubmit={(event) => {
          event.preventDefault();
          setError("");
          api("/api/auth/session", { method: "POST", body: JSON.stringify({ accessToken: token }) })
            .then(onAuthenticated)
            .catch((reason) => setError(reason.message));
        }}
      >
        <p className="eyebrow">Private preview</p>
        <h1>Enter the access token</h1>
        <p>Your Anthropic and Slack credentials always remain server-side.</p>
        <input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoFocus />
        {error && <p className="error">{error}</p>}
        <button type="submit">Continue</button>
      </form>
    </div>
  );
}

function SessionRuntime({
  initialSessionId,
  onSessionCreated
}: {
  initialSessionId: string | null;
  onSessionCreated: (id: string) => void;
}) {
  const [currentId, setCurrentId] = useState(initialSessionId);
  const [state, setState] = useState<Projection>(initialProjection);
  const [loaded, setLoaded] = useState(!initialSessionId);
  const [transportError, setTransportError] = useState<string | null>(null);
  const streamRef = useRef<EventSource | null>(null);
  const streamEventsRef = useRef<ManagedEvent[]>([]);
  const refreshVersionRef = useRef(0);
  const connectRef = useRef<(id: string) => void>(() => undefined);
  const activeSessionRef = useRef<string | null>(currentId);

  const refresh = useCallback(async (id: string) => {
    const version = ++refreshVersionRef.current;
    const source = streamRef.current;
    const result = await api(`/api/sessions/${id}/events`);
    if (activeSessionRef.current !== id || refreshVersionRef.current !== version || streamRef.current !== source) return;
    // Preserve deltas received while the canonical snapshot was in flight.
    // Canonical event IDs make replaying this connection's buffer idempotent.
    const projection = project(streamEventsRef.current, project(result.data as ManagedEvent[]));
    setState(projection);
    setLoaded(true);
    setTransportError(null);
    if (projection.isRunning && (!streamRef.current || streamRef.current.readyState === EventSource.CLOSED)) {
      connectRef.current(id);
    }
  }, []);

  const connect = useCallback((id: string) => {
    streamRef.current?.close();
    streamEventsRef.current = [];
    const source = new EventSource(`${API_BASE}/api/sessions/${id}/stream`, { withCredentials: true });
    streamRef.current = source;
    source.addEventListener("open", () => {
      void refresh(id).catch((reason) => setTransportError(reason.message));
    });
    source.addEventListener("managed-agent", (event) => {
      if (activeSessionRef.current !== id || streamRef.current !== source) return;
      const managedEvent = JSON.parse((event as MessageEvent).data) as ManagedEvent;
      streamEventsRef.current.push(managedEvent);
      setState((current) => applyEvent(current, managedEvent));
      if (managedEvent.type === "session.status_idle" || managedEvent.type === "session.status_terminated") {
        source.close();
      }
    });
    source.addEventListener("error", () => {
      source.close();
      void refresh(id).catch((reason) => setTransportError(reason.message));
    });
    return source;
  }, [refresh]);

  useEffect(() => { connectRef.current = connect; }, [connect]);

  useEffect(() => {
    activeSessionRef.current = currentId;
    if (!currentId) return;
    void refresh(currentId).catch((reason) => setTransportError(reason.message));
    const poll = setInterval(() => {
      if (document.visibilityState === "visible" && (!streamRef.current || streamRef.current.readyState === EventSource.CLOSED)) {
        void refresh(currentId).catch(() => undefined);
      }
    }, 5000);
    const onFocus = () => void refresh(currentId).catch(() => undefined);
    window.addEventListener("focus", onFocus);
    return () => {
      activeSessionRef.current = null;
      clearInterval(poll);
      window.removeEventListener("focus", onFocus);
      streamRef.current?.close();
    };
  }, [currentId, refresh]);

  const ensureSession = useCallback(async () => {
    if (currentId) return currentId;
    const created = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ clientRequestId: crypto.randomUUID() })
    });
    setCurrentId(created.id);
    onSessionCreated(created.id);
    return created.id as string;
  }, [currentId, onSessionCreated]);

  const runtime = useExternalStoreRuntime<ThreadMessageLike>({
    messages: state.messages as ThreadMessageLike[],
    convertMessage: (message) => message,
    isLoading: !loaded,
    isRunning: state.isRunning || state.pendingApprovals.length > 0,
    isDisabled: state.isDead,
    onNew: async (message: AppendMessage) => {
      const text = message.content
        .filter((part) => part.type === "text")
        .map((part) => (part as { text: string }).text)
        .join("\n")
        .trim();
      if (!text) return;
      const id = await ensureSession();
      setTransportError(null);
      connect(id);
      await api(`/api/sessions/${id}/messages`, {
        method: "POST",
        body: JSON.stringify({ clientRequestId: crypto.randomUUID(), text })
      });
      await refresh(id);
    },
    onCancel: async () => {
      if (currentId) await api(`/api/sessions/${currentId}/interrupt`, { method: "POST", body: "{}" });
    },
    onRespondToToolApproval: async ({ approvalId, approved, reason }) => {
      if (!currentId) return;
      setTransportError(null);
      connect(currentId);
      try {
        await api(`/api/sessions/${currentId}/confirm`, {
          method: "POST",
          body: JSON.stringify({ toolUseId: approvalId, approved, reason })
        });
        await refresh(currentId);
      } catch (error) {
        const message = error instanceof Error ? error.message : "Tool approval failed";
        setTransportError(message);
        throw error;
      }
    }
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="thread-root">
        <ThreadPrimitive.Viewport className="thread-viewport">
          <ThreadPrimitive.Empty>
            <div className="empty-state">
              <span>✦</span>
              <h2>Start a durable conversation</h2>
              <p>The same Claude session can continue here and in a linked Slack thread.</p>
            </div>
          </ThreadPrimitive.Empty>
          <ThreadPrimitive.Messages
            components={{ UserMessage, AssistantMessage }}
          />
          {transportError && <p className="transport-error">{transportError}</p>}
          <Composer />
        </ThreadPrimitive.Viewport>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}

function UserMessage() {
  return (
    <MessagePrimitive.Root className="message user-message">
      <div className="message-label">You</div>
      <MessagePrimitive.Content />
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="message assistant-message">
      <div className="message-label">Claude</div>
      <MessagePrimitive.Content components={{ tools: { Fallback: ToolCall } }} />
    </MessagePrimitive.Root>
  );
}

function ToolCall({
  toolName,
  args,
  result,
  isError,
  approval,
  respondToApproval
}: ToolCallMessagePartProps) {
  const isPending = Boolean(
    approval && approval.approved === undefined && approval.resolution === undefined
  );
  return (
    <section className="tool-call">
      <div className="tool-call-header">
        <strong>Tool request</strong>
        <code>{toolName}</code>
      </div>
      <pre>{JSON.stringify(args, null, 2)}</pre>
      {isPending && (
        <div className="tool-actions" aria-label={`Approve ${toolName}`}>
          <button type="button" onClick={() => respondToApproval({ approved: true })}>Allow</button>
          <button
            type="button"
            className="deny"
            onClick={() => respondToApproval({ approved: false })}
          >
            Deny
          </button>
        </div>
      )}
      {approval?.approved !== undefined && (
        <p className={approval.approved ? "tool-decision allowed" : "tool-decision denied"}>
          {approval.approved ? "Allowed" : "Denied"}{approval.reason ? `: ${approval.reason}` : ""}
        </p>
      )}
      {result !== undefined && (
        <pre className={isError ? "tool-result error" : "tool-result"}>
          {typeof result === "string" ? result : JSON.stringify(result, null, 2)}
        </pre>
      )}
    </section>
  );
}

function Composer() {
  return (
    <ComposerPrimitive.Root className="composer">
      <ComposerPrimitive.Input className="composer-input" placeholder="Message Claude…" rows={2} />
      <ThreadPrimitive.If running={false}>
        <ComposerPrimitive.Send className="send-button">Send</ComposerPrimitive.Send>
      </ThreadPrimitive.If>
      <ThreadPrimitive.If running>
        <ComposerPrimitive.Cancel className="send-button stop">Stop</ComposerPrimitive.Cancel>
      </ThreadPrimitive.If>
    </ComposerPrimitive.Root>
  );
}
