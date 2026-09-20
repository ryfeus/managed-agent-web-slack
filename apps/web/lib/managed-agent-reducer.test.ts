import { describe, expect, it } from "vitest";
import { project } from "./managed-agent-reducer";

describe("Managed Agent event projection", () => {
  it("reconstructs a simple turn and deduplicates replayed events", () => {
    const events = [
      { id: "u1", type: "user.message", processed_at: "2026-01-01T00:00:00Z", content: [{ type: "text", text: "Hello" }] },
      { id: "r1", type: "session.status_running" },
      { id: "a1", type: "agent.message", processed_at: "2026-01-01T00:00:01Z", content: [{ type: "text", text: "Hi" }] },
      { id: "i1", type: "session.status_idle", stop_reason: { type: "end_turn" } },
      { id: "a1", type: "agent.message", content: [{ type: "text", text: "Hi" }] }
    ];
    const state = project(events);
    expect(state.messages).toHaveLength(2);
    expect(state.messages[1]?.content[0]).toEqual({ type: "text", text: "Hi" });
    expect(state.isRunning).toBe(false);
  });

  it("reconciles a token preview into the persisted message", () => {
    const state = project([
      { id: "running", type: "session.status_running" },
      { type: "event_start", event: { id: "a1", type: "agent.message" } },
      { type: "event_delta", event_id: "a1", delta: { type: "content_delta", content: { type: "text", text: "Hel" } } },
      { id: "a1", type: "agent.message", content: [{ type: "text", text: "Hello" }] }
    ]);
    expect(state.messages[0]?.content).toEqual([{ type: "text", text: "Hello" }]);
  });

  it("marks required tool approvals", () => {
    const state = project([
      { id: "t1", type: "agent.tool_use", name: "bash", input: { command: "ls" } },
      { id: "i1", type: "session.status_idle", stop_reason: { type: "requires_action", event_ids: ["t1"] } }
    ]);
    expect(state.pendingApprovals).toEqual(["t1"]);
    expect(state.messages[0]?.content[0]).toMatchObject({ approval: { id: "t1" } });
  });

  it("marks a tool when the required-action status arrives before its event", () => {
    const state = project([
      { id: "i1", type: "session.status_idle", stop_reason: { type: "requires_action", event_ids: ["t1"] } },
      { id: "t1", type: "agent.tool_use", name: "bash", input: { command: "pwd" } }
    ]);
    expect(state.messages[0]?.content[0]).toMatchObject({
      toolCallId: "t1",
      approval: { id: "t1" }
    });
  });
});
