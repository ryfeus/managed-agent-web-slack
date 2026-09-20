from __future__ import annotations

from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from managed_agents_app.slack.blocks import denial_reason_modal


class SlackClient:
    def __init__(self, token: str, raw: WebClient | None = None) -> None:
        self.raw = raw or WebClient(token=token)

    @staticmethod
    def _require(response: Any, operation: str) -> Any:
        if not response.get("ok"):
            raise RuntimeError(f"Slack {operation} failed: {response.get('error', 'unknown error')}")
        return response

    def auth_test(self) -> dict[str, str]:
        response = self._require(self.raw.auth_test(), "auth.test")
        return {
            key: str(response[source])
            for key, source in (("team_id", "team_id"), ("bot_user_id", "user_id"))
            if response.get(source)
        }

    def post_reply(
        self, channel_id: str, thread_ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> str:
        response = self._require(
            self.raw.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=text,
                blocks=blocks,
                unfurl_links=False,
                unfurl_media=False,
            ),
            "chat.postMessage",
        )
        if not response.get("ts"):
            raise RuntimeError("Slack chat.postMessage returned no timestamp")
        return str(response["ts"])

    def update_message(
        self, channel_id: str, message_ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> None:
        self._require(
            self.raw.chat_update(channel=channel_id, ts=message_ts, text=text, blocks=blocks),
            "chat.update",
        )

    def set_agent_status(
        self,
        channel_id: str,
        thread_ts: str,
        status: str,
        *,
        title: str | None = None,
        initiator_user_id: str | None = None,
    ) -> None:
        self._require(
            self.raw.agents_sessions_setStatus(
                channel_id=channel_id,
                thread_ts=thread_ts,
                status=status,
                title=title,
                initiator_user_id=initiator_user_id,
            ),
            "agents.sessions.setStatus",
        )

    def rename_agent_session(self, channel_id: str, thread_ts: str, title: str) -> None:
        self._require(
            self.raw.agents_sessions_rename(channel_id=channel_id, thread_ts=thread_ts, title=title[:200]),
            "agents.sessions.rename",
        )

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
    ) -> str:
        if bool(initial_text) == bool(chunks):
            raise ValueError("Slack streams require exactly one of initial_text or chunks")
        payload: dict[str, Any] = {
            "channel": channel_id,
            "thread_ts": thread_ts,
            "recipient_team_id": team_id,
            "recipient_user_id": user_id,
        }
        if initial_text:
            payload["markdown_text"] = initial_text
        else:
            payload["chunks"] = chunks
            if task_display_mode:
                payload["task_display_mode"] = task_display_mode
        response = self._require(
            self.raw.chat_startStream(**payload),
            "chat.startStream",
        )
        if not response.get("ts"):
            raise RuntimeError("Slack chat.startStream returned no timestamp")
        return str(response["ts"])

    def append_stream(self, channel_id: str, message_ts: str, text: str) -> None:
        self._require(
            self.raw.chat_appendStream(channel=channel_id, ts=message_ts, markdown_text=text),
            "chat.appendStream",
        )

    def append_stream_chunks(self, channel_id: str, message_ts: str, chunks: list[dict[str, Any]]) -> None:
        if not chunks:
            raise ValueError("Slack stream chunks cannot be empty")
        self._require(
            self.raw.chat_appendStream(channel=channel_id, ts=message_ts, chunks=chunks),
            "chat.appendStream",
        )

    def stop_stream(
        self,
        channel_id: str,
        message_ts: str,
        *,
        text: str | None = None,
        chunks: list[dict[str, Any]] | None = None,
        blocks: list[dict[str, Any]] | None = None,
        status: str = "active",
    ) -> None:
        if text is not None and chunks is not None:
            raise ValueError("Slack stream stop cannot mix text and chunks")
        payload: dict[str, Any] = {
            "channel": channel_id,
            "ts": message_ts,
            "blocks": blocks,
            "session_status": status,
        }
        if text is not None:
            payload["markdown_text"] = text
        if chunks is not None:
            payload["chunks"] = chunks
        self._require(
            self.raw.chat_stopStream(**payload),
            "chat.stopStream",
        )

    def message_permalink(self, channel_id: str, message_ts: str) -> str:
        response = self._require(
            self.raw.chat_getPermalink(channel=channel_id, message_ts=message_ts),
            "chat.getPermalink",
        )
        permalink = response.get("permalink")
        if not permalink:
            raise RuntimeError("Slack chat.getPermalink returned no permalink")
        return str(permalink)

    def add_reaction(self, channel_id: str, message_ts: str, name: str) -> None:
        try:
            response = self.raw.reactions_add(channel=channel_id, timestamp=message_ts, name=name)
        except SlackApiError as error:
            if error.response.get("error") == "already_reacted":
                return
            raise
        if not response.get("ok") and response.get("error") == "already_reacted":
            return
        self._require(response, "reactions.add")

    def open_denial_modal(self, trigger_id: str, metadata: dict[str, str]) -> None:
        self._require(
            self.raw.views_open(trigger_id=trigger_id, view=denial_reason_modal(metadata)),
            "views.open",
        )

    def conversation_replies(self, channel_id: str, message_ts: str, limit: int = 50) -> list[dict[str, Any]]:
        response = self._require(
            self.raw.conversations_replies(channel=channel_id, ts=message_ts, limit=limit),
            "conversations.replies",
        )
        return list(response.get("messages") or [])

    def unfurl(self, channel_id: str, message_ts: str, unfurls: dict[str, Any]) -> None:
        self._require(
            self.raw.chat_unfurl(channel=channel_id, ts=message_ts, unfurls=unfurls),
            "chat.unfurl",
        )
