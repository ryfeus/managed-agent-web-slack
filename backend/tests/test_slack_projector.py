from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from managed_agents_app.db import ProjectionClaim
from managed_agents_app.domain import ManagedAgentSessionChanged, SlackProjectionRequested
from managed_agents_app.handlers import slack_projector
from managed_agents_app.slack.blocks import completed_task_chunk, markdown_text_chunk, working_task_chunk


def projection_event() -> dict:
    return {
        "detail-type": "SlackProjectionRequested",
        "detail": {
            "version": 1,
            "requestId": "Ev1",
            "sessionId": "sesn_1",
            "bindingId": "00000000-0000-4000-8000-000000000002",
            "teamId": "T1",
            "channelId": "C1",
            "threadTs": "1",
            "userId": "U1",
        },
    }


def test_parity_gate_does_not_call_agent_status_or_stream(runtime, monkeypatch, config) -> None:
    slack = MagicMock()
    monkeypatch.setattr(slack_projector, "load_config", lambda: config)
    monkeypatch.setattr(slack_projector, "_slack", lambda _runtime, _config: slack)
    monkeypatch.setattr(slack_projector, "_live_stream", MagicMock())
    slack_projector.handler(projection_event(), None)
    slack.set_agent_status.assert_not_called()
    slack_projector._live_stream.assert_not_called()


def test_agent_view_and_streaming_gates_are_independent(runtime, monkeypatch, config) -> None:
    enabled = config.model_copy(update={"slack_agent_view_enabled": True, "slack_streaming_enabled": True})
    slack = MagicMock()
    live = MagicMock()
    monkeypatch.setattr(slack_projector, "load_config", lambda: enabled)
    monkeypatch.setattr(slack_projector, "_slack", lambda _runtime, _config: slack)
    monkeypatch.setattr(slack_projector, "_live_stream", live)
    slack_projector.handler(projection_event(), None)
    slack.set_agent_status.assert_called_once()
    live.assert_called_once()


def test_work_started_sets_processing_and_idempotent_reaction(runtime, monkeypatch, config) -> None:
    enabled = config.model_copy(
        update={"slack_agent_view_enabled": True, "slack_receipt_reaction_enabled": True}
    )
    slack = MagicMock()
    monkeypatch.setattr(slack_projector, "load_config", lambda: enabled)
    monkeypatch.setattr(slack_projector, "_slack", lambda _runtime, _config: slack)
    slack_projector.handler(
        {
            "detail-type": "SlackWorkStarted",
            "detail": {
                "version": 1,
                "requestId": "Ev1",
                "teamId": "T1",
                "channelId": "C1",
                "threadTs": "1.0",
                "messageTs": "1.1",
                "userId": "U1",
            },
        },
        None,
    )
    slack.set_agent_status.assert_called_once_with(
        "C1", "1.0", "processing", title="Claude session", initiator_user_id="U1"
    )
    slack.add_reaction.assert_called_once_with("C1", "1.1", "eyes")


def test_work_started_respects_reaction_gate(runtime, monkeypatch, config) -> None:
    slack = MagicMock()
    monkeypatch.setattr(slack_projector, "load_config", lambda: config)
    monkeypatch.setattr(slack_projector, "_slack", lambda _runtime, _config: slack)
    slack_projector.handler(
        {
            "detail-type": "SlackWorkStarted",
            "detail": {
                "version": 1,
                "requestId": "Ev1",
                "teamId": "T1",
                "channelId": "C1",
                "threadTs": "1.0",
                "messageTs": "1.1",
                "userId": "U1",
            },
        },
        None,
    )
    slack.add_reaction.assert_not_called()


def test_task_cards_warn_and_stay_disabled_without_streaming(runtime, monkeypatch, config) -> None:
    invalid = config.model_copy(update={"slack_task_cards_enabled": True})
    slack = MagicMock()
    warning = MagicMock()
    monkeypatch.setattr(slack_projector, "load_config", lambda: invalid)
    monkeypatch.setattr(slack_projector, "_slack", lambda _runtime, _config: slack)
    monkeypatch.setattr(slack_projector, "log", warning)
    slack_projector.handler(projection_event(), None)
    warning.assert_called_once_with("warning", "slack_task_cards_disabled_without_streaming")
    slack.start_stream.assert_not_called()


def _projection() -> SlackProjectionRequested:
    return SlackProjectionRequested(
        request_id="Ev1",
        session_id="sesn_1",
        binding_id="00000000-0000-4000-8000-000000000002",
        team_id="T1",
        channel_id="C1",
        thread_ts="1.0",
        user_id="U1",
        source_message_ts="1.1",
    )


def _install_live(runtime, monkeypatch, managed_events, final_events):
    db = MagicMock()
    db.claim_stream.return_value = ProjectionClaim.ACQUIRED
    managed = MagicMock()
    managed.stream_events.return_value = iter(managed_events)
    managed.list_events.return_value = final_events
    monkeypatch.setattr(runtime, "db", db)
    monkeypatch.setattr(runtime, "agent", managed)
    return (db, managed)


def test_task_stream_starts_early_links_source_and_emits_final_without_deltas(
    runtime, monkeypatch, config
) -> None:
    enabled = config.model_copy(
        update={
            "slack_streaming_enabled": True,
            "slack_task_cards_enabled": True,
            "slack_source_links_enabled": True,
        }
    )
    timeline: list[str] = []
    db, managed = _install_live(
        runtime,
        monkeypatch,
        [
            {"id": "evt_1", "type": "agent.message", "content": [{"type": "text", "text": "done"}]},
            {"type": "session.status_idle"},
        ],
        [],
    )
    managed.stream_events.side_effect = lambda *_args, **_kwargs: (
        timeline.append("managed")
        or iter(
            [
                {"id": "evt_1", "type": "agent.message", "content": [{"type": "text", "text": "done"}]},
                {"type": "session.status_idle"},
            ]
        )
    )
    slack = MagicMock()
    slack.message_permalink.return_value = "https://example.test/original"
    slack.start_stream.side_effect = lambda *_args, **_kwargs: timeline.append("start") or "2.1"
    slack_projector._live_stream(runtime, enabled, slack, _projection())
    assert timeline[:2] == ["start", "managed"]
    slack.start_stream.assert_called_once_with(
        "C1",
        "1.0",
        "T1",
        "U1",
        chunks=[working_task_chunk("Ev1", "https://example.test/original")],
        task_display_mode="timeline",
    )
    slack.append_stream_chunks.assert_called_once_with("C1", "2.1", [markdown_text_chunk("done")])
    slack.stop_stream.assert_called_once_with(
        "C1", "2.1", chunks=[completed_task_chunk("Ev1")], blocks=None, status="active"
    )
    db.complete_stream.assert_called_once()


def test_task_stream_recovers_events_completed_before_subscription(runtime, monkeypatch, config) -> None:
    enabled = config.model_copy(update={"slack_streaming_enabled": True, "slack_task_cards_enabled": True})
    completed = [
        {"id": "evt_input", "type": "user.message", "content": []},
        {"id": "evt_final", "type": "agent.message", "content": [{"type": "text", "text": "fast response"}]},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    db, managed = _install_live(runtime, monkeypatch, [], completed)
    slack = MagicMock()
    slack.start_stream.return_value = "2.1"
    slack_projector._live_stream(runtime, enabled, slack, _projection())
    managed.stream_events.assert_not_called()
    slack.append_stream_chunks.assert_called_once_with("C1", "2.1", [markdown_text_chunk("fast response")])
    slack.stop_stream.assert_called_once_with(
        "C1", "2.1", chunks=[completed_task_chunk("Ev1")], blocks=None, status="active"
    )
    db.complete_stream.assert_called_once()


def test_task_stream_permalink_failure_is_nonfatal(runtime, monkeypatch, config) -> None:
    enabled = config.model_copy(
        update={
            "slack_streaming_enabled": True,
            "slack_task_cards_enabled": True,
            "slack_source_links_enabled": True,
        }
    )
    _install_live(runtime, monkeypatch, [{"type": "session.status_idle"}], [])
    slack = MagicMock()
    slack.message_permalink.side_effect = RuntimeError("not found")
    slack.start_stream.return_value = "2.1"
    slack_projector._live_stream(runtime, enabled, slack, _projection())
    assert slack.start_stream.call_args.kwargs["chunks"] == [working_task_chunk("Ev1")]
    slack.stop_stream.assert_called_once_with(
        "C1", "2.1", chunks=[completed_task_chunk("Ev1")], blocks=None, status="active"
    )


def test_text_stream_appends_source_link_context(runtime, monkeypatch, config) -> None:
    enabled = config.model_copy(update={"slack_streaming_enabled": True, "slack_source_links_enabled": True})
    _install_live(
        runtime,
        monkeypatch,
        [
            {"type": "event_delta", "delta": {"content": {"type": "text", "text": "done"}}},
            {"type": "session.status_idle"},
        ],
        [],
    )
    slack = MagicMock()
    slack.message_permalink.return_value = "https://example.test/original"
    slack.start_stream.return_value = "2.1"
    slack_projector._live_stream(runtime, enabled, slack, _projection())
    slack.start_stream.assert_called_once_with("C1", "1.0", "T1", "U1", "done")
    slack.stop_stream.assert_called_once_with(
        "C1",
        "2.1",
        chunks=None,
        blocks=[
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "<https://example.test/original|Original message>"}],
            }
        ],
        status="active",
    )


def test_pending_approval_leaves_task_open_and_suspends(runtime, monkeypatch, config) -> None:
    enabled = config.model_copy(
        update={
            "slack_streaming_enabled": True,
            "slack_task_cards_enabled": True,
            "slack_tool_approvals_enabled": True,
            "slack_agent_view_enabled": True,
        }
    )
    pending = [
        {"id": "tool_1", "type": "agent.tool_use", "name": "browser", "input": {}},
        {"type": "session.status_idle", "stop_reason": {"type": "requires_action", "event_ids": ["tool_1"]}},
    ]
    db, _managed = _install_live(runtime, monkeypatch, [{"type": "session.status_idle"}], pending)
    db.claim_projection.return_value = ProjectionClaim.ACQUIRED
    slack = MagicMock()
    slack.start_stream.return_value = "2.1"
    slack_projector._live_stream(runtime, enabled, slack, _projection())
    slack.stop_stream.assert_called_once_with("C1", "2.1", chunks=None, blocks=None, status="suspended")
    slack.post_reply.assert_called_once()
    slack.set_agent_status.assert_called_once_with("C1", "1.0", "suspended")


def test_webhook_finalizes_a_recoverable_slack_stream(runtime, monkeypatch, config) -> None:
    enabled = config.model_copy(update={"slack_agent_view_enabled": True, "slack_feedback_enabled": True})
    db = MagicMock()
    db.list_slack_bindings.return_value = [
        {"binding_id": "00000000-0000-4000-8000-000000000002", "external_thread_id": "C1:1"}
    ]
    db.claim_projection.return_value = ProjectionClaim.ACQUIRED
    db.has_active_stream.return_value = False
    db.find_recoverable_stream.return_value = ("Ev1", "2.1")
    managed = MagicMock()
    managed.list_events.return_value = [
        {"id": "evt_1", "type": "agent.message", "content": [{"type": "text", "text": "finished"}]}
    ]
    slack = MagicMock()
    monkeypatch.setattr(runtime, "db", db)
    monkeypatch.setattr(runtime, "agent", managed)
    slack_projector._project_completed(
        runtime,
        enabled,
        slack,
        ManagedAgentSessionChanged(
            webhookEventId="wh_1", webhookType="session.status_idle", sessionId="sesn_1"
        ),
    )
    slack.stop_stream.assert_called_once_with(
        "C1",
        "2.1",
        text="finished",
        chunks=None,
        blocks=slack_projector.feedback_blocks("evt_1"),
        status="active",
    )
    slack.update_message.assert_not_called()
    db.complete_recovered_stream.assert_called_once_with(
        "00000000-0000-4000-8000-000000000002", "Ev1", "evt_1", "2.1"
    )
    db.mark_projected.assert_not_called()


def test_webhook_recovers_task_stream_in_chunk_mode(runtime, monkeypatch, config) -> None:
    enabled = config.model_copy(update={"slack_streaming_enabled": True, "slack_task_cards_enabled": True})
    db = MagicMock()
    db.list_slack_bindings.return_value = [{"binding_id": "binding-1", "external_thread_id": "C1:1.0"}]
    db.claim_projection.return_value = ProjectionClaim.ACQUIRED
    db.has_active_stream.return_value = False
    db.find_recoverable_stream.return_value = ("Ev1", "2.1")
    managed = MagicMock()
    managed.list_events.return_value = [
        {"id": "evt_1", "type": "agent.message", "content": [{"type": "text", "text": "done"}]}
    ]
    slack = MagicMock()
    monkeypatch.setattr(runtime, "db", db)
    monkeypatch.setattr(runtime, "agent", managed)
    slack_projector._project_completed(
        runtime,
        enabled,
        slack,
        ManagedAgentSessionChanged(
            webhookEventId="wh_1", webhookType="session.status_idle", sessionId="sesn_1"
        ),
    )
    slack.stop_stream.assert_called_once_with(
        "C1",
        "2.1",
        text=None,
        chunks=[markdown_text_chunk("done"), completed_task_chunk("Ev1")],
        blocks=None,
        status="active",
    )
    db.complete_recovered_stream.assert_called_once_with("binding-1", "Ev1", "evt_1", "2.1")


def test_webhook_defers_while_live_stream_lease_is_active(runtime, monkeypatch, config) -> None:
    enabled = config.model_copy(update={"slack_streaming_enabled": True})
    db = MagicMock()
    db.list_slack_bindings.return_value = [{"binding_id": "binding-1", "external_thread_id": "C1:1.0"}]
    db.has_active_stream.return_value = True
    managed = MagicMock()
    managed.list_events.return_value = [
        {"id": "evt_1", "type": "agent.message", "content": [{"type": "text", "text": "done"}]}
    ]
    slack = MagicMock()
    monkeypatch.setattr(runtime, "db", db)
    monkeypatch.setattr(runtime, "agent", managed)
    with pytest.raises(RuntimeError, match="still active"):
        slack_projector._project_completed(
            runtime,
            enabled,
            slack,
            ManagedAgentSessionChanged(
                webhookEventId="wh_1", webhookType="session.status_idle", sessionId="sesn_1"
            ),
        )
    db.claim_projection.assert_not_called()
    slack.post_reply.assert_not_called()
    slack.stop_stream.assert_not_called()
