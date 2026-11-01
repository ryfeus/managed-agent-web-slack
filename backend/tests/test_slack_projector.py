from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.types import a2a_pb2 as a2a

from managed_agents_app.db.slack_a2a_repository import Claim
from managed_agents_app.handlers import slack_projector
from managed_agents_app.slack.blocks import markdown_text_chunk, settled_task_chunk, working_task_chunk


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
            "slack_bot_token": "xoxb-test",
        }
    )
    repo, threads, slack = MagicMock(), MagicMock(), MagicMock()
    projection = {
        "slack_message_ts": None,
        "status": "pending",
        "request_event_id": "Ev1",
        "source_message_ts": "1.1",
        "initiator_user_id": "U1",
        "last_task_state": None,
    }
    repo.ensure_projection.return_value = projection
    repo.get_projection.return_value = projection
    repo.claim_task_stream.side_effect = lambda *_args: Claim("acquired", "stream-claim", dict(projection))

    def finish_stream(_binding, _task, _token, *, fallback, state, error=None):
        settled = state in {"INPUT_REQUIRED", "COMPLETED", "FAILED", "CANCELED", "REJECTED"}
        projection.update(
            last_task_state=state, status="fallback" if fallback else "completed" if settled else "pending"
        )
        if settled and not fallback:
            projection["slack_message_ts"] = None

    repo.finish_stream.side_effect = finish_stream
    repo.update_stream_message.side_effect = lambda _b, _t, _c, ts: projection.update(slack_message_ts=ts)
    repo.claim_item.return_value = Claim("acquired", "item-claim")
    repo.has_human_input_receipt.return_value = False
    slack.start_stream.return_value = "2.1"
    service = MagicMock()

    async def get_task(*_args, **_kwargs):
        return current or task()

    service.get_task = AsyncMock(side_effect=get_task)
    service.subscribe_task.side_effect = AssertionError("Slack P0 must not subscribe")

    @asynccontextmanager
    async def control(_runtime):
        yield service

    monkeypatch.setattr(slack_projector, "application_control_plane", control)
    return SimpleNamespace(
        config=config, slack_a2a=repo, threads=threads, slack=slack, slack_preview_trigger=MagicMock()
    ), service


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


def update_event():
    return {
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


def bind(runtime):
    runtime.threads.list_surface_bindings.return_value = [
        {"surface": "slack", "binding_id": "binding-1", "external_thread_id": "C1:1.0", "tenant_id": "T1"}
    ]


def test_initial_card_returns_working_then_push_finishes_without_subscription(config, monkeypatch):
    # nonterminal setup releases the claim as pending; push finishes the same card.
    current = task(a2a.TaskState.TASK_STATE_WORKING)
    current.ClearField("history")
    runtime, service = make_runtime(config, monkeypatch, current)
    bind(runtime)
    slack_projector.handle_domain_event(runtime, projection_event())
    service.get_task.assert_awaited_once_with("thread-1", "task-1", human_input=True)
    service.subscribe_task.assert_not_called()
    assert runtime.slack.start_stream.call_args.kwargs["chunks"] == [working_task_chunk("Ev1")]
    runtime.slack.stop_stream.assert_not_called()
    runtime.slack_a2a.finish_stream.assert_called_once_with(
        "binding-1", "task-1", "stream-claim", fallback=False, state="WORKING"
    )
    current.CopyFrom(task())
    slack_projector.handle_domain_event(runtime, update_event())
    runtime.slack.start_stream.assert_called_once()
    runtime.slack.append_stream_chunks.assert_called_once_with("C1", "2.1", [markdown_text_chunk("answer")])
    assert runtime.slack.stop_stream.call_args.kwargs["chunks"] == [settled_task_chunk("Ev1", "COMPLETED")]
    assert runtime.slack_a2a.get_projection.return_value["status"] == "completed"


@pytest.mark.parametrize(
    "state",
    [
        a2a.TaskState.TASK_STATE_COMPLETED,
        a2a.TaskState.TASK_STATE_CANCELED,
        a2a.TaskState.TASK_STATE_FAILED,
        a2a.TaskState.TASK_STATE_INPUT_REQUIRED,
    ],
)
def test_late_setup_after_settled_push_does_not_open_stream(config, monkeypatch, state):
    runtime, service = make_runtime(config, monkeypatch, task(state))
    bind(runtime)
    projection = runtime.slack_a2a.get_projection.return_value
    projection["initiator_user_id"] = None
    slack_projector.handle_domain_event(runtime, update_event())
    runtime.slack.post_reply.assert_called_once()
    projection["initiator_user_id"] = "U1"
    runtime.slack_a2a.claim_item.return_value = Claim("completed")
    slack_projector.handle_domain_event(runtime, projection_event())
    slack_projector.handle_domain_event(runtime, update_event())
    runtime.slack.start_stream.assert_not_called()
    runtime.slack.post_reply.assert_called_once()
    service.subscribe_task.assert_not_called()


def test_push_retries_short_claim_then_reads_current_task(config, monkeypatch):
    runtime, service = make_runtime(config, monkeypatch)
    bind(runtime)
    runtime.slack_a2a.claim_task_stream.side_effect = [Claim("busy")]
    with pytest.raises(RuntimeError, match="projection is busy"):
        slack_projector.handle_domain_event(runtime, update_event())
    service.get_task.assert_not_called()
    runtime.slack.post_reply.assert_not_called()


def test_resumed_working_push_cannot_reopen_a_card_after_human_input(config, monkeypatch):
    current = task(a2a.TaskState.TASK_STATE_WORKING)
    runtime, _ = make_runtime(config, monkeypatch, current)
    runtime.slack_a2a.get_projection.return_value["last_task_state"] = "WORKING"
    runtime.slack_a2a.has_human_input_receipt.return_value = True
    slack_projector.handle_domain_event(runtime, projection_event())
    current.CopyFrom(task())
    slack_projector.handle_domain_event(runtime, projection_event())
    runtime.slack.start_stream.assert_not_called()
    runtime.slack_preview_trigger.start.assert_not_called()


def test_push_projects_to_each_slack_binding(config, monkeypatch):
    runtime, service = make_runtime(config, monkeypatch)
    runtime.slack_a2a.get_projection.return_value["initiator_user_id"] = None
    runtime.threads.list_surface_bindings.return_value = [
        {"surface": "slack", "binding_id": "b1", "external_thread_id": "C1:1.0", "tenant_id": "T1"},
        {"surface": "slack", "binding_id": "b2", "external_thread_id": "C2:2.0", "tenant_id": "T1"},
        {"surface": "web", "binding_id": "b3", "external_thread_id": "web"},
    ]
    slack_projector.handle_domain_event(runtime, update_event())
    assert runtime.slack.post_reply.call_count == 2
    assert runtime.slack_a2a.ensure_projection.call_count == 2
    assert service.get_task.await_count == 2


def test_failed_append_releases_claim_and_push_retries_same_card(config, monkeypatch):
    runtime, service = make_runtime(config, monkeypatch)
    bind(runtime)
    runtime.slack.append_stream_chunks.side_effect = [RuntimeError("rate limited"), None]
    with pytest.raises(RuntimeError, match="rate limited"):
        slack_projector.handle_domain_event(runtime, projection_event())
    runtime.slack_a2a.finish_stream.assert_called_once_with(
        "binding-1", "task-1", "stream-claim", fallback=True, state="COMPLETED", error="rate limited"
    )
    slack_projector.handle_domain_event(runtime, update_event())
    runtime.slack.start_stream.assert_called_once()
    runtime.slack.stop_stream.assert_called_once()
    service.send.assert_not_called()
    service.subscribe_task.assert_not_called()


@pytest.mark.parametrize("source_link", [True, False])
@pytest.mark.parametrize("answer", ["answer", ("long answer " * 600).strip()], ids=["short", "long"])
def test_nonstreamed_answer_is_visible_with_optional_blocks(config, monkeypatch, source_link, answer):
    runtime, _ = make_runtime(config, monkeypatch)
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
        assert any(block["type"] == "context" for block in blocks) == source_link
    else:
        assert not source_link


@pytest.mark.parametrize("enabled", [True, False])
def test_preview_hint_after_claim_release_once_and_failure_does_not_retry_p0(config, monkeypatch, enabled):
    current = task(a2a.TaskState.TASK_STATE_WORKING)
    current.ClearField("history")
    runtime, _ = make_runtime(config, monkeypatch, current)
    runtime.config = runtime.config.model_copy(
        update={"preview_enabled": True, "slack_preview_enabled": enabled}
    )

    def fail_hint(hint):
        runtime.slack_a2a.finish_stream.assert_called_once()
        assert runtime.slack_a2a.get_projection.return_value["status"] == "pending"
        assert (hint.binding_id, hint.task_id, hint.message_ts) == ("binding-1", "task-1", "2.1")
        raise RuntimeError("sensitive response must not be logged")

    runtime.slack_preview_trigger.start.side_effect = fail_hint
    slack_projector.handle_domain_event(runtime, projection_event())
    slack_projector.handle_domain_event(runtime, projection_event())
    assert runtime.slack_preview_trigger.start.call_count == int(enabled)
    assert all(not call.kwargs["fallback"] for call in runtime.slack_a2a.finish_stream.call_args_list)
    runtime.slack.start_stream.assert_called_once()


@pytest.mark.parametrize("stream_available", [True, False])
def test_settled_or_failed_stream_start_never_schedules_preview(config, monkeypatch, stream_available):
    runtime, _ = make_runtime(
        config, monkeypatch, task() if stream_available else task(a2a.TaskState.TASK_STATE_WORKING)
    )
    runtime.config = runtime.config.model_copy(
        update={"preview_enabled": True, "slack_preview_enabled": True}
    )
    if not stream_available:
        runtime.slack.start_stream.side_effect = RuntimeError("missing_scope")
    slack_projector.handle_domain_event(runtime, projection_event())
    runtime.slack_preview_trigger.start.assert_not_called()


@pytest.mark.parametrize(
    ("state", "title"),
    [
        ("COMPLETED", "Request complete"),
        ("INPUT_REQUIRED", "Waiting for your answer"),
        ("CANCELED", "Request stopped"),
        ("FAILED", "Request failed"),
        ("REJECTED", "Request rejected"),
    ],
)
def test_every_settled_stream_replaces_draft_title_and_preserves_canonical_text(
    config, monkeypatch, state, title
):
    from managed_agents_app.testing.recording_slack import RecordingSlack

    current = task(a2a.TaskState.Value("TASK_STATE_" + state))
    runtime, _ = make_runtime(config, monkeypatch, current)
    slack = RecordingSlack()
    ts = slack.start_stream(
        "C1", "1.0", "T1", "U1", chunks=[working_task_chunk("Ev1", "https://example.test/source")]
    )
    slack.append_stream_chunks(
        "C1",
        ts,
        [{"type": "task_update", "id": "request:Ev1", "title": "Draft: …wrong", "status": "in_progress"}],
    )
    assert slack.streams[ts]["text"] == ""
    slack_projector._project_task(
        runtime, slack, "binding-1", "task-1", "C1", "1.0", current, stream_ts=ts, request_id="Ev1"
    )
    card = slack.streams[ts]
    assert card["text"] == "answer" and card["state"] == "completed"
    assert card["tasks"]["request:Ev1"]["title"] == title
    assert card["tasks"]["request:Ev1"]["sources"][0]["url"] == "https://example.test/source"
    assert card["status"] == ("suspended" if state == "INPUT_REQUIRED" else "active")
