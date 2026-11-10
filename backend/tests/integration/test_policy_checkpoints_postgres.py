from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_cma_controller_postgres import HUMAN_INPUT_OPTIONS, tool_response
from test_cma_controller_postgres import controller as controller
from test_custom_tools_postgres import execution, install, results, start

from managed_agents_app.cma_controller.runtime_repository import LostSchedulerClaim
from managed_agents_app.db.connection import connect
from managed_agents_app.policy.models import PolicyState, ToolPolicyRequest
from managed_agents_app.policy.snapshots import FileSnapshotStore
from managed_agents_app.protocols.controller_profile import HUMAN_INPUT_EXTENSION_URI

pytestmark = pytest.mark.skipif(os.getenv("RUN_POSTGRES_TESTS") != "1", reason="Run through npm run e2e")
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def persisted(controller, tmp_path):
    scheduler = controller[-1]
    scheduler.snapshots = FileSnapshotStore(tmp_path / "policy")
    scheduler.policy_engine.snapshots = scheduler.snapshots


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["call", "result"])
@pytest.mark.parametrize("committed", [False, True])
async def test_checkpoint_and_decision_are_atomic_and_lost_ack_recovers(
    controller, monkeypatch, boundary, committed
):
    repository, provider, _pending, _trigger, _service, scheduler = controller
    calls = install(controller)
    task, session = await start(controller)
    if boundary == "call":
        owner, method = repository, "record_policy_decision"
    else:
        owner, method = scheduler.custom_tools.executions, "transition"
    original = getattr(owner, method)

    def fail(*args, **kwargs):
        selected = boundary == "call" or args[3] == "admitted"
        if selected and not committed:
            raise RuntimeError("checkpoint unavailable")
        value = original(*args, **kwargs)
        if selected:
            raise TimeoutError("commit acknowledgment lost")
        return value

    monkeypatch.setattr(owner, method, fail)
    await scheduler.run_once(task.context_id)
    assert not results(provider, session)
    state = repository.get_context(task.context_id)
    assert state["policy_checkpoint_revision"] == (
        int(committed) if boundary == "call" else 1 + int(committed)
    )
    assert len(calls) == int(boundary == "result")
    monkeypatch.setattr(owner, method, original)
    for _ in range(3):
        await scheduler.run_once(task.context_id)
    assert len(calls) == 1 and len(results(provider, session)) == 1
    assert repository.get_context(task.context_id)["policy_checkpoint_revision"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["revision", "lease", "cancel"])
async def test_fences_roll_back_both_authorization_and_checkpoint(controller, fence):
    repository, _provider, _pending, _trigger, _service, scheduler = controller
    install(controller)
    task, _session = await start(controller)
    await scheduler.run_once(task.context_id, remaining_seconds=5)
    claim = repository.claim_scheduler(task.context_id)
    row = repository.list_input_requests(task.id)[0]
    request = ToolPolicyRequest(
        task.context_id,
        "custom-call",
        repository.config.app_name,
        "custom",
        "text_metrics",
        {"text": "private-input é"},
        state=PolicyState(),
    )
    decision = await scheduler.policy_engine.evaluate_tool_call(request, 20)
    key = scheduler.custom_tools.store_authorization(execution(controller, task), decision.authorization)
    checkpoint = decision.checkpoint
    if fence == "revision":
        checkpoint = replace(checkpoint, expected_revision=7)
    elif fence == "cancel":
        repository.cancel_task(task.id)
    else:
        with connect(repository.config) as conn:
            conn.execute(
                "UPDATE cma_contexts SET scheduler_lease_expires_at = "
                "CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE context_id = %s",
                (task.context_id,),
            )
    with pytest.raises(LostSchedulerClaim):
        repository.record_policy_decision(
            task.context_id,
            task.id,
            claim,
            row["request_id"],
            "allow",
            authorization_object_key=key,
            checkpoint=checkpoint,
        )
    assert repository.get_context(task.context_id)["policy_checkpoint_key"] is None
    assert repository.list_input_requests(task.id)[0]["decision"] is None
    assert execution(controller, task)["authorization_object_key"] is None
    repository.release_scheduler(task.context_id, claim)


@pytest.mark.asyncio
async def test_result_label_precedes_next_call_and_review_survives_reopening(controller):
    repository, provider, _pending, _trigger, service, scheduler = controller
    calls = install(controller)
    scheduler.policy_engine.policy_path = str(ROOT / "services/openappa/tests/stateful-custom.toml")
    task, session = await start(
        controller,
        extra=(
            {"id": "external", "type": "agent.tool_use", "name": "web_fetch", "input": {"to": "public"}},
            {"id": "internal", "type": "agent.tool_use", "name": "web_fetch", "input": {"to": "hr"}},
        ),
    )
    await scheduler.run_once(task.context_id)
    assert not calls and not provider.confirm_calls
    review = next(r for r in repository.list_input_requests(task.id) if r["cma_event_id"] == "custom-call")
    response = tool_response(task, "unused", "allow")
    response.metadata[HUMAN_INPUT_EXTENSION_URI]["response"]["requestId"] = review["request_id"]
    await service.send(response, options=HUMAN_INPUT_OPTIONS)
    for _ in range(4):
        await scheduler.run_once(task.context_id)
    decisions = {r["cma_event_id"]: r["decision"] for r in repository.list_input_requests(task.id)}
    assert decisions == {"custom-call": "allow", "external": "deny", "internal": "allow"}
    assert len(calls) == 1 and len(results(provider, session)) == 1
    assert repository.get_context(task.context_id)["policy_checkpoint_revision"] == 5
    assert "private-input" not in str(repository.get_context(task.context_id))


@pytest.mark.asyncio
async def test_missing_snapshot_after_execution_holds_without_replay(controller):
    repository, provider, _pending, _trigger, _service, scheduler = controller

    async def remove_checkpoint(args, context):
        from managed_agents_app.cma_controller.custom_tools import text_metrics

        key = repository.get_context(task.context_id)["policy_checkpoint_key"]
        scheduler.snapshots.delete(key)
        return await text_metrics(args, context)

    calls = install(controller, remove_checkpoint)
    task, session = await start(controller)
    for _ in range(2):
        await scheduler.run_once(task.context_id)
    assert len(calls) == 1 and not results(provider, session)
    assert execution(controller, task)["state"] == "result_ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_result_commit_racing_stop_or_lease_expiry_cannot_publish_or_deliver(
    controller, monkeypatch, cancel
):
    repository, provider, _pending, _trigger, _service, scheduler = controller
    calls = install(controller)
    task, session = await start(controller)
    evaluate = scheduler.policy_engine.evaluate_tool_result

    async def race(*args):
        result = await evaluate(*args)
        if cancel:
            repository.cancel_task(task.id)
        else:
            with connect(repository.config) as conn:
                conn.execute(
                    "UPDATE cma_contexts SET scheduler_lease_expires_at = "
                    "CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE context_id = %s",
                    (task.context_id,),
                )
        return result

    monkeypatch.setattr(scheduler.policy_engine, "evaluate_tool_result", race)
    with pytest.raises(LostSchedulerClaim):
        await scheduler.run_once(task.context_id)
    assert len(calls) == 1 and not results(provider, session)
    assert execution(controller, task)["state"] == "result_ready"
    assert repository.get_context(task.context_id)["policy_checkpoint_revision"] == 1


@pytest.mark.asyncio
async def test_sweep_preserves_current_snapshot_and_removes_only_old_unreferenced_objects(controller):
    repository, _provider, _pending, _trigger, _service, scheduler = controller
    install(controller)
    task, _session = await start(controller)
    await scheduler.run_once(task.context_id)
    key = repository.get_context(task.context_id)["policy_checkpoint_key"]
    files = list(scheduler.snapshots.directory.iterdir())
    assert len(files) == 2
    for path in files:
        os.utime(path, (time.time() - 90000, time.time() - 90000))
    scheduler.sweep_orphans()
    assert [p.name for p in scheduler.snapshots.directory.iterdir()] == [key]
