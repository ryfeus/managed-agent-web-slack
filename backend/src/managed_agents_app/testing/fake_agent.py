from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from copy import deepcopy
from threading import Condition
from time import monotonic
from typing import Any, Literal, cast

from managed_agents_app.models import SessionSummary
from managed_agents_app.ports.events import EventBus


class FakeManagedAgent:
    """Canonical history and broadcast subscriptions have independent lifetimes."""

    def __init__(self, events: EventBus) -> None:
        self.events = events
        self.condition = Condition()
        self.generation = 0
        self.sessions: dict[str, SessionSummary] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.subscribers: dict[str, list[deque[dict[str, Any]]]] = {}
        self.scripts: dict[str, dict[str, Any]] = {}
        self.approval_scripts: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, deque[dict[str, Any]]] = {}
        self.sent_messages: list[dict[str, Any]] = []
        self.interrupt_events: list[dict[str, Any]] | None = None
        self.confirmations: list[dict[str, Any]] = []
        self.interrupts: list[str] = []
        self.notifications: list[dict[str, Any]] = []
        self.counter = 0

    def _id(self, prefix: str = "evt") -> str:
        self.counter += 1
        return f"{prefix}_{self.counter:06d}"

    def script(self, prompt: str, events: list[dict[str, Any]], automatic: bool = True) -> None:
        with self.condition:
            self.scripts[prompt] = {"events": deepcopy(events), "automatic": automatic}

    def create_session(
        self,
        *,
        principal_id: str,
        surface: Literal["web", "slack"],
        creation_request_id: str,
        title: str | None = None,
        initial_text: str | None = None,
        system_context: str | None = None,
    ) -> SessionSummary:
        with self.condition:
            session = SessionSummary(
                id=self._id("sesn"),
                title=title or "New chat",
                status="idle",
                createdAt="2026-01-01T00:00:00Z",
                archivedAt=None,
                environmentId="env_e2e",
                metadata={
                    "owner_id": principal_id,
                    "surface": surface,
                    "creation_request_id": creation_request_id,
                },
            )
            self.sessions[session.id] = session
            self.history[session.id] = []
            self.subscribers[session.id] = []
            if initial_text:
                self.send_message(session.id, initial_text, system_context)
            return session.model_copy(deep=True)

    def retrieve_session(self, session_id: str) -> SessionSummary:
        with self.condition:
            return self.sessions[session_id].model_copy(deep=True)

    def list_sessions(self) -> list[SessionSummary]:
        with self.condition:
            return [s.model_copy(deep=True) for s in self.sessions.values()]

    def find_by_creation_request_id(self, request_id: str) -> SessionSummary | None:
        return next(
            (s for s in self.list_sessions() if s.metadata["creation_request_id"] == request_id), None
        )

    def list_events(self, session_id: str) -> list[dict[str, Any]]:
        with self.condition:
            return deepcopy(self.history[session_id])

    def emit(self, session_id: str, event: dict[str, Any]) -> str:
        with self.condition:
            event = deepcopy(event)
            event.setdefault("id", self._id())
            event.setdefault("created_at", "2026-01-01T00:00:00Z")
            event["session_id"] = session_id
            if event["type"] not in {"event_start", "event_delta"}:
                self.history[session_id].append(event)
            for queue in self.subscribers[session_id]:
                queue.append(deepcopy(event))
            kind = event["type"]
            if kind.startswith("session.status_"):
                self.sessions[session_id].status = kind.removeprefix("session.status_")
            if kind in {"session.status_idle", "session.status_terminated"}:
                self.notifications.append(
                    {
                        "webhookEventId": self._id("wh"),
                        "webhookType": "session.status_idled" if kind.endswith("idle") else kind,
                        "sessionId": session_id,
                    },
                )
            self.condition.notify_all()
            return str(event["id"])

    def send_message(self, session_id: str, text: str, system_context: str | None = None) -> str:
        with self.condition:
            self.sent_messages.append(
                {"session_id": session_id, "text": text, "system_context": system_context}
            )
            if system_context:
                self.emit(
                    session_id,
                    {"type": "system.message", "content": [{"type": "text", "text": system_context}]},
                )
            event_id = self.emit(
                session_id, {"type": "user.message", "content": [{"type": "text", "text": text}]}
            )
            self.emit(session_id, {"type": "session.status_running"})
            script = self.scripts.get(
                text,
                {
                    "events": [
                        {"type": "agent.message", "content": [{"type": "text", "text": f"Echo: {text}"}]},
                        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
                    ],
                    "automatic": True,
                },
            )
            self.pending.setdefault(session_id, deque()).extend(deepcopy(script["events"]))
            if script["automatic"]:
                self.advance(session_id)
            return event_id

    def flush_notifications(self) -> None:
        with self.condition:
            notifications, self.notifications = self.notifications, []
        for notification in notifications:
            self.events.publish("app.managed-agent", "ManagedAgentSessionChanged", notification)

    def advance(self, session_id: str, count: int | None = None) -> None:
        with self.condition:
            queue = self.pending.setdefault(session_id, deque())
            for _ in range(len(queue) if count is None else count):
                if not queue:
                    break
                self.emit(session_id, queue.popleft())

    def stream_events(
        self, session_id: str, *, include_thinking: bool = True, timeout_seconds: float = 45.0
    ) -> Iterator[dict[str, Any]]:
        # Register at method call, rather than the first iterator next().
        with self.condition:
            queue: deque[dict[str, Any]] = deque()
            self.subscribers[session_id].append(queue)
            generation = self.generation
            self.condition.notify_all()

        def iterate() -> Iterator[dict[str, Any]]:
            deadline = monotonic() + timeout_seconds
            try:
                while True:
                    with self.condition:
                        while not queue and generation == self.generation:
                            remaining = deadline - monotonic()
                            if remaining <= 0:
                                return
                            self.condition.wait(remaining)
                        if generation != self.generation:
                            return
                        item = queue.popleft()
                    if include_thinking or item["type"] != "agent.thinking":
                        yield item
                    if item["type"] in {"session.status_idle", "session.status_terminated"}:
                        return
            finally:
                with self.condition:
                    subscribers = self.subscribers.get(session_id, [])
                    subscribers[:] = [item for item in subscribers if item is not queue]
                    self.condition.notify_all()

        return iterate()

    def confirm_tool(
        self, session_id: str, tool_use_id: str, approved: bool, reason: str | None = None
    ) -> None:
        with self.condition:
            self.confirmations.append(
                {"session_id": session_id, "tool_use_id": tool_use_id, "approved": approved, "reason": reason}
            )
            self.emit(
                session_id,
                {
                    "type": "user.tool_confirmation",
                    "tool_use_id": tool_use_id,
                    "result": "allow" if approved else "deny",
                    "deny_message": reason,
                },
            )
            self.emit(session_id, {"type": "session.status_running"})
            script = self.approval_scripts.get(
                f"{tool_use_id}:{approved}",
                {
                    "events": [
                        {
                            "type": "agent.tool_result",
                            "tool_use_id": tool_use_id,
                            "content": [{"type": "text", "text": "Allowed" if approved else "Denied"}],
                            "is_error": not approved,
                        },
                        {
                            "type": "agent.message",
                            "content": [
                                {"type": "text", "text": "Done" if approved else f"Denied: {reason}"}
                            ],
                        },
                        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
                    ],
                    "automatic": True,
                },
            )
            self.pending[session_id] = deque(deepcopy(script["events"]))
            if script["automatic"]:
                self.advance(session_id)

    def interrupt(self, session_id: str) -> None:
        with self.condition:
            self.interrupts.append(session_id)
            self.pending[session_id] = deque()
            from managed_agents_app.managed_agent.events import pending_tool_ids

            pending_tools = pending_tool_ids(self.history[session_id])
            self.emit(session_id, {"type": "user.interrupt"})
            for tool_id in pending_tools:
                self.emit(
                    session_id,
                    {
                        "type": "agent.tool_result",
                        "tool_use_id": tool_id,
                        "is_error": True,
                        "content": [{"type": "text", "text": "Interrupted"}],
                    },
                )
            for event in self.interrupt_events or [
                {"type": "session.status_idle", "stop_reason": {"type": "interrupted"}}
            ]:
                self.emit(session_id, event)

    def archive(self, session_id: str) -> None:
        with self.condition:
            self.sessions[session_id].archivedAt = "2026-01-01T00:00:01Z"

    def update_title(self, session_id: str, title: str) -> None:
        with self.condition:
            self.sessions[session_id].title = title[:60]

    def snapshot(self) -> dict[str, Any]:
        with self.condition:
            return deepcopy(
                {
                    "sessions": [s.model_dump() for s in self.sessions.values()],
                    "events": self.history,
                    "sent_messages": self.sent_messages,
                    "confirmations": self.confirmations,
                    "interrupts": self.interrupts,
                    "subscribers": {s: len(q) for s, q in self.subscribers.items()},
                }
            )

    def reset(self) -> None:
        with self.condition:
            self.generation += 1
            for collection in [
                self.sessions,
                self.history,
                self.subscribers,
                self.scripts,
                self.approval_scripts,
                self.pending,
                self.sent_messages,
                self.confirmations,
                self.interrupts,
            ]:
                cast(Any, collection).clear()
            self.interrupt_events = None
            self.notifications.clear()
            self.counter = 0
            self.condition.notify_all()
