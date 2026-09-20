from __future__ import annotations

import json

import pytest

from managed_agents_app.handlers import agent_input_sqs


def _record(message_id: str = "message-1", body: object | None = None) -> dict:
    value = body if body is not None else {"detail-type": "SlackMessageReceived", "detail": {}}
    return {
        "messageId": message_id,
        "body": json.dumps(value),
        "attributes": {"ApproximateReceiveCount": "2"},
    }


def test_sqs_adapter_processes_a_valid_record(runtime, monkeypatch) -> None:
    received: list[dict] = []
    monkeypatch.setattr(
        agent_input_sqs, "handle_domain_event", lambda _runtime, event: received.append(event)
    )

    result = agent_input_sqs.handle_sqs_event(runtime, {"Records": [_record()]})

    assert result == {"batchItemFailures": []}
    assert received == [{"detail-type": "SlackMessageReceived", "detail": {}}]


def test_sqs_adapter_reports_malformed_json_as_a_partial_failure(runtime) -> None:
    result = agent_input_sqs.handle_sqs_event(
        runtime, {"Records": [{"messageId": "bad", "body": "not-json", "attributes": {}}]}
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


def test_sqs_adapter_reports_handler_failure_as_a_partial_failure(runtime, monkeypatch) -> None:
    monkeypatch.setattr(
        agent_input_sqs, "handle_domain_event", lambda *_: (_ for _ in ()).throw(RuntimeError())
    )
    result = agent_input_sqs.handle_sqs_event(runtime, {"Records": [_record()]})
    assert result == {"batchItemFailures": [{"itemIdentifier": "message-1"}]}


def test_sqs_adapter_keeps_successful_records_out_of_a_mixed_failure_response(runtime, monkeypatch) -> None:
    def process(_runtime, event):
        if event["detail"]["id"] == "bad":
            raise RuntimeError("planned")

    monkeypatch.setattr(agent_input_sqs, "handle_domain_event", process)
    result = agent_input_sqs.handle_sqs_event(
        runtime,
        {
            "Records": [
                _record("good", {"detail-type": "A", "detail": {"id": "good"}}),
                _record("bad", {"detail-type": "A", "detail": {"id": "bad"}}),
            ]
        },
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


@pytest.mark.parametrize("record", [{"body": "{}"}, {"messageId": "", "body": "{}"}])
def test_sqs_adapter_rejects_records_that_cannot_be_named_in_partial_response(runtime, record) -> None:
    with pytest.raises(ValueError, match="messageId"):
        agent_input_sqs.handle_sqs_event(runtime, {"Records": [record]})
