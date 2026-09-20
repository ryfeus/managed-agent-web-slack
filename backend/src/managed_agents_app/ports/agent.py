from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal, Protocol

from managed_agents_app.models import SessionSummary


class AgentGateway(Protocol):
    def create_session(
        self,
        *,
        principal_id: str,
        surface: Literal["web", "slack"],
        creation_request_id: str,
        title: str | None = None,
        initial_text: str | None = None,
        system_context: str | None = None,
    ) -> SessionSummary: ...

    def retrieve_session(self, session_id: str) -> SessionSummary: ...

    def list_sessions(self) -> list[SessionSummary]: ...

    def find_by_creation_request_id(self, request_id: str) -> SessionSummary | None: ...

    def list_events(self, session_id: str) -> list[dict[str, Any]]: ...

    def stream_events(
        self, session_id: str, *, include_thinking: bool = True, timeout_seconds: float = 45.0
    ) -> Iterator[dict[str, Any]]: ...

    def send_message(self, session_id: str, text: str, system_context: str | None = None) -> str | None: ...

    def confirm_tool(
        self, session_id: str, tool_use_id: str, approved: bool, reason: str | None = None
    ) -> None: ...

    def interrupt(self, session_id: str) -> None: ...

    def archive(self, session_id: str) -> None: ...

    def update_title(self, session_id: str, title: str) -> None: ...
