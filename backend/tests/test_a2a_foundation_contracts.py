from __future__ import annotations

import pytest
from a2a.types import TaskState
from ag_ui.core import RunStartedEvent
from pydantic import TypeAdapter, ValidationError

from managed_agents_app.agents.registry import AgentRegistration, AgentRegistry, from_config
from managed_agents_app.config import load_config
from managed_agents_app.protocols.controller_profile import (
    ASYNC_COPILOT_PROFILE_URI,
    CMA_INITIAL_BEHAVIOR,
    HUMAN_INPUT_EXTENSION_URI,
    ControllerBehavior,
    ControllerProfile,
)
from managed_agents_app.protocols.human_input import HumanInputRequest, HumanInputResponse


def test_profile_matches_pinned_protocol_baseline() -> None:
    profile = ControllerProfile()
    assert profile.uri.endswith("/async-copilot-controller/v1")
    assert profile.uri == ASYNC_COPILOT_PROFILE_URI
    assert HUMAN_INPUT_EXTENSION_URI.endswith("/human-input/v1")
    assert profile.transport == "http-json"
    assert all(
        (
            profile.streaming,
            profile.durable_task_history,
            profile.cancel_task,
            profile.push_notifications,
            profile.durable_message_id_idempotency,
        )
    )
    assert (profile.conversational_output, profile.produced_object_output) == ("message", "artifact")
    assert ControllerBehavior().human_input is False
    assert ControllerBehavior(human_input=True) == CMA_INITIAL_BEHAVIOR
    assert TaskState.Name(TaskState.TASK_STATE_INPUT_REQUIRED) == "TASK_STATE_INPUT_REQUIRED"
    assert {"thread_id", "run_id"} <= set(RunStartedEvent.model_fields)


def test_human_input_wire_payloads_and_responses() -> None:
    requests = TypeAdapter(HumanInputRequest)
    approval = requests.validate_python(
        {
            "kind": "tool_approval",
            "requestId": "input_1",
            "tool": {"name": "refund_invoice", "arguments": {"invoice_id": "inv_1"}},
            "allowedResponses": ["allow", "deny"],
        }
    )
    assert approval.model_dump(by_alias=True)["requestId"] == "input_1"
    clarification = requests.validate_python(
        {
            "kind": "clarification",
            "requestId": "input_2",
            "prompt": "Which environment?",
            "choices": ["development", "production"],
        }
    )
    assert clarification.choices == ["development", "production"]

    responses = TypeAdapter(HumanInputResponse)
    assert (
        responses.validate_python(
            {"kind": "tool_approval", "requestId": "input_1", "decision": "deny"}
        ).decision
        == "deny"
    )
    assert (
        responses.validate_python(
            {"kind": "clarification", "requestId": "input_2", "answer": "production"}
        ).answer
        == "production"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "tool_approval",
            "requestId": "x",
            "tool": {"name": "run", "arguments": {}},
            "tool_use_id": "x",
        },
        {"kind": "clarification", "requestId": "x", "prompt": "choose", "slackChannelId": "C1"},
        {
            "kind": "tool_approval",
            "requestId": "x",
            "tool": {"name": "run", "arguments": {}},
            "allowedResponses": ["allow"],
        },
    ],
)
def test_human_input_rejects_provider_fields_and_invalid_decisions(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(HumanInputRequest).validate_python(payload)


def test_static_registry_configuration_and_validation(config) -> None:
    assert from_config(config).default() is None
    assert from_config(config).get("cma") is None
    registry = from_config(config.model_copy(update={"cma_a2a_endpoint": "https://internal.example/a2a"}))
    assert registry.default() == AgentRegistration("cma", "https://internal.example/a2a")
    assert registry.get("missing") is None
    assert set(AgentRegistration.__dataclass_fields__) == {
        "agent_id",
        "endpoint",
        "transport",
        "required_profile",
    }
    with pytest.raises(ValueError, match="HTTP"):
        AgentRegistration("cma", "ftp://internal.example")
    with pytest.raises(ValueError, match="HTTP"):
        from_config(config.model_copy(update={"cma_a2a_endpoint": "ftp://internal.example"}))
    with pytest.raises(ValueError, match="Duplicate"):
        AgentRegistry((AgentRegistration("cma", "http://one"), AgentRegistration("cma", "http://two")))
    with pytest.raises(ValueError, match="Default"):
        AgentRegistry((), default_agent_id="cma")


def test_a2a_endpoint_loads_through_app_config(monkeypatch) -> None:
    monkeypatch.setenv("CMA_A2A_ENDPOINT", " https://internal.example/a2a ")
    load_config.cache_clear()
    try:
        assert load_config().cma_a2a_endpoint == "https://internal.example/a2a"
    finally:
        load_config.cache_clear()
