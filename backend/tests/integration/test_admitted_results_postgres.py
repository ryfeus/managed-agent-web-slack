"""Admitted bytes, snapshot pointer and execution state share the durable fence."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from test_cma_controller_postgres import controller as controller
from test_custom_tools_postgres import execution, results, start

from managed_agents_app.cma_controller.custom_tools import (
    DEMO_REPORT,
    CustomToolRegistry,
    RegisteredTool,
    default_custom_tools,
)
from managed_agents_app.policy.snapshots import FileSnapshotStore

pytestmark = pytest.mark.skipif(os.getenv("RUN_POSTGRES_TESTS") != "1", reason="Run through npm run e2e")
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def demo(controller, tmp_path):
    scheduler = controller[-1]
    registry = default_custom_tools()
    calls = []
    registrations = []
    for name in registry.names:
        arguments = {"text": "synthetic"} if name == "text_metrics" else {}
        if name == "demo_publish_report":
            arguments = {"audience": "public", "text": "synthetic"}
        registered, _ = registry.prepare(name, arguments)

        async def counted(arguments, context, handler=registered.handler):
            calls.append(context.invocation_id)
            return await handler(arguments, context)

        registrations.append(RegisteredTool(name, registered.input_model, counted))
    scheduler.custom_tools.registry = CustomToolRegistry(tuple(registrations))
    scheduler.snapshots = FileSnapshotStore(tmp_path / "snapshots")
    scheduler.policy_engine.snapshots = scheduler.snapshots
    scheduler.policy_engine.custom_tools = registry.names
    scheduler.policy_engine.policy_path = str(ROOT / "backend/src/managed_agents_app/policy/appa.toml")
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "store-before",
        "store-after",
        "commit-before",
        "commit-after",
        "delivery-ack",
        "delivery-unknown",
        "missing-admitted",
    ],
)
async def test_transformed_output_never_falls_back_to_raw_or_reexecutes(controller, demo, monkeypatch, fault):
    repository, provider, pending, _trigger, _service, scheduler = controller
    task, session = await start(controller, name="demo_read_contact_report", arguments={})
    put = pending.put
    transition = scheduler.custom_tools.executions.transition
    send = provider.send_custom_tool_result

    def interrupted_put(key, text):
        admitted = "raw_result_digest" in json.loads(text)
        if admitted and fault == "store-before":
            raise TimeoutError()
        put(key, text)
        if admitted and fault == "store-after":
            raise TimeoutError()

    def interrupted_commit(*args, **kwargs):
        admitted = args[3] == "admitted"
        if admitted and fault == "commit-before":
            raise TimeoutError()
        row = transition(*args, **kwargs)
        if admitted and fault in {"commit-after", "missing-admitted"}:
            if fault == "missing-admitted":
                pending.delete(row["admitted_result_object_key"])
            raise TimeoutError()
        return row

    async def interrupted_delivery(*args, **kwargs):
        if fault == "delivery-unknown":
            raise TimeoutError()
        await send(*args, **kwargs)
        if fault == "delivery-ack":
            raise TimeoutError()

    monkeypatch.setattr(pending, "put", interrupted_put)
    monkeypatch.setattr(scheduler.custom_tools.executions, "transition", interrupted_commit)
    monkeypatch.setattr(provider, "send_custom_tool_result", interrupted_delivery)
    await scheduler.run_once(task.context_id)
    row = execution(controller, task)
    assert len(demo) == 1
    if fault.startswith("store") or fault == "commit-before":
        assert row["state"] == "result_ready" and row["admitted_result_object_key"] is None
        assert repository.get_context(task.context_id)["policy_checkpoint_revision"] == 1
    elif fault != "delivery-ack":
        assert row["admitted_result_object_key"] and row["admitted_result_digest"] != row["result_digest"]
        assert repository.get_context(task.context_id)["policy_checkpoint_revision"] == 2
        assert row["admitted_result_object_key"] in repository.pending_input_keys()
    monkeypatch.setattr(pending, "put", put)
    monkeypatch.setattr(scheduler.custom_tools.executions, "transition", transition)
    monkeypatch.setattr(provider, "send_custom_tool_result", send)
    for _ in range(2):
        await scheduler.run_once(task.context_id)
    assert len(demo) == 1
    if fault in {"delivery-unknown", "missing-admitted"}:
        assert not results(provider, session)
        assert execution(controller, task)["state"] in {"delivering", "admitted"}
    else:
        assert len(results(provider, session)) == 1
        assert results(provider, session)[0]["content"] == [
            {"type": "text", "text": DEMO_REPORT.replace("alex@example.com", "[redacted-email]")}
        ]
        assert not results(provider, session)[0]["is_error"]
        assert pending.list_keys() == [] or fault.startswith("store") or fault == "commit-before"
    assert "alex@example.com" not in json.dumps(results(provider, session))
    assert "alex@example.com" not in str(execution(controller, task))
