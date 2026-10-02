"use client";

import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  ExportedMessageRepository,
  MessagePrimitive,
  ThreadPrimitive,
  type ChatModelRunOptions,
  type ChatModelRunResult
} from "@assistant-ui/react";
import {
  fromAgUiMessages,
  useAgUiInterrupts,
  useAgUiRuntime,
  useAgUiSubmitInterruptResponses,
  type AgUiInterrupt,
  type AgUiResumeEntry,
  type UseAgUiThreadListAdapter
} from "@assistant-ui/react-ag-ui";
import { HttpAgent } from "@ag-ui/client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type Thread = {
  id: string;
  title: string | null;
  lastTaskState: string | null;
  updatedAt: string;
};
type History = {
  threadId: string;
  messages: unknown[];
  activeTaskId: string | null;
  activeTaskState: string | null;
  hasPendingInterrupts: boolean;
};

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "";

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: { "content-type": "application/json", ...init.headers }
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.detail || body?.error || `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

function setThreadUrl(id: string) {
  const url = new URL(window.location.href);
  url.searchParams.set("thread", id);
  window.history.replaceState(null, "", url);
}

export function Assistant() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [archived, setArchived] = useState<Thread | null>(null);
  const [error, setError] = useState<string | null>(null);
  const creationRequestRef = useRef<string | null>(null);

  const refreshThreads = useCallback(async () => {
    const result = await api<{ data: Thread[] }>("/api/threads");
    setThreads(result.data);
    setThreadId((current) => {
      const linked = new URLSearchParams(window.location.search).get("thread");
      const selected = result.data.find((item) => item.id === linked)?.id
        || result.data.find((item) => item.id === current)?.id
        || result.data[0]?.id || null;
      if (selected) setThreadUrl(selected);
      return selected;
    });
    return result.data;
  }, []);

  const createThread = useCallback(async () => {
    const requestId = creationRequestRef.current || crypto.randomUUID();
    creationRequestRef.current = requestId;
    const thread = await api<Thread>("/api/threads", {
      method: "POST",
      body: JSON.stringify({ clientRequestId: requestId })
    });
    creationRequestRef.current = null;
    setThreads((current) => [thread, ...current.filter((item) => item.id !== thread.id)]);
    setThreadId(thread.id);
    setThreadUrl(thread.id);
    setArchived(null);
  }, []);
  const selectThread = useCallback((id: string) => {
    setThreadId(id);
    setThreadUrl(id);
  }, []);

  useEffect(() => {
    api<{ authenticated: boolean }>("/api/auth/session")
      .then(async () => {
        setAuthenticated(true);
        const current = await refreshThreads();
        if (current.length === 0) await createThread();
      })
      .catch(() => setAuthenticated(false));
  }, [createThread, refreshThreads]);

  if (authenticated === null) return <div className="center-card">Checking access…</div>;
  if (!authenticated) {
    return <Login onAuthenticated={() => {
      setAuthenticated(true);
      void refreshThreads().then((items) => { if (items.length === 0) void createThread(); });
    }} />;
  }

  const selected = threads.find((item) => item.id === threadId);
  return (
    <main className="shell">
      <aside className="sidebar">
        <div>
          <p className="eyebrow">Managed Agents</p>
          <h1>Conversations</h1>
        </div>
        <button className="new-button" onClick={() => void createThread().catch((reason) => setError(reason.message))}>
          + New conversation
        </button>
        <nav className="session-list">
          {threads.map((thread) => (
            <button
              key={thread.id}
              className={thread.id === threadId ? "session active" : "session"}
              onClick={() => selectThread(thread.id)}
            >
              <span>{thread.title || "Untitled conversation"}</span>
              <small>{thread.lastTaskState || "Ready"}</small>
            </button>
          ))}
        </nav>
        {archived && (
          <button className="link-button" onClick={() => {
            void api<Thread>(`/api/threads/${archived.id}/restore`, { method: "POST" })
              .then(() => refreshThreads()).then(() => setArchived(null))
              .catch((reason) => setError(reason.message));
          }}>Undo archive</button>
        )}
        <button className="logout" onClick={() => {
          void api("/api/auth/session", { method: "DELETE" }).then(() => setAuthenticated(false));
        }}>Sign out</button>
      </aside>
      <section className="workspace">
        <header className="topbar">
          <div>
            <strong>{selected?.title || "Conversation"}</strong>
            <p>Claude owns the transcript · DSQL owns access and routing</p>
          </div>
          {threadId && (
            <div>
              <button className="link-button" onClick={() => void navigator.clipboard.writeText(`${window.location.origin}/?thread=${threadId}`)}>
                Copy conversation link
              </button>
              <button className="link-button" onClick={() => {
                void api<Thread>(`/api/threads/${threadId}/archive`, { method: "POST" })
                  .then((item) => { setArchived(item); return refreshThreads(); })
                  .catch((reason) => setError(reason.message));
              }}>Archive</button>
            </div>
          )}
        </header>
        {error && <p className="transport-error">{error}</p>}
        {threadId && <AgUiThread key={threadId} threadId={threadId} threads={threads}
          onSelect={selectThread} onCreate={createThread} onUpdate={() => void refreshThreads()} />}
      </section>
    </main>
  );
}

function Login({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  return (
    <div className="login-page">
      <form className="login-card" onSubmit={(event) => {
        event.preventDefault();
        setError("");
        void api("/api/auth/session", { method: "POST", body: JSON.stringify({ accessToken: token }) })
          .then(onAuthenticated).catch((reason) => setError(reason.message));
      }}>
        <p className="eyebrow">Private preview</p>
        <h1>Enter the access token</h1>
        <p>Your Anthropic and Slack credentials remain server-side.</p>
        <input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoFocus />
        {error && <p className="error">{error}</p>}
        <button type="submit">Continue</button>
      </form>
    </div>
  );
}

async function* resumeTask(
  threadId: string,
  options: ChatModelRunOptions,
  loadedMessageIds: ReadonlySet<string>,
  reconcile: () => void
): AsyncGenerator<ChatModelRunResult> {
  const response = await fetch(`${API_BASE}/api/agui/threads/${threadId}/resume`, {
    credentials: "include", signal: options.abortSignal, headers: { accept: "text/event-stream" }
  });
  if (response.status === 409) {
    reconcile();
    return;
  }
  if (!response.ok || !response.body) throw new Error(`Could not reconnect (${response.status})`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";
  let reconnect = false;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      buffer += decoder.decode(next.value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = frame.split("\n").filter((line) => line.startsWith("data: ")).map((line) => line.slice(6)).join("\n");
        if (data) {
          const event = JSON.parse(data);
          if (event.type === "MESSAGES_SNAPSHOT") {
            const hasNewMessage = Array.isArray(event.messages) && event.messages.some(
              (message: { id?: unknown }) => typeof message.id === "string" && !loadedMessageIds.has(message.id)
            );
            if (hasNewMessage) {
              reconnect = true;
              reconcile();
              return;
            }
          } else if (event.type === "TEXT_MESSAGE_CONTENT") {
            text += event.delta;
            yield { content: [{ type: "text", text }] };
          } else if (event.type === "RUN_FINISHED" && event.outcome?.type === "interrupt") {
            yield {
              content: text ? [{ type: "text", text }] : [],
              status: { type: "requires-action", reason: "interrupt" },
              metadata: { custom: { agui: { interrupts: event.outcome.interrupts } } }
            };
          } else if (event.type === "RUN_FINISHED" && event.outcome?.type === "cancelled") {
            if (!text) {
              reconnect = true;
              reconcile();
              return;
            }
            yield { content: text ? [{ type: "text", text }] : [], status: { type: "incomplete", reason: "cancelled" } };
          } else if (event.type === "RUN_FINISHED" && !text && event.outcome?.type === "success") {
            reconnect = true;
            reconcile();
            return;
          } else if (event.type === "RUN_ERROR") {
            throw new Error(event.message || "The task failed");
          }
        }
        boundary = buffer.indexOf("\n\n");
      }
    }
  } finally {
    if (reconnect) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

function AgUiThread({ threadId, threads, onSelect, onCreate, onUpdate }: {
  threadId: string;
  threads: Thread[];
  onSelect: (id: string) => void;
  onCreate: () => Promise<void>;
  onUpdate: () => void;
}) {
  const [revision, setRevision] = useState(0);
  const reconcile = useCallback(() => setRevision((current) => current + 1), []);
  return <AgUiThreadRuntime key={`${threadId}:${revision}`} threadId={threadId} threads={threads}
    onSelect={onSelect} onCreate={onCreate} onUpdate={onUpdate} onReconcile={reconcile} />;
}

function AgUiThreadRuntime({ threadId, threads, onSelect, onCreate, onUpdate, onReconcile }: {
  threadId: string;
  threads: Thread[];
  onSelect: (id: string) => void;
  onCreate: () => Promise<void>;
  onUpdate: () => void;
  onReconcile: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const loadedMessageIds = useRef<ReadonlySet<string>>(new Set());
  const agent = useMemo(() => new HttpAgent({
    url: `${API_BASE}/api/agui`,
    threadId,
    fetch: (url, init) => fetch(url, { ...init, credentials: "include" })
  }), [threadId]);
  const loadThread = useCallback(async (id: string) => {
      const current = await api<History>(`/api/agui/threads/${id}/history`);
      if (id === threadId) {
        loadedMessageIds.current = new Set(current.messages.flatMap((message) => {
          const id = (message as { id?: unknown }).id;
          return typeof id === "string" ? [id] : [];
        }));
      }
      const messages = fromAgUiMessages(current.messages, { showThinking: false });
      return {
        messages,
        unstable_resume: current.activeTaskState === "TASK_STATE_WORKING" || current.activeTaskState === "TASK_STATE_SUBMITTED"
      };
  }, [threadId]);
  const history = useMemo(() => ({
    async load() {
      const current = await loadThread(threadId);
      return { ...ExportedMessageRepository.fromArray(current.messages), unstable_resume: current.unstable_resume };
    },
    async append() { onUpdate(); },
    async update() { onUpdate(); },
    resume: (options: ChatModelRunOptions) => resumeTask(threadId, options, loadedMessageIds.current, onReconcile)
  }), [threadId, onUpdate, onReconcile, loadThread]);
  const threadList = useMemo<UseAgUiThreadListAdapter>(() => ({
    threadId,
    threads: threads.map((item) => ({ id: item.id, remoteId: item.id, status: "regular", title: item.title || undefined })),
    onSwitchToNewThread: onCreate,
    onSwitchToThread: async (id) => {
      const loaded = await loadThread(id);
      onSelect(id);
      return {
        messages: ExportedMessageRepository.fromArray(loaded.messages).messages.map((item) => item.message),
        unstable_resume: loaded.unstable_resume
      };
    }
  }), [threadId, threads, onCreate, onSelect, loadThread]);
  const runtime = useAgUiRuntime({
    agent,
    showThinking: false,
    autoCancelPendingToolCalls: false,
    resumeTranscript: "appended",
    onError: (reason) => setError(reason.message),
    adapters: { history, threadList }
  });
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="thread-root">
        <ThreadPrimitive.Viewport className="thread-viewport">
          <ThreadPrimitive.Empty>
            <div className="empty-state">
              <span>✦</span>
              <h2>Start a durable conversation</h2>
              <p>The same conversation can continue here and in a linked Slack thread.</p>
            </div>
          </ThreadPrimitive.Empty>
          <ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} />
          <InterruptPanel />
          {error && <p className="transport-error">{error}</p>}
          <Composer threadId={threadId} stopLocal={() => runtime.thread.cancelRun()} onReconcile={onReconcile} />
        </ThreadPrimitive.Viewport>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}

function UserMessage() {
  return <MessagePrimitive.Root className="message user-message"><div className="message-label">You</div><MessagePrimitive.Content /></MessagePrimitive.Root>;
}

function AssistantMessage() {
  return <MessagePrimitive.Root className="message assistant-message"><div className="message-label">Claude</div><MessagePrimitive.Content /></MessagePrimitive.Root>;
}

function InterruptPanel() {
  const interrupts = useAgUiInterrupts();
  const submit = useAgUiSubmitInterruptResponses();
  const [decisions, setDecisions] = useState<Record<string, unknown>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (interrupts.length === 0) return null;
  const allAnswered = interrupts.every((item) => decisions[item.id] !== undefined);
  return (
    <section className="interrupt-panel">
      {interrupts.map((item: AgUiInterrupt) => {
        const metadata = item.metadata || {};
        const tool = metadata.tool as { name: string; arguments: unknown } | undefined;
        return <div key={item.id} className="tool-call">
          <div className="tool-call-header"><strong>{tool ? "Tool request" : "Question"}</strong>{tool && <code>{tool.name}</code>}</div>
          {tool && <pre>{JSON.stringify(tool.arguments, null, 2)}</pre>}
          {!tool && <p>{item.message}</p>}
          {tool ? <div className="tool-actions">
            <button type="button" onClick={() => setDecisions((value) => ({ ...value, [item.id]: { decision: "allow" } }))}>Allow</button>
            <button type="button" className="deny" onClick={() => setDecisions((value) => ({ ...value, [item.id]: { decision: "deny" } }))}>Deny</button>
            <input aria-label={`Denial reason for ${tool.name}`} placeholder="Optional denial reason" onChange={(event) => {
              const reason = event.target.value;
              setDecisions((value) => ({ ...value, [item.id]: { decision: "deny", reason } }));
            }} />
          </div> : <div>
            {Array.isArray(metadata.choices) && <select aria-label="Suggested answer" onChange={(event) => {
              setDecisions((value) => ({ ...value, [item.id]: { answer: event.target.value } }));
            }}><option value="">Choose an answer</option>{metadata.choices.map((choice) => <option key={String(choice)}>{String(choice)}</option>)}</select>}
            <input aria-label="Clarification answer" onChange={(event) => {
              setDecisions((value) => ({ ...value, [item.id]: { answer: event.target.value } }));
            }} />
          </div>}
        </div>;
      })}
      {error && <p className="transport-error">{error}</p>}
      <button disabled={!allAnswered || submitting} onClick={() => {
        const responses: AgUiResumeEntry[] = interrupts.map((item) => ({
          interruptId: item.id, status: "resolved", payload: decisions[item.id]
        }));
        setSubmitting(true);
        void submit(responses).then(() => { setDecisions({}); setError(null); })
          .catch((reason) => setError(reason.message)).finally(() => setSubmitting(false));
      }}>Submit responses</button>
    </section>
  );
}

function Composer({ threadId, stopLocal, onReconcile }: { threadId: string; stopLocal: () => void; onReconcile: () => void }) {
  const interrupts = useAgUiInterrupts();
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const stop = () => {
    setStopping(true);
    setError(null);
    void api(`/api/agui/threads/${threadId}/cancel`, { method: "POST" })
      .then(() => { stopLocal(); onReconcile(); })
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setStopping(false));
  };
  const stopButton = <button type="button" className="send-button stop" disabled={stopping} onClick={stop}>Stop</button>;
  return <ComposerPrimitive.Root className="composer">
    <ComposerPrimitive.Input className="composer-input" placeholder="Message Claude…" rows={2} disabled={interrupts.length > 0} />
    <ThreadPrimitive.If running={false}>
      {interrupts.length > 0 ? stopButton : <ComposerPrimitive.Send className="send-button">Send</ComposerPrimitive.Send>}
    </ThreadPrimitive.If>
    <ThreadPrimitive.If running>
      {stopButton}
    </ThreadPrimitive.If>
    {error && <p className="transport-error">{error}</p>}
  </ComposerPrimitive.Root>;
}
