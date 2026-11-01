from __future__ import annotations

import asyncio
from typing import Any

from a2a.types import a2a_pb2 as a2a

from managed_agents_app.agent_control_plane.composition import application_control_plane
from managed_agents_app.agents.registry import from_config
from managed_agents_app.config import AppConfig, is_allowed_by_development_slack_filter, load_config
from managed_agents_app.db.slack_a2a_repository import Claim
from managed_agents_app.db.thread_repository import BindingConflict
from managed_agents_app.domain import (
    SLACK_CONTROL_REPLY_REQUESTED,
    SLACK_MESSAGE_RECEIVED,
    SLACK_TASK_PROJECTION_REQUESTED,
    SLACK_THREAD_STOP_REQUESTED,
    SLACK_TOOL_CONFIRMATION_REQUESTED,
    SLACK_WORK_STARTED,
    SlackControlReplyRequested,
    SlackMessageReceived,
    SlackTaskProjectionRequested,
    SlackThreadStopRequested,
    SlackToolConfirmationRequested,
    SlackWorkStarted,
)
from managed_agents_app.logging import log
from managed_agents_app.ports.slack import SlackGateway
from managed_agents_app.protocols.human_input_wire import requests_from_task
from managed_agents_app.runtime import Runtime, get_runtime


class RetryableDeliveryError(RuntimeError):
    pass


def handle_domain_event(runtime: Runtime, event: dict[str, Any]) -> None:
    asyncio.run(handle_domain_event_async(runtime, event))


async def handle_domain_event_async(runtime: Runtime, event: dict[str, Any]) -> None:
    kind = event.get("detail-type")
    detail = event.get("detail") or {}
    if kind == SLACK_MESSAGE_RECEIVED:
        await _message(runtime, SlackMessageReceived.model_validate(detail))
    elif kind == SLACK_THREAD_STOP_REQUESTED:
        await _stop(runtime, SlackThreadStopRequested.model_validate(detail))
    elif kind == SLACK_TOOL_CONFIRMATION_REQUESTED:
        await _tool_confirmation(runtime, SlackToolConfirmationRequested.model_validate(detail))


def _authorize(runtime: Runtime, team_id: str, user_id: str) -> str | None:
    if not is_allowed_by_development_slack_filter(runtime.config, team_id, user_id):
        log("warning", "slack_development_allowlist_rejected", team_id=team_id, user_id=user_id)
        return None
    return runtime.db.resolve_external_identity("slack", team_id, user_id)


def _claim(runtime: Runtime, surface: str, event_id: str) -> Claim | None:
    claim = runtime.slack_a2a.claim_ingress(surface, event_id)
    if claim.status == "busy":
        raise RetryableDeliveryError(f"Ingress lease is active for {surface}:{event_id}")
    return claim if claim.status == "acquired" else None


def _binding(runtime: Runtime, team_id: str, channel_id: str, thread_ts: str) -> dict[str, Any] | None:
    return runtime.threads.get_surface_binding("slack", team_id, f"{channel_id}:{thread_ts}")


def _owned_thread(runtime: Runtime, principal_id: str | None, binding: dict[str, Any] | None) -> str | None:
    if not principal_id or not binding:
        return None
    thread_id = str(binding["thread_id"])
    return thread_id if runtime.threads.owns_thread(principal_id, thread_id) else None


def _reply(runtime: Runtime, team_id: str, channel_id: str, thread_ts: str, text: str) -> None:
    runtime.events.publish(
        "app.slack",
        SLACK_CONTROL_REPLY_REQUESTED,
        SlackControlReplyRequested(team_id=team_id, channel_id=channel_id, thread_ts=thread_ts, text=text),
    )


def _work_started(runtime: Runtime, detail: SlackMessageReceived) -> None:
    request_id = detail.slack_event_id
    runtime.events.publish(
        "app.slack",
        SLACK_WORK_STARTED,
        SlackWorkStarted(
            request_id=request_id,
            team_id=detail.team_id,
            channel_id=detail.channel_id,
            thread_ts=detail.thread_ts,
            message_ts=detail.message_ts,
            user_id=detail.user_id,
        ),
    )


def _projection(
    runtime: Runtime,
    detail: SlackMessageReceived,
    binding_id: str,
    thread_id: str,
    task_id: str,
) -> None:
    request_id = detail.slack_event_id
    runtime.slack_a2a.ensure_projection(binding_id, task_id, request_id, detail.message_ts, detail.user_id)
    runtime.events.publish(
        "app.slack",
        SLACK_TASK_PROJECTION_REQUESTED,
        SlackTaskProjectionRequested(
            request_id=request_id,
            thread_id=thread_id,
            task_id=task_id,
            binding_id=binding_id,
            team_id=detail.team_id,
            channel_id=detail.channel_id,
            thread_ts=detail.thread_ts,
            user_id=detail.user_id,
            source_message_ts=detail.message_ts,
        ),
    )


def _application_agent_id(config: AppConfig) -> str:
    registration = from_config(config).default()
    if registration is None:
        raise RuntimeError("CMA application agent is not registered")
    return registration.agent_id


async def _message(runtime: Runtime, detail: SlackMessageReceived) -> None:
    config = runtime.config
    binding = _binding(runtime, detail.team_id, detail.channel_id, detail.thread_ts)
    if detail.event_type == "message" and detail.channel_type != "im" and not binding:
        return
    claim = _claim(runtime, "slack", detail.slack_event_id)
    if claim is None:
        return
    assert claim.token
    try:
        principal = _authorize(runtime, detail.team_id, detail.user_id)
        if not principal:
            _reply(
                runtime,
                detail.team_id,
                detail.channel_id,
                detail.thread_ts,
                "This Slack identity is not linked to an application principal.",
            )
            runtime.slack_a2a.complete_ingress("slack", detail.slack_event_id, claim.token, None, None)
            return
        if detail.command == "link":
            thread_id = _link_existing(
                runtime, principal, detail.team_id, detail.channel_id, detail.thread_ts, detail.link_thread_id
            )
            runtime.slack_a2a.complete_ingress("slack", detail.slack_event_id, claim.token, thread_id, None)
            return
        if not detail.text.strip():
            raise ValueError("Slack message is empty")
        if binding and not _owned_thread(runtime, principal, binding):
            _reply(
                runtime,
                detail.team_id,
                detail.channel_id,
                detail.thread_ts,
                "This request is not authorized for the linked thread.",
            )
            runtime.slack_a2a.complete_ingress("slack", detail.slack_event_id, claim.token, None, None)
            return
        thread, binding = runtime.threads.get_or_create_surface_thread(
            principal,
            _application_agent_id(config),
            "slack",
            detail.team_id,
            f"{detail.channel_id}:{detail.thread_ts}",
            detail.text[:60],
        )
        thread_id = str(thread["thread_id"])
        _work_started(runtime, detail)
        runtime.faults.hit("before_agent_send", detail.slack_event_id)
        async with application_control_plane(runtime) as service:
            task = await service.send_turn(
                thread_id,
                f"slack:event:{detail.team_id}:{detail.slack_event_id}",
                detail.text,
            )
        runtime.faults.hit("after_agent_send", detail.slack_event_id)
        _projection(runtime, detail, str(binding["binding_id"]), thread_id, task.id)
        runtime.faults.hit("before_ingress_complete", detail.slack_event_id)
        runtime.slack_a2a.complete_ingress("slack", detail.slack_event_id, claim.token, thread_id, task.id)
    except Exception as error:
        runtime.slack_a2a.fail_ingress("slack", detail.slack_event_id, claim.token, str(error))
        raise


async def _stop(runtime: Runtime, detail: SlackThreadStopRequested) -> None:
    claim = _claim(runtime, "slack-stop", detail.event_id)
    if claim is None:
        return
    assert claim.token
    try:
        principal = _authorize(runtime, detail.team_id, detail.user_id)
        thread_id = _owned_thread(
            runtime, principal, _binding(runtime, detail.team_id, detail.channel_id, detail.thread_ts)
        )
        task_id = None
        if thread_id:
            async with application_control_plane(runtime) as service:
                task = await service.cancel_active_task(thread_id)
                task_id = task.id if task else None
            _slack(runtime).set_agent_status(detail.channel_id, detail.thread_ts, "active")
        runtime.slack_a2a.complete_ingress("slack-stop", detail.event_id, claim.token, thread_id, task_id)
    except Exception as error:
        runtime.slack_a2a.fail_ingress("slack-stop", detail.event_id, claim.token, str(error))
        raise


async def _tool_confirmation(runtime: Runtime, detail: SlackToolConfirmationRequested) -> None:
    claim = _claim(runtime, "slack-interaction", detail.interaction_id)
    if claim is None:
        return
    assert claim.token
    try:
        principal = _authorize(runtime, detail.team_id, detail.user_id)
        thread_id = _owned_thread(
            runtime, principal, _binding(runtime, detail.team_id, detail.channel_id, detail.thread_ts)
        )
        if not thread_id:
            runtime.slack_a2a.complete_ingress(
                "slack-interaction", detail.interaction_id, claim.token, None, None
            )
            return
        resolved = False
        async with application_control_plane(runtime) as service:
            task = await service.get_task(thread_id, detail.task_id, human_input=True)
            pending = {request.request_id for request in requests_from_task(task)}
            if (
                task.status.state != a2a.TaskState.TASK_STATE_INPUT_REQUIRED
                or detail.input_request_id not in pending
            ):
                resolved = True
            else:
                response: dict[str, Any] = {
                    "kind": "tool_approval",
                    "requestId": detail.input_request_id,
                    "decision": "allow" if detail.approved else "deny",
                }
                if detail.reason:
                    response["reason"] = detail.reason
                try:
                    await service.continue_human_input(
                        thread_id,
                        detail.task_id,
                        f"slack:interaction:{detail.team_id}:{detail.interaction_id}",
                        response,
                    )
                except Exception:
                    current = await service.get_task(thread_id, detail.task_id, human_input=True)
                    if (
                        current.status.state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED
                        and detail.input_request_id
                        in {item.request_id for item in requests_from_task(current)}
                    ):
                        raise
                    resolved = True
        if detail.response_message_ts:
            if resolved:
                text = "This approval was already resolved."
            else:
                decision = (
                    "Approval submitted to OpenAPPA" if detail.approved else "Denial submitted to OpenAPPA"
                )
                suffix = f"\nReason: {detail.reason}" if detail.reason else ""
                text = f"{decision} by <@{detail.user_id}>{suffix}"
            _slack(runtime).update_message(detail.channel_id, detail.response_message_ts, text, [])
        if not resolved:
            _slack(runtime).set_agent_status(detail.channel_id, detail.thread_ts, "processing")
        runtime.slack_a2a.complete_ingress(
            "slack-interaction", detail.interaction_id, claim.token, thread_id, detail.task_id
        )
    except Exception as error:
        runtime.slack_a2a.fail_ingress("slack-interaction", detail.interaction_id, claim.token, str(error))
        raise


def _link_existing(
    runtime: Runtime,
    principal: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    thread_id: str | None,
) -> str | None:
    target = runtime.threads.get_thread(thread_id) if thread_id else None
    if not target or not runtime.threads.owns_thread(principal, str(target["thread_id"])):
        _reply(
            runtime,
            team_id,
            channel_id,
            thread_ts,
            "That thread does not exist or is not owned by your identity.",
        )
        return None
    binding = _binding(runtime, team_id, channel_id, thread_ts)
    if binding and str(binding["thread_id"]) != thread_id:
        _reply(
            runtime, team_id, channel_id, thread_ts, "This Slack thread is already linked to another thread."
        )
        return None
    try:
        runtime.threads.bind_surface(str(target["thread_id"]), "slack", team_id, f"{channel_id}:{thread_ts}")
    except BindingConflict:
        _reply(
            runtime, team_id, channel_id, thread_ts, "This Slack thread is already linked to another thread."
        )
        return None
    _reply(runtime, team_id, channel_id, thread_ts, f"Linked this thread to Claude thread {thread_id}.")
    return thread_id


def _slack(runtime: Runtime) -> SlackGateway:
    if not runtime.config.slack_bot_token:
        raise RuntimeError("Slack bot token is not configured")
    return runtime.slack


def handler(event: dict[str, Any], _context: Any) -> None:
    config: AppConfig = load_config()
    if config.app_env == "e2e":
        raise RuntimeError("E2E runtime is only available through local_api")
    return handle_domain_event(get_runtime(config), event)
