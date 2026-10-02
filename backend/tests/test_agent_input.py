from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from a2a.types import a2a_pb2 as a2a

from managed_agents_app.db.slack_a2a_repository import Claim
from managed_agents_app.domain import SLACK_TASK_PROJECTION_REQUESTED, SLACK_WORK_STARTED
from managed_agents_app.handlers import agent_input


def message_event(event_id: str = "Ev1") -> dict:
    return {
        "detail-type": "SlackMessageReceived",
        "detail": {
            "version": 1,
            "slackEventId": event_id,
            "teamId": "T1",
            "channelId": "C1",
            "threadTs": "1.0",
            "messageTs": "1.1",
            "userId": "U1",
            "text": "hello",
            "eventType": "app_mention",
        },
    }


def make_runtime(config, monkeypatch):
    config = config.model_copy(
        update={
            "a2a_event_sink_url": "http://sink.test/internal/a2a/events",
            "cma_a2a_endpoint": "http://controller.test",
        }
    )
    db, threads, repo, events, slack = (MagicMock() for _ in range(5))
    db.resolve_external_identity.return_value = "principal-1"
    threads.get_surface_binding.return_value = None
    threads.get_or_create_surface_thread.return_value = (
        {"thread_id": "thread-1", "agent_id": "cma"},
        {"binding_id": "binding-1", "thread_id": "thread-1"},
    )
    threads.owns_thread.return_value = True
    repo.claim_ingress.return_value = Claim("acquired", "claim-1")
    service = MagicMock()

    async def send_turn(*_args):
        return a2a.Task(id="task-1")

    service.send_turn.side_effect = send_turn

    @asynccontextmanager
    async def control(_runtime):
        yield service

    monkeypatch.setattr(agent_input, "application_control_plane", control)
    return SimpleNamespace(
        config=config, db=db, threads=threads, slack_a2a=repo, events=events, slack=slack, faults=MagicMock()
    ), service


def test_message_sends_with_stable_id_and_persists_projection(config, monkeypatch):
    runtime, service = make_runtime(config, monkeypatch)
    agent_input.handle_domain_event(runtime, message_event())
    assert service.send_turn.call_args.args == ("thread-1", "slack:event:T1:Ev1", "hello")
    assert runtime.threads.get_or_create_surface_thread.call_args.args[:2] == ("principal-1", "cma")
    assert [call.args[1] for call in runtime.events.publish.call_args_list] == [
        SLACK_WORK_STARTED,
        SLACK_TASK_PROJECTION_REQUESTED,
    ]
    runtime.slack_a2a.ensure_projection.assert_called_once_with("binding-1", "task-1", "Ev1", "1.1", "U1")
    runtime.slack_a2a.complete_ingress.assert_called_once_with(
        "slack", "Ev1", "claim-1", "thread-1", "task-1"
    )


def test_busy_ingress_retries(config, monkeypatch):
    runtime, _ = make_runtime(config, monkeypatch)
    runtime.slack_a2a.claim_ingress.return_value = Claim("busy")
    with pytest.raises(agent_input.RetryableDeliveryError):
        agent_input.handle_domain_event(runtime, message_event())


def test_unowned_reply_does_not_send(config, monkeypatch):
    runtime, service = make_runtime(config, monkeypatch)
    runtime.config = runtime.config.model_copy(update={"slack_bound_thread_replies": True})
    runtime.threads.get_surface_binding.return_value = {"binding_id": "binding-1", "thread_id": "thread-1"}
    runtime.threads.owns_thread.return_value = False
    event = message_event()
    event["detail"].update(eventType="message", channelType="channel")
    agent_input.handle_domain_event(runtime, event)
    service.send_turn.assert_not_called()
    assert runtime.events.publish.call_args.args[1] == "SlackControlReplyRequested"


def test_shortcut_context_is_sanitized(config, monkeypatch):
    runtime, service = make_runtime(config, monkeypatch)
    runtime.config = runtime.config.model_copy(update={"slack_shortcuts_enabled": True})
    runtime.slack.conversation_replies.return_value = [
        {"ts": "1.2", "user": "U2", "text": "selected", "secret": "omit-me"}
    ]
    event = {
        "detail-type": "SlackShortcutReceived",
        "detail": {
            "interactionId": "Ix1",
            "teamId": "T1",
            "channelId": "C1",
            "messageTs": "1.2",
            "threadTs": "1.0",
            "userId": "U1",
            "callbackId": "summarize_thread",
        },
    }
    agent_input.handle_domain_event(runtime, event)
    sent = service.send_turn.call_args.args
    assert sent[:2] == ("thread-1", "slack:interaction:T1:Ix1")
    assert "untrusted reference data" in sent[2]
    assert "omit-me" not in sent[2]
