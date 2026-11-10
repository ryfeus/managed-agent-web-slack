from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from google.protobuf.json_format import MessageToDict
from test_cma_controller_postgres import HUMAN_INPUT_OPTIONS, message, tool_response
from test_cma_controller_postgres import controller as controller

from managed_agents_app.cma_controller.custom_tools import (
    CustomToolRegistry,
    RegisteredTool,
    TextMetricsInput,
    ToolResult,
    text_metrics,
)
from managed_agents_app.cma_controller.runtime_repository import LostSchedulerClaim
from managed_agents_app.cma_controller.scheduler import SchedulerBusy
from managed_agents_app.db.connection import connect
from managed_agents_app.policy.models import PolicyError
from managed_agents_app.policy.openappa import OpenAppaPolicyEngine
from managed_agents_app.protocols.controller_profile import HUMAN_INPUT_EXTENSION_URI

pytestmark = pytest.mark.skipif(os.getenv("RUN_POSTGRES_TESTS") != "1", reason="Run through npm run e2e")
ROOT = Path(__file__).resolve().parents[3]


def install(controller, handler=None):
    repository, _provider, _pending, _trigger, _service, scheduler = controller
    calls = []

    async def counted(arguments, context):
        calls.append(context.invocation_id)
        with connect(repository.config) as conn:
            saved = conn.execute(
                "SELECT * FROM cma_tool_executions WHERE invocation_id = %s", (context.invocation_id,)
            ).fetchone()
        assert saved["state"] == "running" and saved["execution_attempted_at"]
        assert saved["authorization_object_key"]
        return await (handler or text_metrics)(arguments, context)

    scheduler.custom_tools.registry = CustomToolRegistry(
        (RegisteredTool("text_metrics", TextMetricsInput, counted),)
    )
    scheduler.policy_engine.custom_tools = ("text_metrics",)
    return calls


async def start(controller, *, name="text_metrics", arguments=None, extra=()):
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("custom tool turn"))
    await scheduler.run_once(task.context_id)
    session = str(repository.get_context(task.context_id)["cma_session_id"])
    events = [
        {
            "id": "custom-call",
            "type": "agent.custom_tool_use",
            "name": name,
            "input": {"text": "private-input é"} if arguments is None else arguments,
        },
        *extra,
    ]
    for event in events:
        provider.emit(session, event)
    provider.emit(
        session,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": [e["id"] for e in reversed(events)]},
        },
    )
    return task, session


def execution(controller, task):
    return controller[-1].custom_tools.executions.get(task.id, "custom-call")


def results(provider, session):
    return [e for e in provider.events[session] if e["type"] == "user.custom_tool_result"]


@pytest.mark.asyncio
async def test_custom_execution_commits_before_effect_and_delivery_and_deduplicates(controller, monkeypatch):
    repository, provider, pending, _trigger, service, scheduler = controller
    calls = install(controller)
    task, session = await start(controller)
    send = provider.send_custom_tool_result

    async def fenced(*args, **kwargs):
        row = execution(controller, task)
        assert row["state"] == "delivering" and row["delivery_attempted_at"] and row["result_digest"]
        return await send(*args, **kwargs)

    monkeypatch.setattr(provider, "send_custom_tool_result", fenced)
    for _ in range(3):
        await scheduler.run_once(task.context_id)
    assert len(calls) == 1 and provider.custom_result_calls == 1 and provider.confirm_calls == 0
    result = results(provider, session)[0]
    assert json.loads(result["content"][0]["text"]) == {"characters": 15, "utf8_bytes": 16}
    assert not result["is_error"]
    assert execution(controller, task)["state"] == "resolved"
    assert "private-input" not in str(execution(controller, task))
    assert pending.list_keys() == []
    provider.finish(session)
    await scheduler.run_once(task.context_id)
    view = await service.get_task(task.id)
    assert "private-input" not in str(MessageToDict(view))
    assert repository.get_task(task.id)["a2a_state"] == "COMPLETED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args,code",
    [
        ("unregistered", {}, "unknown-tool"),
        ("text_metrics", {"text": 3}, "invalid-arguments"),
        ("text_metrics", {"text": "x", "extra": True}, "invalid-arguments"),
    ],
)
async def test_invalid_custom_calls_return_fixed_errors_without_executing(controller, name, args, code):
    calls = install(controller)
    task, session = await start(controller, name=name, arguments=args)
    await controller[-1].run_once(task.context_id)
    assert not calls and controller[1].confirm_calls == 0
    assert execution(controller, task)["control_code"] == code
    assert results(controller[1], session)[0]["is_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ruling", ["allow", "deny"])
async def test_custom_native_review_survives_reload_and_settles_once(controller, tmp_path, ruling):
    repository, provider, _pending, _trigger, service, scheduler = controller
    calls = install(controller)
    policy = tmp_path / "review.toml"
    policy.write_text((ROOT / "services/openappa/tests/human-review.toml").read_text())
    scheduler.policy_engine = OpenAppaPolicyEngine(
        scheduler.policy_engine.binary, str(policy), custom_tools=("text_metrics",)
    )
    task, session = await start(controller)
    await scheduler.run_once(task.context_id)
    assert not calls and provider.custom_result_calls == 0
    view = await service.get_task(task.id, options=HUMAN_INPUT_OPTIONS)
    assert "text_metrics" in str(MessageToDict(view.status.message))
    row = repository.list_input_requests(task.id)[0]
    response = tool_response(task, "unused", ruling)
    response.metadata[HUMAN_INPUT_EXTENSION_URI]["response"]["requestId"] = row["request_id"]
    await service.send(response, options=HUMAN_INPUT_OPTIONS)
    await service.send(response, options=HUMAN_INPUT_OPTIONS)
    for _ in range(2):
        await scheduler.run_once(task.context_id)
    assert len(calls) == (1 if ruling == "allow" else 0)
    assert len(results(provider, session)) == 1
    assert results(provider, session)[0]["is_error"] == (ruling == "deny")
    assert provider.confirm_calls == 0


@pytest.mark.asyncio
async def test_result_policy_failure_retries_only_policy_and_uses_pinned_authorization(
    controller, monkeypatch, tmp_path
):
    calls = install(controller)
    task, session = await start(controller)
    scheduler = controller[-1]
    engine = scheduler.policy_engine
    real = engine.evaluate_tool_result
    attempts = []

    async def unavailable(*args):
        attempts.append(args)
        raise PolicyError("policy-unavailable")

    monkeypatch.setattr(engine, "evaluate_tool_result", unavailable)
    for _ in range(2):
        await scheduler.run_once(task.context_id)
    assert len(calls) == 1 and not results(controller[1], session)
    assert execution(controller, task)["state"] == "result_ready"
    assert len(attempts) >= 2
    # A new worker's current policy is irrelevant to this already-executed call.
    path = tmp_path / "changed.toml"
    path.write_text("invalid current policy")
    engine.policy_path = str(path)
    monkeypatch.setattr(engine, "evaluate_tool_result", real)
    await scheduler.run_once(task.context_id)
    assert len(calls) == 1 and len(results(controller[1], session)) == 1


@pytest.mark.asyncio
async def test_native_withheld_result_never_reaches_provider_views_or_database(controller, tmp_path, caplog):
    async def private_result(_args, _context):
        return ToolResult("withheld-private-output" * 40)

    calls = install(controller, private_result)
    scheduler = controller[-1]
    path = tmp_path / "small.toml"
    path.write_text(
        Path(scheduler.policy_engine.policy_path)
        .read_text()
        .replace("max_body_bytes = 65536", "max_body_bytes = 256")
    )
    scheduler.policy_engine.policy_path = str(path)
    task, session = await start(controller)
    for _ in range(3):
        await scheduler.run_once(task.context_id)
    assert len(calls) == 1 and not results(controller[1], session)
    assert execution(controller, task)["state"] == "withheld"
    assert controller[0].get_task(task.id)["held_reason"] == "tool-result-withheld"
    assert "withheld-private-output" not in caplog.text
    assert "withheld-private-output" not in str(execution(controller, task))
    assert "withheld-private-output" not in str(MessageToDict(await controller[4].get_task(task.id)))
    # Live references survive orphan collection until Stop/operator failure.
    assert set(controller[2].list_keys()) <= controller[0].pending_input_keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "timeout", "oversize", "declared"])
async def test_execution_failure_modes_do_not_replay_or_leak(controller, monkeypatch, caplog, failure):
    async def handler(_args, _context):
        if failure == "exception":
            raise RuntimeError("private-handler-exception")
        if failure == "timeout":
            await asyncio.sleep(10)
        if failure == "oversize":
            return ToolResult("x" * 65537)
        return ToolResult.failure()

    monkeypatch.setattr("managed_agents_app.cma_controller.tool_execution.TOOL_TIMEOUT_SECONDS", 0.05)
    calls = install(controller, handler)
    task, session = await start(controller)
    for _ in range(2):
        await controller[-1].run_once(task.context_id)
    assert len(calls) == 1
    if failure == "declared":
        assert results(controller[1], session)[0]["content"][0]["text"] == "The custom tool failed."
    else:
        assert not results(controller[1], session)
    assert "private-handler-exception" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_uncertain_delivery_reconciles_without_reexecution_or_resend(controller, monkeypatch, accepted):
    calls = install(controller)
    task, session = await start(controller)
    provider, scheduler = controller[1], controller[-1]
    send = provider.send_custom_tool_result
    attempts = []

    async def lost(*args, **kwargs):
        attempts.append((args, kwargs))
        if accepted:
            await send(*args, **kwargs)
        raise TimeoutError("private-provider-error")

    monkeypatch.setattr(provider, "send_custom_tool_result", lost)
    for _ in range(3):
        await scheduler.run_once(task.context_id)
    assert len(calls) == len(attempts) == 1
    if not accepted:
        assert execution(controller, task)["state"] == "delivering"
        # Same event ID with different content is not proof of our delivery.
        provider.emit(
            session,
            {
                "type": "user.custom_tool_result",
                "custom_tool_use_id": "custom-call",
                "content": [{"type": "text", "text": "wrong"}],
                "is_error": False,
            },
        )
        await scheduler.run_once(task.context_id)
        assert execution(controller, task)["state"] == "delivering"
        await send(*attempts[0][0], **attempts[0][1])
        await scheduler.run_once(task.context_id)
    assert execution(controller, task)["state"] == "resolved"
    assert len(attempts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_result_object_put_recovery_never_reexecutes(controller, monkeypatch, accepted):
    calls = install(controller)
    task, session = await start(controller)
    pending, scheduler = controller[2], controller[-1]
    put = pending.put

    def lost(key, value):
        if '"result":' in value:
            if accepted:
                put(key, value)
            raise TimeoutError("private-storage-error")
        put(key, value)

    monkeypatch.setattr(pending, "put", lost)
    await scheduler.run_once(task.context_id)
    monkeypatch.setattr(pending, "put", put)
    await scheduler.run_once(task.context_id)
    assert len(calls) == 1
    assert bool(results(controller[1], session)) is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_running_handler_has_one_lease_and_is_cancelled_on_stop_or_expiry(controller, stop):
    repository, provider, _pending, _trigger, _service, scheduler = controller
    started = asyncio.Event()
    ended = asyncio.Event()

    async def handler(_args, _context):
        started.set()
        try:
            await asyncio.sleep(20)
        finally:
            ended.set()

    calls = install(controller, handler)
    task, session = await start(controller)
    running = asyncio.create_task(scheduler.run_once(task.context_id))
    await asyncio.wait_for(started.wait(), 5)
    with pytest.raises(SchedulerBusy):
        await scheduler.run_once(task.context_id)
    if stop:
        repository.cancel_task(task.id)
        provider.interrupt_preserves_approval = True
    else:
        with connect(repository.config) as conn:
            conn.execute(
                "UPDATE cma_contexts SET scheduler_lease_expires_at = "
                "CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE context_id = %s",
                (task.context_id,),
            )
    with pytest.raises(LostSchedulerClaim):
        await running
    assert ended.is_set()
    for _ in range(2):
        await scheduler.run_once(task.context_id)
    assert len(calls) == 1
    if stop:
        assert results(provider, session)[0]["is_error"]
        assert results(provider, session)[0]["content"][0]["text"] == "Task cancelled by user."
    else:
        assert not results(provider, session)


@pytest.mark.asyncio
async def test_stop_during_running_status_with_null_reason_does_not_dispatch_a_tool(controller):
    calls = install(controller)
    task, session = await start(controller)
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.emit(session, {"type": "session.status_running", "stop_reason": None})
    await service.cancel(task.id)
    for _ in range(2):
        await scheduler.run_once(task.context_id)
    assert not calls and not results(provider, session)
    assert repository.get_task(task.id)["a2a_state"] == "CANCELED"


@pytest.mark.asyncio
async def test_insufficient_budget_defers_execution_and_mixed_calls_survive_partial_resume(controller):
    calls = install(controller)
    task, session = await start(
        controller,
        extra=(
            {"id": "built-in", "type": "agent.tool_use", "name": "web_fetch", "input": {}},
            {
                "id": "clarify",
                "type": "agent.custom_tool_use",
                "name": "ask_user",
                "input": {"prompt": "Choose?"},
            },
        ),
    )
    await controller[-1].run_once(task.context_id, remaining_seconds=6)
    assert not calls
    for _ in range(2):
        await controller[-1].run_once(task.context_id)
    assert len(calls) == 1 and len(results(controller[1], session)) == 1
    assert controller[1].confirm_calls == 1
    assert controller[0].get_task(task.id)["a2a_state"] == "INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_changed_custom_call_rotates_review_before_execution(controller, tmp_path):
    repository, provider, _pending, _trigger, service, scheduler = controller
    calls = install(controller)
    policy = tmp_path / "review.toml"
    policy.write_text((ROOT / "services/openappa/tests/human-review.toml").read_text())
    scheduler.policy_engine.policy_path = str(policy)
    task, session = await start(controller)
    await scheduler.run_once(task.context_id)
    first = repository.list_input_requests(task.id)[0]
    response = tool_response(task, "unused", "allow")
    response.metadata[HUMAN_INPUT_EXTENSION_URI]["response"]["requestId"] = first["request_id"]
    await service.send(response, options=HUMAN_INPUT_OPTIONS)
    for event in provider.events[session]:
        if event.get("id") == "custom-call":
            event["input"] = {"text": "changed consent"}
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    current = repository.list_input_requests(task.id)[0]
    assert current["request_id"] != first["request_id"]
    assert current["human_ruling"] is None and current["review_digest"] != first["review_digest"]
    assert not calls and not results(provider, session)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["running", "admitted", "delivering"])
async def test_failed_checkpoint_prevents_its_external_effect(controller, monkeypatch, point):
    calls = install(controller)
    task, session = await start(controller)
    scheduler = controller[-1]
    transition = scheduler.custom_tools.executions.transition

    def fail(row, context, claim, state, **fields):
        if state == point:
            raise RuntimeError("checkpoint unavailable")
        return transition(row, context, claim, state, **fields)

    monkeypatch.setattr(scheduler.custom_tools.executions, "transition", fail)
    await scheduler.run_once(task.context_id)
    assert len(calls) == (0 if point == "running" else 1)
    assert not results(controller[1], session)
    monkeypatch.setattr(scheduler.custom_tools.executions, "transition", transition)
    await scheduler.run_once(task.context_id)
    assert len(calls) == 1 and len(results(controller[1], session)) == 1


@pytest.mark.asyncio
async def test_result_binding_mismatch_is_held_without_exposing_or_reexecuting(controller, monkeypatch):
    calls = install(controller)
    task, session = await start(controller)
    scheduler = controller[-1]
    native = scheduler.policy_engine.evaluate_tool_result

    async def unavailable(*_args):
        raise PolicyError()

    monkeypatch.setattr(scheduler.policy_engine, "evaluate_tool_result", unavailable)
    await scheduler.run_once(task.context_id)
    row = execution(controller, task)
    path = controller[2].directory / row["result_object_key"]
    artifact = json.loads(path.read_text())
    artifact["invocation_id"] = "another-invocation"
    path.write_text(json.dumps(artifact))
    monkeypatch.setattr(scheduler.policy_engine, "evaluate_tool_result", native)
    await scheduler.run_once(task.context_id)
    assert len(calls) == 1 and not results(controller[1], session)
    assert execution(controller, task)["state"] == "result_ready"
