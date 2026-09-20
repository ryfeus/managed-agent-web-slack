export type ManagedEvent = {
  id?: string;
  type: string;
  processed_at?: string | null;
  content?: unknown;
  input?: unknown;
  name?: string;
  tool_use_id?: string;
  mcp_tool_use_id?: string;
  custom_tool_use_id?: string;
  is_error?: boolean;
  stop_reason?: { type?: string; event_ids?: string[] } | null;
  event?: { id: string; type: string };
  event_id?: string;
  delta?: { type?: string; content?: { type?: string; text?: string } };
  error?: { message?: string };
};

export type ProjectedPart =
  | { type: "text"; text: string }
  | { type: "reasoning"; text: string }
  | {
      type: "tool-call";
      toolCallId: string;
      toolName: string;
      args: Record<string, unknown>;
      result?: string;
      isError?: boolean;
      approval?: { id: string; approved?: boolean; reason?: string };
    };

export type ProjectedMessage = {
  id: string;
  role: "user" | "assistant";
  content: ProjectedPart[];
  createdAt: Date;
};

export type Projection = {
  messages: ProjectedMessage[];
  isRunning: boolean;
  isDead: boolean;
  seen: Set<string>;
  previews: Map<string, { messageIndex: number; partIndex: number }>;
  pendingApprovals: string[];
  error: string | null;
};

export function initialProjection(): Projection {
  return {
    messages: [],
    isRunning: false,
    isDead: false,
    seen: new Set(),
    previews: new Map(),
    pendingApprovals: [],
    error: null
  };
}

function textOf(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return content
    .filter((block): block is { type: string; text: string } =>
      Boolean(block && typeof block === "object" && (block as any).type === "text" && typeof (block as any).text === "string")
    )
    .map((block) => block.text)
    .join("\n\n");
}

function timeOf(value?: string | null): Date {
  return value ? new Date(value) : new Date(0);
}

function ensureAssistant(messages: ProjectedMessage[], processedAt?: string | null): number {
  const index = messages.length - 1;
  if (index >= 0 && messages[index]?.role === "assistant") return index;
  messages.push({
    id: `assistant-${messages.length}`,
    role: "assistant",
    content: [],
    createdAt: timeOf(processedAt)
  });
  return messages.length - 1;
}

function findTool(messages: ProjectedMessage[], id: string) {
  for (let mi = messages.length - 1; mi >= 0; mi -= 1) {
    const pi = messages[mi]?.content.findIndex(
      (part) => part.type === "tool-call" && part.toolCallId === id
    );
    if (pi !== undefined && pi >= 0) return { mi, pi };
  }
  return null;
}

export function applyEvent(state: Projection, event: ManagedEvent): Projection {
  const eventId = event.id;
  if (eventId && state.seen.has(eventId)) return state;
  const next: Projection = {
    ...state,
    messages: state.messages.map((message) => ({
      ...message,
      content: message.content.map((part) => ({ ...part }))
    })),
    seen: new Set(state.seen),
    previews: new Map(state.previews),
    pendingApprovals: [...state.pendingApprovals]
  };
  if (eventId) next.seen.add(eventId);

  switch (event.type) {
    case "user.message":
      next.messages.push({
        id: eventId || `user-${next.messages.length}`,
        role: "user",
        content: [{ type: "text", text: textOf(event.content) }],
        createdAt: timeOf(event.processed_at)
      });
      next.error = null;
      break;
    case "session.status_running":
    case "session.thread_status_running":
      next.isRunning = true;
      next.error = null;
      break;
    case "session.status_idle":
    case "session.thread_status_idle": {
      next.isRunning = false;
      const ids = event.stop_reason?.type === "requires_action" ? event.stop_reason.event_ids || [] : [];
      next.pendingApprovals = ids;
      for (const id of ids) {
        const found = findTool(next.messages, id);
        const part = found ? next.messages[found.mi]?.content[found.pi] : undefined;
        if (part?.type === "tool-call") part.approval = { id };
      }
      break;
    }
    case "session.status_terminated":
    case "session.deleted":
      next.isRunning = false;
      next.isDead = true;
      break;
    case "event_start": {
      if (!event.event || next.seen.has(event.event.id)) break;
      const mi = ensureAssistant(next.messages);
      const content = next.messages[mi]!.content;
      content.push(
        event.event.type === "agent.thinking"
          ? { type: "reasoning", text: "Thinking…" }
          : { type: "text", text: "" }
      );
      next.previews.set(event.event.id, { messageIndex: mi, partIndex: content.length - 1 });
      break;
    }
    case "event_delta": {
      const preview = event.event_id ? next.previews.get(event.event_id) : undefined;
      const deltaText = event.delta?.content?.type === "text" ? event.delta.content.text || "" : "";
      if (!preview || !deltaText) break;
      const part = next.messages[preview.messageIndex]?.content[preview.partIndex];
      if (part?.type === "text") part.text += deltaText;
      break;
    }
    case "agent.thinking": {
      if (eventId && next.previews.has(eventId)) {
        next.previews.delete(eventId);
      } else {
        const mi = ensureAssistant(next.messages, event.processed_at);
        next.messages[mi]!.content.push({ type: "reasoning", text: "Thinking…" });
      }
      break;
    }
    case "agent.message": {
      const text = textOf(event.content);
      const preview = eventId ? next.previews.get(eventId) : undefined;
      if (preview) {
        const part = next.messages[preview.messageIndex]?.content[preview.partIndex];
        if (part?.type === "text") part.text = text;
        next.previews.delete(eventId!);
      } else {
        const mi = ensureAssistant(next.messages, event.processed_at);
        next.messages[mi]!.content.push({ type: "text", text });
      }
      next.error = null;
      break;
    }
    case "agent.tool_use":
    case "agent.mcp_tool_use":
    case "agent.custom_tool_use": {
      const mi = ensureAssistant(next.messages, event.processed_at);
      next.messages[mi]!.content.push({
        type: "tool-call",
        toolCallId: eventId || `tool-${mi}`,
        toolName: event.name || "tool",
        args: event.input && typeof event.input === "object" ? (event.input as Record<string, unknown>) : {},
        ...(eventId && next.pendingApprovals.includes(eventId) ? { approval: { id: eventId } } : {})
      });
      break;
    }
    case "agent.tool_result":
    case "agent.mcp_tool_result":
    case "user.custom_tool_result": {
      const target = event.tool_use_id || event.mcp_tool_use_id || event.custom_tool_use_id;
      const found = target ? findTool(next.messages, target) : null;
      const part = found ? next.messages[found.mi]?.content[found.pi] : undefined;
      if (part?.type === "tool-call") {
        part.result = textOf(event.content);
        part.isError = Boolean(event.is_error);
      }
      break;
    }
    case "user.tool_confirmation": {
      const target = event.tool_use_id;
      const found = target ? findTool(next.messages, target) : null;
      const part = found ? next.messages[found.mi]?.content[found.pi] : undefined;
      if (part?.type === "tool-call") {
        const raw = event as ManagedEvent & { result?: string; deny_message?: string };
        part.approval = {
          id: target!,
          approved: raw.result === "allow",
          ...(raw.deny_message ? { reason: raw.deny_message } : {})
        };
      }
      if (target) next.pendingApprovals = next.pendingApprovals.filter((id) => id !== target);
      break;
    }
    case "session.error":
      next.error = event.error?.message || "The managed agent reported an error.";
      break;
    default:
      break;
  }
  return next;
}

export function project(events: Iterable<ManagedEvent>, from = initialProjection()): Projection {
  let state = from;
  for (const event of events) state = applyEvent(state, event);
  return state;
}
