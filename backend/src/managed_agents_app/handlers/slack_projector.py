"""Project canonical A2A Tasks into Slack on initial setup and standard A2A push."""

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
from managed_agents_app.observability import emit
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
    markdown_text_chunk,
    response_text_blocks,
    settled_task_chunk,
    source_link_blocks,
    tool_approval_blocks,
    working_task_chunk,
)
from managed_agents_app.slack.preview import SlackPreviewHint


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
        await _initial_projection(runtime, slack, SlackTaskProjectionRequested.model_validate(detail))
    elif kind == A2A_TASK_UPDATED:
        await _task_updated(runtime, slack, A2ATaskUpdated.model_validate(detail))


def _work_started(config: AppConfig, slack: SlackGateway, detail: SlackWorkStarted) -> None:
    slack.set_agent_status(
        detail.channel_id,
        detail.thread_ts,
        "processing",
        title="Claude thread",
        initiator_user_id=detail.user_id,
    )
    slack.add_reaction(detail.channel_id, detail.message_ts, config.slack_receipt_reaction)


def _split_thread(value: str) -> tuple[str, str]:
    channel_id, separator, thread_ts = value.partition(":")
    if not separator:
        raise ValueError("Invalid Slack external thread ID")
    return channel_id, thread_ts


def _source_permalink(
    config: AppConfig, slack: SlackGateway, channel_id: str, source_message_ts: str | None
) -> str | None:
    if not config.public_app_url or not source_message_ts:
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


async def _initial_projection(
    runtime: Runtime, slack: SlackGateway, detail: SlackTaskProjectionRequested
) -> None:
    runtime.slack_a2a.ensure_projection(
        detail.binding_id, detail.task_id, detail.request_id, detail.source_message_ts, detail.user_id
    )
    await _reconcile_projection(
        runtime,
        slack,
        detail.thread_id,
        detail.binding_id,
        detail.task_id,
        detail.channel_id,
        detail.thread_ts,
        detail.team_id,
    )


async def _task_updated(runtime: Runtime, slack: SlackGateway, detail: A2ATaskUpdated) -> None:
    for binding in runtime.threads.list_surface_bindings(detail.thread_id):
        if binding["surface"] != "slack":
            continue
        binding_id = str(binding["binding_id"])
        channel_id, thread_ts = _split_thread(str(binding["external_thread_id"]))
        runtime.slack_a2a.ensure_projection(binding_id, detail.task_id)
        await _reconcile_projection(
            runtime,
            slack,
            detail.thread_id,
            binding_id,
            detail.task_id,
            channel_id,
            thread_ts,
            str(binding["tenant_id"]),
        )


async def _reconcile_projection(
    runtime: Runtime,
    slack: SlackGateway,
    thread_id: str,
    binding_id: str,
    task_id: str,
    channel_id: str,
    thread_ts: str,
    team_id: str,
) -> None:
    repo = runtime.slack_a2a
    # Serialize only a current Task read and Slack writes, never agent execution.
    claim = repo.claim_task_stream(binding_id, task_id)
    if claim.status != "acquired":
        raise RuntimeError("Slack projection is busy; retry current Task later")
    assert claim.token and claim.row
    projection = claim.row
    state = projection["last_task_state"]
    new_stream = None
    try:
        # Read inside the claim: an older delivery cannot overwrite a newer projection.
        async with application_control_plane(runtime) as service:
            task = await service.get_task(thread_id, task_id, human_input=True)
        state = task_state(task)
        emit("projection_snapshot", task_id=task_id, state=state)
        stream_ts = projection["slack_message_ts"]
        permalink = _source_permalink(runtime.config, slack, channel_id, projection["source_message_ts"])
        # Only a Task that has never settled can start a card. Late setup after a
        # terminal push (or clarification boundary) must not reopen a stopped stream.
        can_start = projection["last_task_state"] in {None, "SUBMITTED", "WORKING"} and bool(
            projection["initiator_user_id"]
        )
        if can_start and not stream_ts and not repo.has_human_input_receipt(binding_id, task_id):
            try:
                stream_ts = slack.start_stream(
                    channel_id,
                    thread_ts,
                    team_id,
                    projection["initiator_user_id"],
                    chunks=[working_task_chunk(projection["request_event_id"] or task_id, permalink)],
                    task_display_mode="timeline",
                )
                repo.update_stream_message(binding_id, task_id, claim.token, stream_ts)
                new_stream = stream_ts
            except Exception as error:
                response = getattr(error, "response", None)
                code = response.get("error") if response is not None else None
                log(
                    "warning",
                    "slack_stream_open_failed",
                    error_type=type(error).__name__,
                    error_code=code if isinstance(code, str) and code.isidentifier() else "unknown",
                    action="Check installed Slack scopes and reinstall the manifest for missing_scope",
                )
                stream_ts = None
        _project_task(
            runtime,
            slack,
            binding_id,
            task_id,
            channel_id,
            thread_ts,
            task,
            stream_ts=stream_ts,
            request_id=projection["request_event_id"],
            permalink=permalink,
        )
        repo.finish_stream(binding_id, task_id, claim.token, fallback=False, state=state)
    except Exception as error:
        repo.finish_stream(binding_id, task_id, claim.token, fallback=True, state=state, error=str(error))
        # EventBridge retries projection only; it never resubmits the CMA turn.
        raise
    # P1 network/configuration failures must never re-enter the P0 failure path.
    if (
        new_stream
        and state in {"SUBMITTED", "WORKING"}
        and runtime.config.preview_enabled
        and runtime.config.slack_preview_enabled
    ):
        try:
            if trigger := runtime.slack_preview_trigger:
                trigger.start(SlackPreviewHint(binding_id, task_id, new_stream))
        except Exception:
            log("warning", "slack_preview_hint_failed", task_id=task_id, binding_id=binding_id)


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
    request_id: str | None = None,
    permalink: str | None = None,
) -> str | None:
    repo = runtime.slack_a2a
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
                slack.append_stream_chunks(channel_id, stream_ts, [markdown_text_chunk(text)])
                message_ts = stream_ts
            else:
                blocks = source_link_blocks(permalink)
                if blocks:
                    # With blocks, top-level text is only the notification/accessibility fallback.
                    blocks = response_text_blocks(text) + blocks
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
    pending_ids = (
        {request.request_id for request in requests_from_task(task)}
        if task.status.state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED
        else set()
    )
    for item in repo.human_input_messages(binding_id, task_id):
        if item["item_id"] not in pending_ids:
            slack.update_message(
                channel_id, item["slack_message_ts"], "This request is no longer pending.", []
            )
            repo.close_human_input(binding_id, task_id, item["item_id"])
    status = task_slack_status(task)
    if stream_ts and is_settled(task):
        slack.stop_stream(
            channel_id,
            stream_ts,
            chunks=[
                settled_task_chunk(
                    request_id or task_id,
                    task_state(task),
                    approval=any(isinstance(r, ToolApprovalRequest) for r in requests_from_task(task)),
                )
            ],
            status=status,
        )
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
            blocks = tool_approval_blocks(
                task_id, request.request_id, request.tool.name, request.tool.arguments, request.message
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
