from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from a2a.types import a2a_pb2 as a2a

from managed_agents_app.db.slack_a2a_repository import Claim
from managed_agents_app.handlers import slack_projector
from managed_agents_app.slack.blocks import completed_task_chunk, markdown_text_chunk, working_task_chunk


def task(state: int = a2a.TaskState.TASK_STATE_COMPLETED) -> a2a.Task:
    return a2a.Task(
        id="task-1",
        context_id="context-1",
        status=a2a.TaskStatus(state=state),
        history=[
            a2a.Message(message_id="message-1", role=a2a.Role.ROLE_AGENT, parts=[a2a.Part(text="answer")])
        ],
    )


def make_runtime(config, monkeypatch, current=None):
    config = config.model_copy(
        update={
            "slack_streaming_enabled": True,
            "slack_task_cards_enabled": True,
            "slack_agent_view_enabled": True,
            "slack_bot_token": "xoxb-test",
        }
    )
    repo, threads, slack = MagicMock(), MagicMock(), MagicMock()
    repo.ensure_projection.return_value = {
        "slack_message_ts": None,
        "status": "pending",
        "request_event_id": "Ev1",
        "source_message_ts": "1.1",
        "initiator_user_id": "U1",
    }
    repo.claim_task_stream.return_value = Claim("acquired", "stream-claim")
    repo.claim_item.return_value = Claim("acquired", "item-claim")
    slack.start_stream.return_value = "2.1"
    service = MagicMock()

    async def get_task(*_args, **_kwargs):
        return current or task()

    async def subscribe(*_args, **_kwargs):
        yield a2a.StreamResponse(task=current or task())

    service.get_task.side_effect = get_task
    service.subscribe_task.side_effect = subscribe

    @asynccontextmanager
    async def control(_runtime):
        yield service

    monkeypatch.setattr(slack_projector, "application_control_plane", control)
    return SimpleNamespace(config=config, slack_a2a=repo, threads=threads, slack=slack), service


def projection_event():
    return {
        "detail-type": "SlackTaskProjectionRequested",
        "detail": {
            "requestId": "Ev1",
            "threadId": "thread-1",
            "taskId": "task-1",
            "bindingId": "binding-1",
            "teamId": "T1",
            "channelId": "C1",
            "threadTs": "1.0",
            "userId": "U1",
            "sourceMessageTs": "1.1",
        },
    }


def test_live_subscription_projects_complete_message_and_card(config, monkeypatch):
    runtime, service = make_runtime(config, monkeypatch)
    slack_projector.handle_domain_event(runtime, projection_event())
    service.subscribe_task.assert_called_once_with("thread-1", "task-1", human_input=True)
    assert runtime.slack.start_stream.call_args.kwargs["chunks"] == [working_task_chunk("Ev1")]
    runtime.slack.append_stream_chunks.assert_called_once_with("C1", "2.1", [markdown_text_chunk("answer")])
    assert runtime.slack.stop_stream.call_args.kwargs["chunks"] == [completed_task_chunk("Ev1")]
    runtime.slack_a2a.finish_item.assert_called_once()
    runtime.slack_a2a.finish_stream.assert_called_once_with(
        "binding-1", "task-1", "stream-claim", fallback=False, state="COMPLETED"
    )


def test_push_retries_during_live_lease(config, monkeypatch):
    runtime, _ = make_runtime(config, monkeypatch)
    runtime.threads.list_surface_bindings.return_value = [
        {"surface": "slack", "binding_id": "binding-1", "external_thread_id": "C1:1.0"}
    ]
    runtime.slack_a2a.active_stream.return_value = True
    event = {
        "detail-type": "A2ATaskUpdated",
        "detail": {
            "deliveryId": "delivery-1",
            "agentId": "cma",
            "threadId": "thread-1",
            "taskId": "task-1",
            "contextId": "context-1",
            "eventKind": "task",
            "taskState": "COMPLETED",
        },
    }
    import pytest

    with pytest.raises(RuntimeError, match="live projection"):
        slack_projector.handle_domain_event(runtime, event)
    runtime.slack.post_reply.assert_not_called()


def test_push_projects_to_each_slack_binding(config, monkeypatch):
    runtime, _ = make_runtime(config, monkeypatch)
    runtime.threads.list_surface_bindings.return_value = [
        {"surface": "slack", "binding_id": "b1", "external_thread_id": "C1:1.0"},
        {"surface": "slack", "binding_id": "b2", "external_thread_id": "C2:2.0"},
        {"surface": "web", "binding_id": "b3", "external_thread_id": "web"},
    ]
    runtime.slack_a2a.active_stream.return_value = False
    event = {
        "detail-type": "A2ATaskUpdated",
        "detail": {
            "deliveryId": "delivery-1",
            "agentId": "cma",
            "threadId": "thread-1",
            "taskId": "task-1",
            "contextId": "context-1",
            "eventKind": "task",
            "taskState": "COMPLETED",
        },
    }
    slack_projector.handle_domain_event(runtime, event)
    assert runtime.slack.post_reply.call_count == 2
    assert runtime.slack_a2a.ensure_projection.call_count == 2


@pytest.mark.parametrize("feedback,source_link", [(True, False), (False, True), (True, True), (False, False)])
@pytest.mark.parametrize("answer", ["answer", ("long answer " * 600).strip()], ids=["short", "long"])
def test_nonstreamed_answer_is_visible_with_optional_blocks(
    config, monkeypatch, feedback, source_link, answer
):
    runtime, _ = make_runtime(config, monkeypatch)
    runtime.config = runtime.config.model_copy(update={"slack_feedback_enabled": feedback})
    current = task()
    current.history[0].parts[0].text = answer
    slack_projector._project_task(
        runtime,
        runtime.slack,
        "binding-1",
        "task-1",
        "C1",
        "1.0",
        current,
        permalink="https://slack.test/source" if source_link else None,
    )
    _, _, fallback, blocks = runtime.slack.post_reply.call_args.args
    assert fallback == answer
    if blocks:
        sections = [block for block in blocks if block["type"] == "section"]
        assert "".join(block["text"]["text"] for block in sections) == answer
        assert all(0 < len(block["text"]["text"]) <= 3000 for block in sections)
        assert blocks[0]["type"] == "section"
        assert any(block["type"] == "context_actions" for block in blocks) == feedback
        assert any(block["type"] == "context" for block in blocks) == source_link
    else:
        assert not feedback and not source_link
