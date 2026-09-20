from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any, cast

from managed_agents_app.slack.client import SlackClient


class RecordingSlack(SlackClient):
    """Exercise the real wrapper against an in-memory Slack API recorder."""

    def __init__(self) -> None:
        self.lock = RLock()
        self.calls: list[dict[str, Any]] = []
        self.messages: dict[str, dict[str, Any]] = {}
        self.streams: dict[str, dict[str, Any]] = {}
        self.reactions: list[dict[str, Any]] = []
        self.statuses: list[dict[str, Any]] = []
        self.modals: list[dict[str, Any]] = []
        self.unfurls: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.counter = 0
        super().__init__("e2e", raw=cast(Any, _Recorder(self)))

    def fail_next(
        self, operation: str, error: str = "rate_limited", *, after: bool = False, skip: int = 0
    ) -> None:
        with self.lock:
            self.failures.append({"operation": operation, "error": error, "after": after, "skip": skip})

    def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            record = {"operation": operation, "payload": deepcopy(payload)}
            self.calls.append(record)
            failure = next((f for f in self.failures if f["operation"] == operation), None)
            if failure and failure["skip"]:
                failure["skip"] -= 1
                failure = None
            elif failure:
                self.failures.remove(failure)
            if failure and not failure["after"]:
                record["error"] = failure["error"]
                raise RuntimeError(failure["error"])
            result: dict[str, Any] = {"ok": True}
            if operation in {"chat.postMessage", "chat.startStream"}:
                self.counter += 1
                ts = f"1000.{self.counter:06d}"
                message = {**deepcopy(payload), "ts": ts, "text": payload.get("text", "")}
                self.messages[ts] = message
                if operation == "chat.startStream":
                    message.update(
                        mode="chunks" if "chunks" in payload else "text",
                        state="streaming",
                        text="",
                        chunks=[],
                    )
                    self._append(message, payload)
                    self.streams[ts] = message
                result["ts"] = ts
            elif operation in {"chat.appendStream", "chat.stopStream"}:
                stream = self.streams[payload["ts"]]
                if stream["channel"] != payload["channel"] or stream["state"] != "streaming":
                    raise RuntimeError("message_not_in_streaming_state")
                self._append(stream, payload)
                if operation == "chat.stopStream":
                    stream.update(
                        state="completed", status=payload["session_status"], blocks=payload.get("blocks")
                    )
            elif operation == "chat.update":
                self.messages[payload["ts"]].update(deepcopy(payload))
            elif operation == "chat.getPermalink":
                timestamp = payload["message_ts"].replace(".", "")
                result["permalink"] = f"https://e2e.slack.test/archives/{payload['channel']}/p{timestamp}"
            elif operation == "reactions.add":
                if payload not in self.reactions:
                    self.reactions.append(deepcopy(payload))
            elif operation == "agents.sessions.setStatus":
                self.statuses.append(deepcopy(payload))
            elif operation == "views.open":
                self.modals.append(deepcopy(payload))
            elif operation == "chat.unfurl":
                self.unfurls.append(deepcopy(payload))
            elif operation == "conversations.replies":
                result["messages"] = [
                    deepcopy(m)
                    for m in self.messages.values()
                    if m["channel"] == payload["channel"] and m.get("thread_ts") == payload["ts"]
                ]
            elif operation == "auth.test":
                result.update(team_id="T001", user_id="BOT")
            elif operation != "agents.sessions.rename":
                raise ValueError(f"Unsupported Slack operation: {operation}")
            if failure:
                record["error"] = failure["error"]
                raise RuntimeError(failure["error"])
            return result

    @staticmethod
    def _append(stream: dict[str, Any], payload: dict[str, Any]) -> None:
        if "markdown_text" in payload:
            if stream["mode"] != "text":
                raise ValueError("Cannot mix text and chunk stream modes")
            stream["text"] += payload["markdown_text"]
        if "chunks" in payload:
            if stream["mode"] != "chunks":
                raise ValueError("Cannot mix text and chunk stream modes")
            stream["chunks"].extend(deepcopy(payload["chunks"]))
            stream["text"] += "".join(
                c.get("text", "") for c in payload["chunks"] if c["type"] == "markdown_text"
            )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return deepcopy(
                {
                    "calls": self.calls,
                    "messages": list(self.messages.values()),
                    "streams": list(self.streams.values()),
                    "reactions": self.reactions,
                    "agent_statuses": self.statuses,
                    "modals": self.modals,
                    "unfurls": self.unfurls,
                }
            )

    def reset(self) -> None:
        with self.lock:
            for collection in [
                self.calls,
                self.messages,
                self.streams,
                self.reactions,
                self.statuses,
                self.modals,
                self.unfurls,
                self.failures,
            ]:
                cast(Any, collection).clear()
            self.counter = 0


class _Recorder:
    def __init__(self, slack: RecordingSlack) -> None:
        self.slack = slack

    def __getattr__(self, name: str) -> Any:
        return lambda **payload: self.slack.call(name.replace("_", "."), payload)
