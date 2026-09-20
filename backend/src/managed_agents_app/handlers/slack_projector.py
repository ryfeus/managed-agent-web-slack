from __future__ import annotations

import time
from contextlib import suppress
from typing import Any

from managed_agents_app.config import AppConfig, load_config
from managed_agents_app.db import Database, ProjectionClaim
from managed_agents_app.domain import (
    MANAGED_AGENT_SESSION_CHANGED,
    SLACK_CONTROL_REPLY_REQUESTED,
    SLACK_PROJECTION_REQUESTED,
    SLACK_WORK_STARTED,
    ManagedAgentSessionChanged,
    SlackControlReplyRequested,
    SlackProjectionRequested,
    SlackWorkStarted,
)
from managed_agents_app.logging import log
from managed_agents_app.managed_agent.events import pending_tool_ids
from managed_agents_app.ports.slack import SlackGateway
from managed_agents_app.runtime import Runtime, get_runtime
from managed_agents_app.slack.blocks import (
    completed_task_chunk,
    feedback_blocks,
    markdown_text_chunk,
    source_link_blocks,
    tool_approval_blocks,
    working_task_chunk,
)


def handle_domain_event(runtime: Runtime, event: dict[str, Any]) -> None:
    config = runtime.config
    _task_cards_enabled(config)
    slack = _slack(runtime, config)
    detail_type = event.get("detail-type")
    detail = event.get("detail") or {}
    if detail_type == SLACK_CONTROL_REPLY_REQUESTED:
        control = SlackControlReplyRequested.model_validate(detail)
        slack.post_reply(control.channel_id, control.thread_ts, control.text)
        return
    if detail_type == SLACK_WORK_STARTED:
        _work_started(config, slack, SlackWorkStarted.model_validate(detail))
        return
    if detail_type == SLACK_PROJECTION_REQUESTED:
        projection = SlackProjectionRequested.model_validate(detail)
        if config.slack_agent_view_enabled:
            slack.set_agent_status(
                projection.channel_id,
                projection.thread_ts,
                "processing",
                title="Claude session",
                initiator_user_id=projection.user_id,
            )
        if config.slack_streaming_enabled:
            _live_stream(runtime, config, slack, projection)
        return
    if detail_type == MANAGED_AGENT_SESSION_CHANGED:
        _project_completed(runtime, config, slack, ManagedAgentSessionChanged.model_validate(detail))


def _work_started(config: AppConfig, slack: SlackGateway, detail: SlackWorkStarted) -> None:
    if config.slack_agent_view_enabled:
        slack.set_agent_status(
            detail.channel_id,
            detail.thread_ts,
            "processing",
            title="Claude session",
            initiator_user_id=detail.user_id,
        )
    if config.slack_receipt_reaction_enabled:
        slack.add_reaction(detail.channel_id, detail.message_ts, config.slack_receipt_reaction)


def _live_stream(
    runtime: Runtime, config: AppConfig, slack: SlackGateway, detail: SlackProjectionRequested
) -> None:
    db = runtime.db
    claim = db.claim_stream(detail.binding_id, detail.request_id, detail.session_id)
    if claim != ProjectionClaim.ACQUIRED:
        return
    managed = runtime.agent
    stream_ts: str | None = None
    buffer = ""
    final_event_id: str | None = None
    final_text = ""
    text_emitted = False
    last_flush = time.monotonic()
    task_cards = _task_cards_enabled(config)
    permalink = _source_permalink(config, slack, detail)
    try:
        if task_cards:
            stream_ts = slack.start_stream(
                detail.channel_id,
                detail.thread_ts,
                detail.team_id,
                detail.user_id,
                chunks=[working_task_chunk(detail.request_id, permalink)],
                task_display_mode="timeline",
            )
            db.update_stream(detail.binding_id, detail.request_id, slack_message_ts=stream_ts)
        listed_events = managed.list_events(detail.session_id)
        completed_message, pending, turn_complete = _turn_completion(listed_events, detail.input_event_id)
        if completed_message:
            final_event_id = str(completed_message["id"])
            final_text = _text(completed_message)
        if not turn_complete:
            stream_error: Exception | None = None
            try:
                for event in managed.stream_events(
                    detail.session_id, include_thinking=False, timeout_seconds=10.0
                ):
                    event_type = event.get("type")
                    if event.get("id"):
                        db.update_stream(
                            detail.binding_id, detail.request_id, last_managed_event_id=str(event["id"])
                        )
                    if event_type == "event_delta":
                        delta = event.get("delta") or {}
                        content = delta.get("content") or {}
                        if content.get("type") == "text" and isinstance(content.get("text"), str):
                            buffer += content["text"]
                            if not stream_ts or len(buffer) >= 120 or time.monotonic() - last_flush >= 0.4:
                                stream_ts = _flush(slack, detail, stream_ts, buffer, task_cards=task_cards)
                                db.update_stream(
                                    detail.binding_id, detail.request_id, slack_message_ts=stream_ts
                                )
                                text_emitted = True
                                buffer = ""
                                last_flush = time.monotonic()
                    elif event_type == "agent.message":
                        final_event_id = str(event["id"]) if event.get("id") else None
                        final_text = _text(event)
                        break
                    elif event_type in {"session.status_idle", "session.status_terminated"}:
                        break
            except Exception as error:
                stream_error = error
            listed_events = managed.list_events(detail.session_id)
            listed_message, pending, turn_complete = _turn_completion(listed_events, detail.input_event_id)
            if not final_text and listed_message:
                final_event_id = str(listed_message["id"])
                final_text = _text(listed_message)
            if stream_error and (not turn_complete) and (not final_text) and (not pending):
                raise stream_error
        if buffer:
            stream_ts = _flush(slack, detail, stream_ts, buffer, task_cards=task_cards)
            db.update_stream(detail.binding_id, detail.request_id, slack_message_ts=stream_ts)
            text_emitted = True
        if not text_emitted and final_text:
            stream_ts = _flush(slack, detail, stream_ts, final_text, task_cards=task_cards)
            db.update_stream(detail.binding_id, detail.request_id, slack_message_ts=stream_ts)
            text_emitted = True
        if stream_ts:
            blocks = [] if task_cards else source_link_blocks(permalink)
            if config.slack_feedback_enabled and final_event_id:
                blocks.extend(feedback_blocks(final_event_id))
            slack.stop_stream(
                detail.channel_id,
                stream_ts,
                chunks=[completed_task_chunk(detail.request_id)] if task_cards and (not pending) else None,
                blocks=blocks or None,
                status="suspended" if pending else "active",
            )
        if final_event_id and stream_ts:
            runtime.faults.hit("before_projection_marked", detail.request_id)
            db.mark_projected(detail.binding_id, final_event_id, stream_ts)
        db.complete_stream(detail.binding_id, detail.request_id, final_event_id, stream_ts)
        if pending and config.slack_tool_approvals_enabled:
            _project_tools(db, slack, detail.binding_id, detail.channel_id, detail.thread_ts, pending)
            if config.slack_agent_view_enabled:
                slack.set_agent_status(detail.channel_id, detail.thread_ts, "suspended")
        elif config.slack_agent_view_enabled:
            slack.set_agent_status(detail.channel_id, detail.thread_ts, "active")
    except Exception as error:
        db.complete_stream(
            detail.binding_id, detail.request_id, final_event_id, stream_ts, fallback=True, error=str(error)
        )
        if config.slack_agent_view_enabled:
            with suppress(Exception):
                slack.set_agent_status(detail.channel_id, detail.thread_ts, "active")
        log(
            "error",
            "slack_stream_recoverable",
            session_id=detail.session_id,
            binding_id=detail.binding_id,
            error=str(error),
        )


def _flush(
    slack: SlackGateway,
    detail: SlackProjectionRequested,
    stream_ts: str | None,
    text: str,
    *,
    task_cards: bool,
) -> str:
    if task_cards:
        if not stream_ts:
            raise RuntimeError("Task-card stream was not initialized")
        slack.append_stream_chunks(detail.channel_id, stream_ts, [markdown_text_chunk(text)])
        return stream_ts
    if not stream_ts:
        return slack.start_stream(detail.channel_id, detail.thread_ts, detail.team_id, detail.user_id, text)
    slack.append_stream(detail.channel_id, stream_ts, text)
    return stream_ts


def _task_cards_enabled(config: AppConfig) -> bool:
    if config.slack_task_cards_enabled and (not config.slack_streaming_enabled):
        log("warning", "slack_task_cards_disabled_without_streaming")
        return False
    return config.slack_task_cards_enabled


def _source_permalink(config: AppConfig, slack: SlackGateway, detail: SlackProjectionRequested) -> str | None:
    if not config.slack_source_links_enabled or not detail.source_message_ts:
        return None
    try:
        return slack.message_permalink(detail.channel_id, detail.source_message_ts)
    except Exception as error:
        log(
            "warning",
            "slack_source_permalink_failed",
            request_id=detail.request_id,
            channel_id=detail.channel_id,
            source_message_ts=detail.source_message_ts,
            error=str(error),
        )
        return None


def _project_completed(
    runtime: Runtime, config: AppConfig, slack: SlackGateway, detail: ManagedAgentSessionChanged
) -> None:
    db = runtime.db
    bindings = db.list_slack_bindings(detail.session_id)
    if not bindings:
        return
    managed = runtime.agent
    events = managed.list_events(detail.session_id)
    messages = [item for item in events if item.get("type") == "agent.message" and item.get("id")]
    pending = _pending_tools(events)
    for binding in bindings:
        binding_id = str(binding["binding_id"])
        channel_id, thread_ts = _split_thread(str(binding["external_thread_id"]))
        if db.has_active_stream(binding_id, detail.session_id):
            log(
                "info",
                "slack_webhook_deferred_for_active_stream",
                session_id=detail.session_id,
                binding_id=binding_id,
            )
            raise RuntimeError("Slack live stream is still active")
        for message in messages:
            event_id = str(message["id"])
            if db.claim_projection(binding_id, event_id) != ProjectionClaim.ACQUIRED:
                continue
            text = _text(message)
            if not text:
                db.fail_projection(
                    binding_id, event_id, "Agent message did not contain Slack-renderable text"
                )
                continue
            blocks = feedback_blocks(event_id) if config.slack_feedback_enabled else None
            try:
                recovered = db.find_recoverable_stream(binding_id, detail.session_id)
                if recovered:
                    request_id, slack_message_ts = recovered
                    task_cards = _task_cards_enabled(config)
                    slack.stop_stream(
                        channel_id,
                        slack_message_ts,
                        text=None if task_cards else text,
                        chunks=[markdown_text_chunk(text)]
                        + ([] if pending else [completed_task_chunk(request_id)])
                        if task_cards
                        else None,
                        blocks=blocks,
                        status="suspended" if pending else "active",
                    )
                    db.complete_recovered_stream(binding_id, request_id, event_id, slack_message_ts)
                else:
                    slack_message_ts = slack.post_reply(channel_id, thread_ts, text, blocks)
                    db.mark_projected(binding_id, event_id, slack_message_ts)
            except Exception as error:
                db.fail_projection(binding_id, event_id, str(error))
                raise
        if pending and config.slack_tool_approvals_enabled:
            _project_tools(db, slack, binding_id, channel_id, thread_ts, pending)
            if config.slack_agent_view_enabled:
                slack.set_agent_status(channel_id, thread_ts, "suspended")
        elif config.slack_agent_view_enabled:
            status = "closed" if detail.webhook_type == "session.status_terminated" else "active"
            slack.set_agent_status(channel_id, thread_ts, status)


def _project_tools(
    db: Database,
    slack: SlackGateway,
    binding_id: str,
    channel_id: str,
    thread_ts: str,
    tools: list[dict[str, Any]],
) -> None:
    for tool in tools:
        event_id = str(tool["id"])
        if db.claim_projection(binding_id, event_id) != ProjectionClaim.ACQUIRED:
            continue
        try:
            message_ts = slack.post_reply(
                channel_id,
                thread_ts,
                f"Agent wants to run {tool.get('name', 'a tool')}",
                tool_approval_blocks(event_id, str(tool.get("name") or "tool"), tool.get("input") or {}),
            )
            db.mark_projected(binding_id, event_id, message_ts)
        except Exception as error:
            db.fail_projection(binding_id, event_id, str(error))
            raise


def _pending_tools(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending_ids = pending_tool_ids(events)
    return [
        event
        for event in events
        if event.get("id") in pending_ids
        and event.get("type") in {"agent.tool_use", "agent.mcp_tool_use", "agent.custom_tool_use"}
    ]


def _turn_completion(
    events: list[dict[str, Any]], input_event_id: str | None
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    start = -1
    if input_event_id:
        start = next((index for index, event in enumerate(events) if event.get("id") == input_event_id), -1)
    if start < 0:
        start = next(
            (
                index
                for index in range(len(events) - 1, -1, -1)
                if events[index].get("type") == "user.message"
            ),
            -1,
        )
    turn_events = events[start + 1 :]
    complete = any(
        event.get("type")
        in {"session.status_idle", "session.thread_status_idle", "session.status_terminated"}
        for event in turn_events
    )
    message = next(
        (
            event
            for event in reversed(turn_events)
            if event.get("type") == "agent.message" and event.get("id")
        ),
        None,
    )
    return (message if complete else None, _pending_tools(events), complete)


def _text(event: dict[str, Any]) -> str:
    content = event.get("content")
    if not isinstance(content, list):
        return ""
    return "\n\n".join(
        str(block["text"])
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ).strip()


def _split_thread(value: str) -> tuple[str, str]:
    channel_id, separator, thread_ts = value.partition(":")
    if not separator:
        raise ValueError("Invalid Slack external thread ID")
    return (channel_id, thread_ts)


def _slack(runtime: Runtime, config: AppConfig) -> SlackGateway:
    if not config.slack_bot_token:
        raise RuntimeError("Slack bot token is not configured")
    return runtime.slack


def handler(event: dict[str, Any], _context: Any) -> None:
    config = load_config()
    if config.app_env == "e2e":
        raise RuntimeError("E2E runtime is only available through local_api")
    return handle_domain_event(get_runtime(config), event)
