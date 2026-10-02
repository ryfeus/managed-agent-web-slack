"""Project current A2A Tasks into Slack; subscriptions only reduce latency."""

from __future__ import annotations

import asyncio
from typing import Any

from a2a.types import a2a_pb2 as a2a

from managed_agents_app.agent_control_plane.composition import application_control_plane
from managed_agents_app.config import AppConfig, load_config
from managed_agents_app.domain import (
    A2A_TASK_UPDATED,
    SLACK_CONTROL_REPLY_REQUESTED,
    SLACK_TASK_PROJECTION_REQUESTED,
    SLACK_WORK_STARTED,
    A2ATaskUpdated,
    SlackControlReplyRequested,
    SlackTaskProjectionRequested,
    SlackWorkStarted,
)
from managed_agents_app.logging import log
from managed_agents_app.ports.slack import SlackGateway
from managed_agents_app.protocols.human_input import ClarificationRequest, ToolApprovalRequest
from managed_agents_app.runtime import Runtime, get_runtime
from managed_agents_app.slack.a2a_projection import (
    agent_messages,
    is_settled,
    message_text,
    requests_from_task,
    task_slack_status,
    task_state,
)
from managed_agents_app.slack.blocks import (
    completed_task_chunk,
    feedback_blocks,
    markdown_text_chunk,
    source_link_blocks,
    tool_approval_blocks,
    working_task_chunk,
)


def handle_domain_event(runtime: Runtime, event: dict[str, Any]) -> None:
    asyncio.run(handle_domain_event_async(runtime, event))


async def handle_domain_event_async(runtime: Runtime, event: dict[str, Any]) -> None:
    kind = event.get("detail-type")
    detail = event.get("detail") or {}
    slack = _slack(runtime)
    if kind == SLACK_CONTROL_REPLY_REQUESTED:
        control = SlackControlReplyRequested.model_validate(detail)
        slack.post_reply(control.channel_id, control.thread_ts, control.text)
    elif kind == SLACK_WORK_STARTED:
        _work_started(runtime.config, slack, SlackWorkStarted.model_validate(detail))
    elif kind == SLACK_TASK_PROJECTION_REQUESTED:
        await _live_projection(runtime, slack, SlackTaskProjectionRequested.model_validate(detail))
    elif kind == A2A_TASK_UPDATED:
        await _task_updated(runtime, slack, A2ATaskUpdated.model_validate(detail))


def _work_started(config: AppConfig, slack: SlackGateway, detail: SlackWorkStarted) -> None:
    if config.slack_agent_view_enabled:
        slack.set_agent_status(
            detail.channel_id,
            detail.thread_ts,
            "processing",
            title="Claude thread",
            initiator_user_id=detail.user_id,
        )
    if config.slack_receipt_reaction_enabled:
        slack.add_reaction(detail.channel_id, detail.message_ts, config.slack_receipt_reaction)


def _task_cards_enabled(config: AppConfig) -> bool:
    if config.slack_task_cards_enabled and not config.slack_streaming_enabled:
        log("warning", "slack_task_cards_disabled_without_streaming")
        return False
    return config.slack_task_cards_enabled


def _split_thread(value: str) -> tuple[str, str]:
    channel_id, separator, thread_ts = value.partition(":")
    if not separator:
        raise ValueError("Invalid Slack external thread ID")
    return channel_id, thread_ts


def _source_permalink(
    config: AppConfig, slack: SlackGateway, channel_id: str, source_message_ts: str | None
) -> str | None:
    if not config.slack_source_links_enabled or not source_message_ts:
        return None
    try:
        return slack.message_permalink(channel_id, source_message_ts)
    except Exception as error:
        log(
            "warning",
            "slack_source_permalink_failed",
            channel_id=channel_id,
            source_message_ts=source_message_ts,
            error=str(error),
        )
        return None


async def _live_projection(
    runtime: Runtime, slack: SlackGateway, detail: SlackTaskProjectionRequested
) -> None:
    if not runtime.config.slack_streaming_enabled:
        return
    repo = runtime.slack_a2a
    projection = repo.ensure_projection(
        detail.binding_id, detail.task_id, detail.request_id, detail.source_message_ts, detail.user_id
    )
    claim = repo.claim_task_stream(detail.binding_id, detail.task_id, lease_seconds=840)
    if claim.status != "acquired":
        return
    assert claim.token
    stream_ts = projection["slack_message_ts"]
    state: str | None = None
    try:
        permalink = _source_permalink(runtime.config, slack, detail.channel_id, detail.source_message_ts)
        cards = _task_cards_enabled(runtime.config)
        if cards and not stream_ts:
            stream_ts = slack.start_stream(
                detail.channel_id,
                detail.thread_ts,
                detail.team_id,
                detail.user_id,
                chunks=[working_task_chunk(detail.request_id, permalink)],
                task_display_mode="timeline",
            )
            repo.update_stream_message(detail.binding_id, detail.task_id, claim.token, stream_ts)
        async with application_control_plane(runtime) as service:
            # SubscribeToTask sends a current Task first; it is not a replay log.
            async with asyncio.timeout(800):
                async for event in service.subscribe_task(detail.thread_id, detail.task_id, human_input=True):
                    kind = event.WhichOneof("payload")
                    if kind == "task":
                        task = event.task
                    else:
                        task = await service.get_task(detail.thread_id, detail.task_id, human_input=True)
                    state = task_state(task)
                    stream_ts = _project_task(
                        runtime,
                        slack,
                        detail.binding_id,
                        detail.task_id,
                        detail.channel_id,
                        detail.thread_ts,
                        task,
                        stream_ts=stream_ts,
                        live_token=claim.token,
                        request_id=detail.request_id,
                        permalink=permalink,
                        team_id=detail.team_id,
                    )
                    if is_settled(task):
                        break
        repo.finish_stream(detail.binding_id, detail.task_id, claim.token, fallback=False, state=state)
    except Exception as error:
        repo.finish_stream(
            detail.binding_id, detail.task_id, claim.token, fallback=True, state=state, error=str(error)
        )
        log(
            "error",
            "slack_a2a_stream_fallback",
            thread_id=detail.thread_id,
            task_id=detail.task_id,
            binding_id=detail.binding_id,
            error=str(error),
        )
        # A2ATaskUpdated retries against current Task after the lease is released.


async def _task_updated(runtime: Runtime, slack: SlackGateway, detail: A2ATaskUpdated) -> None:
    async with application_control_plane(runtime) as service:
        task = await service.get_task(detail.thread_id, detail.task_id, human_input=True)
    for binding in runtime.threads.list_surface_bindings(detail.thread_id):
        if binding["surface"] != "slack":
            continue
        binding_id = str(binding["binding_id"])
        channel_id, thread_ts = _split_thread(str(binding["external_thread_id"]))
        projection = runtime.slack_a2a.ensure_projection(binding_id, detail.task_id)
        if runtime.slack_a2a.active_stream(binding_id, detail.task_id):
            raise RuntimeError("Slack live projection is active; retry current Task later")
        permalink = _source_permalink(runtime.config, slack, channel_id, projection["source_message_ts"])
        _project_task(
            runtime,
            slack,
            binding_id,
            detail.task_id,
            channel_id,
            thread_ts,
            task,
            stream_ts=projection["slack_message_ts"] if projection["status"] != "completed" else None,
            request_id=projection["request_event_id"],
            permalink=permalink,
        )


def _project_task(
    runtime: Runtime,
    slack: SlackGateway,
    binding_id: str,
    task_id: str,
    channel_id: str,
    thread_ts: str,
    task: a2a.Task,
    *,
    stream_ts: str | None = None,
    live_token: str | None = None,
    request_id: str | None = None,
    permalink: str | None = None,
    team_id: str | None = None,
) -> str | None:
    repo = runtime.slack_a2a
    cards = _task_cards_enabled(runtime.config)
    for message in agent_messages(task):
        claim = repo.claim_item(binding_id, task_id, "agent_message", message.message_id)
        if claim.status == "busy":
            raise RuntimeError("Agent Message projection is busy")
        if claim.status != "acquired":
            continue
        assert claim.token
        text = message_text(message)
        if not text:
            log("warning", "slack_a2a_message_without_text", task_id=task_id, message_id=message.message_id)
            repo.finish_item(binding_id, task_id, "agent_message", message.message_id, claim.token, None)
            continue
        try:
            if stream_ts:
                if cards:
                    slack.append_stream_chunks(channel_id, stream_ts, [markdown_text_chunk(text)])
                else:
                    slack.append_stream(channel_id, stream_ts, text)
                message_ts = stream_ts
            elif live_token:
                projection = repo.get_projection(binding_id, task_id)
                user_id = str(projection["initiator_user_id"]) if projection else ""
                if not user_id:
                    raise RuntimeError("Live projection has no initiating user")
                if not team_id:
                    raise RuntimeError("Live projection has no Slack team")
                stream_ts = slack.start_stream(channel_id, thread_ts, team_id, user_id, text)
                repo.update_stream_message(binding_id, task_id, live_token, stream_ts)
                message_ts = stream_ts
            else:
                blocks = (
                    feedback_blocks(task_id, message.message_id)
                    if runtime.config.slack_feedback_enabled
                    else []
                )
                blocks += source_link_blocks(permalink)
                message_ts = slack.post_reply(channel_id, thread_ts, text, blocks or None)
            repo.finish_item(
                binding_id, task_id, "agent_message", message.message_id, claim.token, message_ts
            )
        except Exception as error:
            repo.finish_item(
                binding_id, task_id, "agent_message", message.message_id, claim.token, None, str(error)
            )
            raise
    if task.status.state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED:
        for request in requests_from_task(task):
            _project_human_input(runtime, slack, binding_id, task_id, channel_id, thread_ts, request)
    status = task_slack_status(task)
    if stream_ts and is_settled(task):
        blocks = source_link_blocks(permalink) if not cards else []
        messages = agent_messages(task)
        if runtime.config.slack_feedback_enabled and messages:
            blocks += feedback_blocks(task_id, messages[-1].message_id)
        slack.stop_stream(
            channel_id,
            stream_ts,
            chunks=[completed_task_chunk(request_id or task_id)] if cards and status != "suspended" else None,
            blocks=blocks or None,
            status=status,
        )
    if runtime.config.slack_agent_view_enabled:
        slack.set_agent_status(channel_id, thread_ts, status)
    repo.mark_projection(
        binding_id, task_id, task_state(task), "completed" if is_settled(task) else "pending"
    )
    return stream_ts


def _project_human_input(
    runtime: Runtime,
    slack: SlackGateway,
    binding_id: str,
    task_id: str,
    channel_id: str,
    thread_ts: str,
    request: ToolApprovalRequest | ClarificationRequest,
) -> None:
    repo = runtime.slack_a2a
    claim = repo.claim_item(binding_id, task_id, "human_input", request.request_id)
    if claim.status == "busy":
        raise RuntimeError("Human-input projection is busy")
    if claim.status != "acquired":
        return
    assert claim.token
    try:
        if isinstance(request, ToolApprovalRequest):
            if not runtime.config.slack_tool_approvals_enabled:
                repo.finish_item(binding_id, task_id, "human_input", request.request_id, claim.token, None)
                return
            blocks = tool_approval_blocks(
                task_id, request.request_id, request.tool.name, request.tool.arguments
            )
            text = f"Agent wants to run {request.tool.name}"
        else:
            blocks = None
            text = request.prompt
            log(
                "warning",
                "slack_clarification_response_unsupported",
                task_id=task_id,
                request_id=request.request_id,
            )
        message_ts = slack.post_reply(channel_id, thread_ts, text, blocks)
        repo.finish_item(binding_id, task_id, "human_input", request.request_id, claim.token, message_ts)
    except Exception as error:
        repo.finish_item(
            binding_id, task_id, "human_input", request.request_id, claim.token, None, str(error)
        )
        raise


def _slack(runtime: Runtime) -> SlackGateway:
    if not runtime.config.slack_bot_token:
        raise RuntimeError("Slack bot token is not configured")
    return runtime.slack


def handler(event: dict[str, Any], _context: Any) -> None:
    config = load_config()
    if config.app_env == "e2e":
        raise RuntimeError("E2E runtime is only available through local_api")
    return handle_domain_event(get_runtime(config), event)
