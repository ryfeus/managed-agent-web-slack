from __future__ import annotations

from typing import Any, Protocol


class SlackGateway(Protocol):
    def auth_test(self) -> dict[str, str]: ...

    def post_reply(
        self, channel_id: str, thread_ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> str: ...

    def update_message(
        self, channel_id: str, message_ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> None: ...

    def set_agent_status(
        self,
        channel_id: str,
        thread_ts: str,
        status: str,
        *,
        title: str | None = None,
        initiator_user_id: str | None = None,
    ) -> None: ...

    def rename_agent_session(self, channel_id: str, thread_ts: str, title: str) -> None: ...

    def start_stream(
        self,
        channel_id: str,
        thread_ts: str,
        team_id: str,
        user_id: str,
        initial_text: str = "",
        *,
        chunks: list[dict[str, Any]] | None = None,
        task_display_mode: str | None = None,
    ) -> str: ...

    def append_stream(self, channel_id: str, message_ts: str, text: str) -> None: ...

    def append_stream_chunks(
        self, channel_id: str, message_ts: str, chunks: list[dict[str, Any]]
    ) -> None: ...

    def stop_stream(
        self,
        channel_id: str,
        message_ts: str,
        *,
        text: str | None = None,
        chunks: list[dict[str, Any]] | None = None,
        blocks: list[dict[str, Any]] | None = None,
        status: str = "active",
    ) -> None: ...

    def message_permalink(self, channel_id: str, message_ts: str) -> str: ...

    def add_reaction(self, channel_id: str, message_ts: str, name: str) -> None: ...

    def open_denial_modal(self, trigger_id: str, metadata: dict[str, str]) -> None: ...

    def conversation_replies(
        self, channel_id: str, message_ts: str, limit: int = 50
    ) -> list[dict[str, Any]]: ...

    def unfurl(self, channel_id: str, message_ts: str, unfurls: dict[str, Any]) -> None: ...
