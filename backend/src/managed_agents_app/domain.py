from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BoundaryModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    def boundary(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


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
    link_session_id: str | None = Field(default=None, alias="linkSessionId")
    channel_type: str | None = Field(default=None, alias="channelType")
    active_context: dict[str, Any] | None = Field(default=None, alias="activeContext")
    event_type: str = Field(default="app_mention", alias="eventType")


class ManagedAgentSessionChanged(BoundaryModel):
    version: Literal[1] = 1
    webhook_event_id: str = Field(alias="webhookEventId", min_length=1)
    webhook_type: str = Field(alias="webhookType", min_length=1)
    session_id: str = Field(alias="sessionId", min_length=1)


class SlackControlReplyRequested(BoundaryModel):
    version: Literal[1] = 1
    team_id: str = Field(alias="teamId")
    channel_id: str = Field(alias="channelId")
    thread_ts: str = Field(alias="threadTs")
    text: str = Field(min_length=1)


class SlackProjectionRequested(BoundaryModel):
    version: Literal[1] = 1
    request_id: str = Field(alias="requestId")
    session_id: str = Field(alias="sessionId")
    binding_id: str = Field(alias="bindingId")
    team_id: str = Field(alias="teamId")
    channel_id: str = Field(alias="channelId")
    thread_ts: str = Field(alias="threadTs")
    user_id: str = Field(alias="userId")
    input_event_id: str | None = Field(default=None, alias="inputEventId")
    source_message_ts: str | None = Field(default=None, alias="sourceMessageTs")


class SlackWorkStarted(BoundaryModel):
    version: Literal[1] = 1
    request_id: str = Field(alias="requestId", min_length=1)
    team_id: str = Field(alias="teamId", min_length=1)
    channel_id: str = Field(alias="channelId", min_length=1)
    thread_ts: str = Field(alias="threadTs", min_length=1)
    message_ts: str = Field(alias="messageTs", min_length=1)
    user_id: str = Field(alias="userId", min_length=1)


class SlackSessionStopRequested(BoundaryModel):
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
    tool_use_id: str = Field(alias="toolUseId")
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
    managed_event_id: str | None = Field(default=None, alias="managedEventId")
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


class SlackSessionLinkRequested(BoundaryModel):
    version: Literal[1] = 1
    interaction_id: str = Field(alias="interactionId")
    team_id: str = Field(alias="teamId")
    channel_id: str = Field(alias="channelId")
    thread_ts: str = Field(alias="threadTs")
    user_id: str = Field(alias="userId")
    session_id: str = Field(alias="sessionId")


class SlackSessionLinkShared(BoundaryModel):
    version: Literal[1] = 1
    event_id: str = Field(alias="eventId")
    team_id: str = Field(alias="teamId")
    channel_id: str = Field(alias="channelId")
    message_ts: str = Field(alias="messageTs")
    user_id: str = Field(alias="userId")
    urls: list[str]


SLACK_MESSAGE_RECEIVED = "SlackMessageReceived"
MANAGED_AGENT_SESSION_CHANGED = "ManagedAgentSessionChanged"
SLACK_CONTROL_REPLY_REQUESTED = "SlackControlReplyRequested"
SLACK_PROJECTION_REQUESTED = "SlackProjectionRequested"
SLACK_WORK_STARTED = "SlackWorkStarted"
SLACK_SESSION_STOP_REQUESTED = "SlackSessionStopRequested"
SLACK_TOOL_CONFIRMATION_REQUESTED = "SlackToolConfirmationRequested"
SLACK_FEEDBACK_RECEIVED = "SlackFeedbackReceived"
SLACK_SHORTCUT_RECEIVED = "SlackShortcutReceived"
SLACK_SESSION_LINK_REQUESTED = "SlackSessionLinkRequested"
SLACK_SESSION_LINK_SHARED = "SlackSessionLinkShared"
