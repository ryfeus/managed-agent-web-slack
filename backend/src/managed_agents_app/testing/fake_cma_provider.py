"""Controllable CMA provider for A2A controller contract tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import uuid4

from managed_agents_app.cma_controller.provider import DefinitiveProviderError
from managed_agents_app.models import SessionSummary


class FakeCmaProvider:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionSummary] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.listeners: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self.create_calls = 0
        self.send_calls = 0
        self.confirm_calls = 0
        self.custom_result_calls = 0
        self.interrupt_calls = 0
        self.lose_create_response = False
        self.lose_send_response = False
        self.timeout_before_send_acceptance = False
        self.interrupt_preserves_approval = False
        self.lose_confirmation_response = False
        self.lose_interrupt_response = False
        self.timeout_before_confirmation_acceptance = False
        self.timeout_before_interrupt_acceptance = False
        self.definitive_send_failure = False
        self.automatic = True
        self.counter = 0
        self.instance_id = uuid4().hex[:8]
        self.scripted_turns: dict[str, list[dict[str, Any]]] = {}

    def script(self, prompt: str, events: list[dict[str, Any]]) -> None:
        self.scripted_turns[prompt] = deepcopy(events)

    def _complete_turn(self, session_id: str, prompt: str) -> None:
        scripted = next(
            (events for key, events in self.scripted_turns.items() if prompt == key or prompt.endswith(key)),
            None,
        )
        if scripted is None:
            self.finish(session_id)
        else:
            for event in scripted:
                self.emit(session_id, event)

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}_{self.instance_id}_{self.counter:06d}"

    def emit(self, session_id: str, event: dict[str, Any]) -> str:
        item = deepcopy(event)
        item.setdefault("id", self._id("evt"))
        self.events[session_id].append(item)
        for listener in self.listeners.get(session_id, []):
            listener.put_nowait(deepcopy(item))
        if item["type"] == "session.status_idle":
            self.sessions[session_id].status = "idle"
        elif item["type"] == "session.status_running":
            self.sessions[session_id].status = "running"
        return str(item["id"])

    def finish(self, session_id: str, text: str = "Done") -> None:
        self.emit(session_id, {"type": "agent.message", "content": [{"type": "text", "text": text}]})
        self.emit(session_id, {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}})

    async def create_session(
        self, context_id: str, creation_message_id: str, initial_text: str
    ) -> SessionSummary:
        self.create_calls += 1
        session = SessionSummary(
            id=self._id("sesn"),
            title="A2A conversation",
            status="running",
            createdAt="2026-01-01T00:00:00Z",
            archivedAt=None,
            environmentId="env_e2e",
            metadata={
                "controller": "a2a-cma",
                "a2a_context_id": context_id,
                "a2a_creation_message_id": creation_message_id,
            },
        )
        self.sessions[session.id] = session
        self.events[session.id] = []
        self.emit(session.id, {"type": "user.message", "content": [{"type": "text", "text": initial_text}]})
        self.emit(session.id, {"type": "session.status_running"})
        if self.automatic:
            self._complete_turn(session.id, initial_text)
        if self.lose_create_response:
            self.lose_create_response = False
            raise TimeoutError("response lost after provider create")
        return session.model_copy(deep=True)

    async def find_session(self, context_id: str, creation_message_id: str) -> SessionSummary | None:
        return next(
            (
                session.model_copy(deep=True)
                for session in self.sessions.values()
                if session.metadata.get("a2a_context_id") == context_id
                and session.metadata.get("a2a_creation_message_id") == creation_message_id
            ),
            None,
        )

    async def retrieve_session(self, session_id: str) -> SessionSummary:
        return self.sessions[session_id].model_copy(deep=True)

    async def list_events(self, session_id: str) -> list[dict[str, Any]]:
        return deepcopy(self.events[session_id])

    async def send_message(self, session_id: str, text: str) -> str | None:
        self.send_calls += 1
        if self.definitive_send_failure:
            self.definitive_send_failure = False
            raise DefinitiveProviderError("bad request")
        if self.timeout_before_send_acceptance:
            self.timeout_before_send_acceptance = False
            raise TimeoutError("provider acceptance is unknown")
        event_id = self.emit(
            session_id, {"type": "user.message", "content": [{"type": "text", "text": text}]}
        )
        self.emit(session_id, {"type": "session.status_running"})
        if self.automatic:
            self._complete_turn(session_id, text)
        if self.lose_send_response:
            self.lose_send_response = False
            raise TimeoutError("response lost after provider send")
        return event_id

    async def confirm_tool(
        self, session_id: str, tool_use_id: str, approved: bool, reason: str | None = None
    ) -> None:
        self.confirm_calls += 1
        if self.timeout_before_confirmation_acceptance:
            self.timeout_before_confirmation_acceptance = False
            raise TimeoutError("provider confirmation acceptance is unknown")
        confirmation = {
            "type": "user.tool_confirmation",
            "tool_use_id": tool_use_id,
            "result": "allow" if approved else "deny",
        }
        if reason:
            confirmation["deny_message"] = reason
        self.emit(session_id, confirmation)
        self.emit(session_id, {"type": "session.status_running"})
        if self.automatic:
            self.finish(session_id)
        if self.lose_confirmation_response:
            self.lose_confirmation_response = False
            raise TimeoutError("confirmation response lost")

    async def send_custom_tool_result(
        self, session_id: str, custom_tool_use_id: str, answer: str, *, is_error: bool = False
    ) -> None:
        self.custom_result_calls += 1
        self.emit(
            session_id,
            {
                "type": "user.custom_tool_result",
                "custom_tool_use_id": custom_tool_use_id,
                "content": [{"type": "text", "text": answer}],
                "is_error": is_error,
            },
        )
        self.emit(session_id, {"type": "session.status_running"})
        if self.automatic:
            self.finish(session_id)

    async def interrupt(self, session_id: str) -> None:
        self.interrupt_calls += 1
        if self.timeout_before_interrupt_acceptance:
            self.timeout_before_interrupt_acceptance = False
            raise TimeoutError("provider interrupt acceptance is unknown")
        self.emit(session_id, {"type": "user.interrupt"})
        if self.interrupt_preserves_approval:
            pending = [
                str(event["id"])
                for event in self.events[session_id]
                if event.get("type") in {"agent.tool_use", "agent.mcp_tool_use", "agent.custom_tool_use"}
            ]
            reason = {"type": "requires_action", "event_ids": pending}
        else:
            reason = {"type": "end_turn"}
        self.emit(session_id, {"type": "session.status_idle", "stop_reason": reason})
        if self.lose_interrupt_response:
            self.lose_interrupt_response = False
            raise TimeoutError("interrupt response lost")

    async def stream_events(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        listener: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.listeners.setdefault(session_id, []).append(listener)
        try:
            while True:
                yield await listener.get()
        finally:
            self.listeners[session_id].remove(listener)
