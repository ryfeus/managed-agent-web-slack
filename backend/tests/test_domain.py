from __future__ import annotations

import pytest
from pydantic import ValidationError

from managed_agents_app.domain import SlackMessageReceived, SlackProjectionRequested, SlackWorkStarted


def test_event_boundary_preserves_v1_camel_case_contract() -> None:
    detail = SlackMessageReceived(
        slack_event_id="Ev1",
        team_id="T1",
        channel_id="C1",
        thread_ts="1.0",
        message_ts="1.1",
        user_id="U1",
        text="hello",
        active_context={"channel_id": "C1"},
    )
    assert detail.boundary() == {
        "version": 1,
        "slackEventId": "Ev1",
        "teamId": "T1",
        "channelId": "C1",
        "threadTs": "1.0",
        "messageTs": "1.1",
        "userId": "U1",
        "text": "hello",
        "command": "message",
        "activeContext": {"channel_id": "C1"},
        "eventType": "app_mention",
    }


def test_projection_contract_accepts_existing_eventbridge_payload() -> None:
    detail = SlackProjectionRequested.model_validate(
        {
            "version": 1,
            "requestId": "Ev1",
            "sessionId": "sesn_1",
            "bindingId": "binding-1",
            "teamId": "T1",
            "channelId": "C1",
            "threadTs": "1.0",
            "userId": "U1",
            "inputEventId": "evt_1",
        }
    )
    assert detail.session_id == "sesn_1"
    assert detail.boundary()["inputEventId"] == "evt_1"
    assert detail.source_message_ts is None


def test_projection_contract_accepts_source_timestamp() -> None:
    detail = SlackProjectionRequested.model_validate(
        {
            "version": 1,
            "requestId": "Ev1",
            "sessionId": "sesn_1",
            "bindingId": "binding-1",
            "teamId": "T1",
            "channelId": "C1",
            "threadTs": "1.0",
            "userId": "U1",
            "sourceMessageTs": "1.1",
        }
    )
    assert detail.boundary()["sourceMessageTs"] == "1.1"


def test_work_started_v1_contract() -> None:
    detail = SlackWorkStarted(
        request_id="Ev1",
        team_id="T1",
        channel_id="C1",
        thread_ts="1.0",
        message_ts="1.1",
        user_id="U1",
    )
    assert detail.boundary() == {
        "version": 1,
        "requestId": "Ev1",
        "teamId": "T1",
        "channelId": "C1",
        "threadTs": "1.0",
        "messageTs": "1.1",
        "userId": "U1",
    }


def test_contract_rejects_future_or_missing_version() -> None:
    with pytest.raises(ValidationError):
        SlackMessageReceived.model_validate(
            {
                "version": 2,
                "slackEventId": "Ev1",
                "teamId": "T1",
                "channelId": "C1",
                "threadTs": "1",
                "messageTs": "1",
                "userId": "U1",
                "text": "hello",
            }
        )
