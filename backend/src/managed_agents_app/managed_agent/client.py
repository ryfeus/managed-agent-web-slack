from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal, cast

from anthropic import Anthropic

from managed_agents_app.config import AppConfig
from managed_agents_app.models import SessionSummary


def as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, dict):
        return value
    raise TypeError(f"Unsupported SDK response type: {type(value)!r}")


class ManagedAgentClient:
    def __init__(self, config: AppConfig, raw: Any | None = None) -> None:
        if not config.anthropic_api_key:
            raise RuntimeError("Missing Anthropic API key")
        if not config.agent_id:
            raise RuntimeError("Missing Claude agent ID")
        if not config.environment_id:
            raise RuntimeError("Missing Claude environment ID")
        self.config = config
        self.raw = raw or Anthropic(api_key=config.anthropic_api_key)

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
        params: dict[str, Any] = {
            "agent": self.config.agent_id,
            "environment_id": self.config.environment_id,
            "title": title or "New chat",
            "metadata": {
                "application": self.config.app_name,
                "owner_id": principal_id,
                "surface": surface,
                "creation_request_id": creation_request_id,
            },
        }
        if initial_text:
            first_turn = (
                f"{system_context}\n\nUser request:\n{initial_text}" if system_context else initial_text
            )
            params["initial_events"] = [
                {"type": "user.message", "content": [{"type": "text", "text": first_turn}]}
            ]
        return self._session(self.raw.beta.sessions.create(**params))

    def retrieve_session(self, session_id: str) -> SessionSummary:
        return self._session(self.raw.beta.sessions.retrieve(session_id))

    def list_sessions(self) -> list[SessionSummary]:
        page = self.raw.beta.sessions.list(agent_id=self.config.agent_id, limit=100)
        return [self._session(item) for item in page]

    def find_by_creation_request_id(self, request_id: str) -> SessionSummary | None:
        return next(
            (item for item in self.list_sessions() if item.metadata.get("creation_request_id") == request_id),
            None,
        )

    def list_events(self, session_id: str) -> list[dict[str, Any]]:
        return [as_dict(event) for event in self.raw.beta.sessions.events.list(session_id, limit=100)]

    def stream_events(
        self,
        session_id: str,
        *,
        include_thinking: bool = True,
        timeout_seconds: float = 45.0,
    ) -> Iterator[dict[str, Any]]:
        delta_types: list[Literal["agent.message", "agent.thinking"]] = (
            ["agent.message", "agent.thinking"] if include_thinking else ["agent.message"]
        )
        stream = self.raw.beta.sessions.events.stream(
            session_id, event_deltas=delta_types, timeout=timeout_seconds
        )
        for event in stream:
            yield as_dict(event)

    def send_message(self, session_id: str, text: str, system_context: str | None = None) -> str | None:
        events: list[dict[str, Any]] = []
        if system_context:
            events.append({"type": "system.message", "content": [{"type": "text", "text": system_context}]})
        events.append({"type": "user.message", "content": [{"type": "text", "text": text}]})
        response = self.raw.beta.sessions.events.send(session_id, events=cast(Any, events))
        data = as_dict(response).get("data") or []
        return str(data[0]["id"]) if data and data[0].get("id") else None

    def confirm_tool(
        self, session_id: str, tool_use_id: str, approved: bool, reason: str | None = None
    ) -> None:
        event: dict[str, Any] = {
            "type": "user.tool_confirmation",
            "tool_use_id": tool_use_id,
            "result": "allow" if approved else "deny",
        }
        if not approved and reason:
            event["deny_message"] = reason
        self.raw.beta.sessions.events.send(session_id, events=cast(Any, [event]))

    def interrupt(self, session_id: str) -> None:
        self.raw.beta.sessions.events.send(session_id, events=[{"type": "user.interrupt"}])

    def archive(self, session_id: str) -> None:
        self.raw.beta.sessions.archive(session_id)

    def update_title(self, session_id: str, title: str) -> None:
        self.raw.beta.sessions.update(session_id, title=title[:60])

    @staticmethod
    def _session(raw: Any) -> SessionSummary:
        value = as_dict(raw)
        return SessionSummary(
            id=str(value["id"]),
            title=value.get("title"),
            status=str(value["status"]),
            createdAt=str(value["created_at"]),
            archivedAt=str(value["archived_at"]) if value.get("archived_at") else None,
            environmentId=str(value["environment_id"]),
            metadata={str(key): str(item) for key, item in (value.get("metadata") or {}).items()},
        )
