"""Provider-neutral asynchronous port for CMA controller execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from managed_agents_app.managed_agent.client import ManagedAgentClient
from managed_agents_app.models import SessionSummary


class CmaProvider(Protocol):
    async def create_session(
        self, context_id: str, creation_message_id: str, initial_text: str
    ) -> SessionSummary: ...
    async def find_session(self, context_id: str, creation_message_id: str) -> SessionSummary | None: ...
    async def retrieve_session(self, session_id: str) -> SessionSummary: ...
    async def list_events(self, session_id: str) -> list[dict[str, Any]]: ...
    async def send_message(self, session_id: str, text: str) -> str | None: ...
    async def confirm_tool(
        self, session_id: str, tool_use_id: str, approved: bool, reason: str | None = None
    ) -> None: ...
    async def send_custom_tool_result(
        self, session_id: str, custom_tool_use_id: str, answer: str, *, is_error: bool = False
    ) -> None: ...
    async def interrupt(self, session_id: str) -> None: ...
    def stream_events(self, session_id: str) -> AsyncIterator[dict[str, Any]]: ...


class DefinitiveProviderError(RuntimeError):
    """The provider rejected an operation without accepting its side effect."""


async def _invoke[T](operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return await asyncio.to_thread(operation, *args, **kwargs)
    except Exception as error:
        status = getattr(error, "status_code", None)
        if isinstance(status, int) and 400 <= status < 500 and status not in {408, 409, 429}:
            raise DefinitiveProviderError(type(error).__name__) from error
        raise


class AnthropicCmaProvider:
    def __init__(self, client: ManagedAgentClient) -> None:
        self.client = client

    async def create_session(
        self, context_id: str, creation_message_id: str, initial_text: str
    ) -> SessionSummary:
        return await _invoke(
            self.client.create_controller_session,
            context_id=context_id,
            creation_message_id=creation_message_id,
            initial_text=initial_text,
        )

    async def find_session(self, context_id: str, creation_message_id: str) -> SessionSummary | None:
        return await _invoke(self.client.find_controller_session, context_id, creation_message_id)

    async def retrieve_session(self, session_id: str) -> SessionSummary:
        return await _invoke(self.client.retrieve_session, session_id)

    async def list_events(self, session_id: str) -> list[dict[str, Any]]:
        return await _invoke(self.client.list_events, session_id)

    async def send_message(self, session_id: str, text: str) -> str | None:
        return await _invoke(self.client.send_message, session_id, text)

    async def confirm_tool(
        self, session_id: str, tool_use_id: str, approved: bool, reason: str | None = None
    ) -> None:
        await _invoke(self.client.confirm_tool, session_id, tool_use_id, approved, reason)

    async def send_custom_tool_result(
        self, session_id: str, custom_tool_use_id: str, answer: str, *, is_error: bool = False
    ) -> None:
        await _invoke(
            self.client.send_custom_tool_result,
            session_id,
            custom_tool_use_id,
            answer,
            is_error=is_error,
        )

    async def interrupt(self, session_id: str) -> None:
        await _invoke(self.client.interrupt, session_id)

    async def stream_events(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        # The SDK iterator is synchronous. A single bridge keeps the controller async.
        iterator = self.client.stream_events(session_id, include_thinking=False)
        sentinel = object()

        def next_event() -> object:
            try:
                return next(iterator)
            except StopIteration:
                return sentinel

        while (item := await asyncio.to_thread(next_event)) is not sentinel:
            assert isinstance(item, dict)
            yield item
