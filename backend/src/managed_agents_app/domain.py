from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BoundaryModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    def boundary(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class A2ATaskUpdated(BoundaryModel):
    version: Literal[1] = 1
    delivery_id: str = Field(alias="deliveryId", min_length=1)
    agent_id: str = Field(alias="agentId", min_length=1)
    task_id: str = Field(alias="taskId", min_length=1)
    thread_id: str = Field(alias="threadId", min_length=1)
    context_id: str = Field(alias="contextId", min_length=1)
    event_kind: Literal["task", "message", "status_update", "artifact_update"] = Field(alias="eventKind")
    task_state: str = Field(alias="taskState", min_length=1)


class SlackMessageReceived(BoundaryModel):
    version: Literal[1] = 1
    slack_event_id: str = Field(alias="slackEventId", min_length=1)
    team_id: str = Field(alias="teamId", min_length=1)
    channel_id: str = Field(alias="channelId", min_length=1)
    thread_ts: str = Field(alias="threadTs", min_length=1)
    message_ts: str = Field(alias="messageTs", min_length=1)
    user_id: str = Field(alias="userId", min_length=1)
    text: str
    command: Literal["message", "link"] = "message"
    link_thread_id: str | None = Field(default=None, alias="linkThreadId")
    channel_type: str | None = Field(default=None, alias="channelType")
    active_context: dict[str, Any] | None = Field(default=None, alias="activeContext")
    event_type: str = Field(default="app_mention", alias="eventType")


class SlackControlReplyRequested(BoundaryModel):
    version: Literal[1] = 1
    team_id: str = Field(alias="teamId")
    channel_id: str = Field(alias="channelId")
    thread_ts: str = Field(alias="threadTs")
    text: str = Field(min_length=1)


class SlackTaskProjectionRequested(BoundaryModel):
    version: Literal[1] = 1
    request_id: str = Field(alias="requestId")
    thread_id: str = Field(alias="threadId")
    task_id: str = Field(alias="taskId")
    binding_id: str = Field(alias="bindingId")
    team_id: str = Field(alias="teamId")
    channel_id: str = Field(alias="channelId")
    thread_ts: str = Field(alias="threadTs")
    user_id: str = Field(alias="userId")
    source_message_ts: str | None = Field(default=None, alias="sourceMessageTs")


class SlackWorkStarted(BoundaryModel):
    version: Literal[1] = 1
    request_id: str = Field(alias="requestId", min_length=1)
    team_id: str = Field(alias="teamId", min_length=1)
    channel_id: str = Field(alias="channelId", min_length=1)
    thread_ts: str = Field(alias="threadTs", min_length=1)
    message_ts: str = Field(alias="messageTs", min_length=1)
    user_id: str = Field(alias="userId", min_length=1)


class SlackThreadStopRequested(BoundaryModel):
    version: Literal[1] = 1
    event_id: str = Field(alias="eventId")
    team_id: str = Field(alias="teamId")
    channel_id: str = Field(alias="channelId")
    thread_ts: str = Field(alias="threadTs")
    user_id: str = Field(alias="userId")


class SlackToolConfirmationRequested(BoundaryModel):
    version: Literal[1] = 1
    interaction_id: str = Field(alias="interactionId")
    team_id: str = Field(alias="teamId")
    channel_id: str = Field(alias="channelId")
    thread_ts: str = Field(alias="threadTs")
    user_id: str = Field(alias="userId")
    task_id: str = Field(alias="taskId")
    input_request_id: str = Field(alias="inputRequestId")
    approved: bool
    reason: str | None = None
    response_message_ts: str | None = Field(default=None, alias="responseMessageTs")


class SlackFeedbackReceived(BoundaryModel):
    version: Literal[1] = 1
    interaction_id: str = Field(alias="interactionId")
    team_id: str = Field(alias="teamId")
    channel_id: str = Field(alias="channelId")
    thread_ts: str = Field(alias="threadTs")
    user_id: str = Field(alias="userId")
    rating: Literal["positive", "negative"]
    task_id: str = Field(alias="taskId")
    message_id: str | None = Field(default=None, alias="messageId")
    external_message_id: str | None = Field(default=None, alias="externalMessageId")
    reason: str | None = None


class SlackShortcutReceived(BoundaryModel):
    version: Literal[1] = 1
    interaction_id: str = Field(alias="interactionId")
    team_id: str = Field(alias="teamId")
    channel_id: str = Field(alias="channelId")
    message_ts: str = Field(alias="messageTs")
    thread_ts: str = Field(alias="threadTs")
    user_id: str = Field(alias="userId")
    callback_id: Literal["ask_agent", "summarize_thread", "investigate"] = Field(alias="callbackId")


class SlackThreadLinkRequested(BoundaryModel):
    version: Literal[1] = 1
    interaction_id: str = Field(alias="interactionId")
    team_id: str = Field(alias="teamId")
    channel_id: str = Field(alias="channelId")
    thread_ts: str = Field(alias="threadTs")
    user_id: str = Field(alias="userId")
    thread_id: str = Field(alias="threadId")


class SlackThreadLinkShared(BoundaryModel):
    version: Literal[1] = 1
    event_id: str = Field(alias="eventId")
    team_id: str = Field(alias="teamId")
    channel_id: str = Field(alias="channelId")
    message_ts: str = Field(alias="messageTs")
    user_id: str = Field(alias="userId")
    urls: list[str]


SLACK_MESSAGE_RECEIVED = "SlackMessageReceived"
SLACK_CONTROL_REPLY_REQUESTED = "SlackControlReplyRequested"
SLACK_TASK_PROJECTION_REQUESTED = "SlackTaskProjectionRequested"
SLACK_WORK_STARTED = "SlackWorkStarted"
SLACK_THREAD_STOP_REQUESTED = "SlackThreadStopRequested"
SLACK_TOOL_CONFIRMATION_REQUESTED = "SlackToolConfirmationRequested"
SLACK_FEEDBACK_RECEIVED = "SlackFeedbackReceived"
SLACK_SHORTCUT_RECEIVED = "SlackShortcutReceived"
SLACK_THREAD_LINK_REQUESTED = "SlackThreadLinkRequested"
SLACK_THREAD_LINK_SHARED = "SlackThreadLinkShared"
A2A_TASK_UPDATED = "A2ATaskUpdated"
