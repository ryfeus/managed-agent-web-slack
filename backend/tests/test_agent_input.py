from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from managed_agents_app.db import IngressClaim
from managed_agents_app.domain import (
    SLACK_CONTROL_REPLY_REQUESTED,
    SLACK_PROJECTION_REQUESTED,
    SLACK_WORK_STARTED,
)
from managed_agents_app.handlers import agent_input


def message_event() -> dict:
    return {
        "detail-type": "SlackMessageReceived",
        "detail": {
            "version": 1,
            "slackEventId": "Ev1",
            "teamId": "T1",
            "channelId": "C1",
            "threadTs": "1.0",
            "messageTs": "1.1",
            "userId": "U1",
            "text": "hello",
            "eventType": "app_mention",
        },
    }


def shortcut_event() -> dict:
    return {
        "detail-type": "SlackShortcutReceived",
        "detail": {
            "version": 1,
            "interactionId": "Ix1",
            "teamId": "T1",
            "channelId": "C1",
            "messageTs": "1.2",
            "threadTs": "1.0",
            "userId": "U1",
            "callbackId": "summarize_thread",
        },
    }


def _binding() -> dict[str, str]:
    return {"binding_id": "binding-1", "session_id": "sesn_1"}


def test_message_emits_work_before_claude_and_exact_source(runtime, monkeypatch, config) -> None:
    timeline: list[str] = []
    db = MagicMock()
    db.get_slack_binding.return_value = _binding()
    db.claim_ingress.return_value = IngressClaim.ACQUIRED
    db.resolve_external_identity.return_value = "principal-1"
    db.owns_session.return_value = True
    managed = MagicMock()
    managed.send_message.side_effect = lambda *_args: timeline.append("deliver") or "evt-input"

    def put(_source, detail_type, detail):
        timeline.append(detail_type)
        if detail_type == SLACK_PROJECTION_REQUESTED:
            assert detail.source_message_ts == "1.1"

    monkeypatch.setattr(agent_input, "load_config", lambda: config)
    monkeypatch.setattr(runtime, "db", db)
    monkeypatch.setattr(runtime, "agent", managed)
    monkeypatch.setattr(runtime.events, "publish", put)
    agent_input.handler(message_event(), None)
    assert timeline == [SLACK_WORK_STARTED, "deliver", SLACK_PROJECTION_REQUESTED]
    db.complete_ingress.assert_called_once_with("slack", "Ev1", "sesn_1", "evt-input")


def test_unowned_message_binding_completes_without_work_or_claude(runtime, monkeypatch, config) -> None:
    emitted: list[tuple[str, object]] = []
    db = MagicMock()
    db.get_slack_binding.return_value = _binding()
    db.claim_ingress.return_value = IngressClaim.ACQUIRED
    db.resolve_external_identity.return_value = "principal-1"
    db.owns_session.return_value = False
    managed_factory = MagicMock()
    monkeypatch.setattr(agent_input, "load_config", lambda: config)
    monkeypatch.setattr(runtime, "db", db)
    monkeypatch.setattr(runtime, "agent", managed_factory)
    monkeypatch.setattr(
        runtime.events, "publish", lambda _source, detail_type, detail: emitted.append((detail_type, detail))
    )
    agent_input.handler(message_event(), None)
    assert [kind for kind, _ in emitted] == [SLACK_CONTROL_REPLY_REQUESTED]
    assert emitted[0][1].text == "This request is not authorized for the linked session."
    managed_factory.assert_not_called()
    db.complete_ingress.assert_called_once_with("slack", "Ev1", None)


def test_active_ingress_lease_is_retryable(runtime, monkeypatch, config) -> None:
    db = MagicMock()
    db.get_slack_binding.return_value = _binding()
    db.claim_ingress.return_value = IngressClaim.BUSY
    monkeypatch.setattr(agent_input, "load_config", lambda: config)
    monkeypatch.setattr(runtime, "db", db)

    with pytest.raises(agent_input.RetryableDeliveryError):
        agent_input.handler(message_event(), None)


def test_shortcut_checks_ownership_before_fetching_context(runtime, monkeypatch, config) -> None:
    enabled = config.model_copy(update={"slack_shortcuts_enabled": True})
    db = MagicMock()
    db.claim_ingress.return_value = IngressClaim.ACQUIRED
    db.resolve_external_identity.return_value = "principal-1"
    db.get_slack_binding.return_value = _binding()
    db.owns_session.return_value = False
    slack = MagicMock()
    monkeypatch.setattr(agent_input, "load_config", lambda: enabled)
    monkeypatch.setattr(runtime, "db", db)
    monkeypatch.setattr(agent_input, "_slack", lambda _runtime, _config: slack)
    monkeypatch.setattr(runtime.events, "publish", MagicMock())
    agent_input.handler(shortcut_event(), None)
    slack.conversation_replies.assert_not_called()
    db.complete_ingress.assert_called_once_with("slack-shortcut", "Ix1", None)


def test_shortcut_context_precedes_work_and_projection_uses_selected_message(
    runtime, monkeypatch, config
) -> None:
    enabled = config.model_copy(update={"slack_shortcuts_enabled": True})
    timeline: list[str] = []
    db = MagicMock()
    db.claim_ingress.return_value = IngressClaim.ACQUIRED
    db.resolve_external_identity.return_value = "principal-1"
    db.get_slack_binding.return_value = _binding()
    db.owns_session.return_value = True
    slack = MagicMock()
    slack.conversation_replies.side_effect = lambda *_args: (
        timeline.append("context") or [{"ts": "1.2", "user": "U2", "text": "selected"}]
    )
    managed = MagicMock()
    managed.send_message.side_effect = lambda *_args: timeline.append("deliver") or "evt-input"

    def put(_source, detail_type, detail):
        timeline.append(detail_type)
        if detail_type == SLACK_WORK_STARTED:
            assert detail.message_ts == "1.2"
        if detail_type == SLACK_PROJECTION_REQUESTED:
            assert detail.source_message_ts == "1.2"

    monkeypatch.setattr(agent_input, "load_config", lambda: enabled)
    monkeypatch.setattr(runtime, "db", db)
    monkeypatch.setattr(agent_input, "_slack", lambda _runtime, _config: slack)
    monkeypatch.setattr(runtime, "agent", managed)
    monkeypatch.setattr(runtime.events, "publish", put)
    agent_input.handler(shortcut_event(), None)
    assert timeline == ["context", SLACK_WORK_STARTED, "deliver", SLACK_PROJECTION_REQUESTED]
