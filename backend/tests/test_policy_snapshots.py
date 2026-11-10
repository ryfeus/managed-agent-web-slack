from __future__ import annotations

import hashlib
import io
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from managed_agents_app.policy.models import PolicyError, PolicyState, ToolPolicyRequest
from managed_agents_app.policy.openappa import OpenAppaPolicyEngine
from managed_agents_app.policy.snapshots import FileSnapshotStore, S3SnapshotStore, snapshot

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / ".generated/openappa-bin/openappa-cma"


def engine(tmp_path, policy="baseline"):
    return OpenAppaPolicyEngine(
        str(BINARY),
        str(ROOT / f"services/openappa/tests/{policy}.toml"),
        custom_tools=("text_metrics",),
        snapshots=FileSnapshotStore(tmp_path / "objects"),
    )


def request():
    return ToolPolicyRequest(
        "context", "event", "app", "custom", "text_metrics", {"text": "private"}, state=PolicyState()
    )


def advance(request, decision):
    checkpoint = decision.checkpoint
    return replace(
        request, state=PolicyState(checkpoint.key, checkpoint.expected_revision + 1, checkpoint.policy_digest)
    )


def batches(store, key, tmp_path):
    path = tmp_path / "inspect.sqlite"
    path.write_bytes(store.get(key))
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["baseline", "human-review"])
async def test_native_receipts_survive_fresh_processes_and_bind_results(tmp_path, policy):
    r = request()
    e = engine(tmp_path, policy)
    first = await e.evaluate_tool_call(r, 20)
    r = advance(r, first)
    if first.review:
        first = await engine(tmp_path, policy).resolve_tool_call(r, first.review, "allow", 20)
        r = advance(r, first)
    result = await engine(tmp_path, policy).evaluate_tool_result(
        r, first.authorization, "private-output", False, 20
    )
    assert result.decision == "deliver"
    r = advance(r, result)
    count = batches(e.snapshots, r.state.key, tmp_path)
    replay = await engine(tmp_path, policy).evaluate_tool_result(
        r, first.authorization, "private-output", False, 20
    )
    assert replay.decision == "deliver"
    assert batches(e.snapshots, replay.checkpoint.key, tmp_path) == count
    if first.authorization.review:
        with pytest.raises(PolicyError):
            await e.evaluate_tool_result(
                r, replace(first.authorization, ruling="deny"), "private-output", False, 20
            )
    with pytest.raises(PolicyError):
        await e.evaluate_tool_result(r, first.authorization, "different", False, 20)
    with pytest.raises(PolicyError):
        await e.evaluate_tool_result(
            replace(r, invocation_id="substituted"), first.authorization, "private-output", False, 20
        )


@pytest.mark.asyncio
async def test_native_accumulated_audience_survives_result_and_reopening(tmp_path):
    e = engine(tmp_path, "stateful-custom")
    r = request()
    review = await e.evaluate_tool_call(r, 20)
    assert review.decision == "human_review"
    r = advance(r, review)
    allowed = await engine(tmp_path, "stateful-custom").resolve_tool_call(r, review.review, "allow", 20)
    r = advance(r, allowed)
    result = await engine(tmp_path, "stateful-custom").evaluate_tool_result(
        r, allowed.authorization, "HR-private", False, 20
    )
    assert result.decision == "deliver"
    r = advance(r, result)
    for to, expected in [("public", "deny"), ("hr", "allow")]:
        r = replace(r, invocation_id=to, tool_kind="builtin", tool_name="web_fetch", arguments={"to": to})
        decision = await engine(tmp_path, "stateful-custom").evaluate_tool_call(r, 20)
        assert decision.decision == expected
        r = advance(r, decision)


@pytest.mark.asyncio
async def test_checkpoint_loss_corruption_policy_change_and_wrong_context_hold(tmp_path):
    e = engine(tmp_path)
    r = advance(request(), await e.evaluate_tool_call(request(), 20))
    original = e.snapshots.get(r.state.key)
    with pytest.raises(PolicyError, match="policy-version-changed"):
        await engine(tmp_path, "human-review").evaluate_tool_call(r, 20)
    with pytest.raises(PolicyError):
        await e.evaluate_tool_call(replace(r, root_id="wrong-context"), 20)
    path = tmp_path / "objects" / r.state.key
    path.write_bytes(b"corrupt")
    with pytest.raises(PolicyError, match="policy-checkpoint-invalid"):
        await e.evaluate_tool_call(r, 20)
    path.unlink()
    with pytest.raises(PolicyError, match="policy-checkpoint-unavailable"):
        await e.evaluate_tool_call(r, 20)
    path.write_bytes(original)
    assert (await e.evaluate_tool_call(r, 20)).decision == "allow"


@pytest.mark.asyncio
async def test_withholding_and_first_human_ruling_survive_reopening(tmp_path):
    e = engine(tmp_path, "custom-result-withheld")
    r = request()
    allowed = await e.evaluate_tool_call(r, 20)
    r = advance(r, allowed)
    outcome = await e.evaluate_tool_result(r, allowed.authorization, "private" * 200, False, 20)
    assert outcome.decision == "withhold" and "private" not in str(outcome)
    r = advance(r, outcome)
    assert (
        await e.evaluate_tool_result(r, allowed.authorization, "private" * 200, False, 20)
    ).decision == "withhold"
    e = engine(tmp_path / "review", "human-review")
    r = request()
    review = await e.evaluate_tool_call(r, 20)
    r = advance(r, review)
    denied = await e.resolve_tool_call(r, review.review, "deny", 20)
    assert denied.decision == "deny"
    r = advance(r, denied)
    with pytest.raises(PolicyError):
        await e.resolve_tool_call(r, review.review, "allow", 20)


@pytest.mark.asyncio
async def test_upload_accepted_but_ack_lost_cannot_return_a_decision(tmp_path, monkeypatch):
    e = engine(tmp_path)
    put = e.snapshots.put

    def lost_ack(key, body):
        put(key, body)
        raise TimeoutError()

    monkeypatch.setattr(e.snapshots, "put", lost_ack)
    with pytest.raises(PolicyError):
        await e.evaluate_tool_call(request(), 20)
    assert len(list((tmp_path / "objects").iterdir())) == 1
    monkeypatch.setattr(e.snapshots, "put", put)
    assert (await e.evaluate_tool_call(request(), 20)).decision == "allow"


def test_snapshot_contains_committed_wal_pages(tmp_path):
    source = tmp_path / "wal.sqlite"
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE example(value TEXT)")
    connection.execute("INSERT INTO example VALUES ('private committed result')")
    connection.commit()
    body = snapshot(source, tmp_path / "snapshot.sqlite")
    standalone = tmp_path / "standalone.sqlite"
    standalone.write_bytes(body)
    read = sqlite3.connect(standalone)
    try:
        assert read.execute("SELECT value FROM example").fetchone() == ("private committed result",)
    finally:
        read.close()
        connection.close()


def test_s3_uses_immutable_private_objects_and_closes_reads():
    class Client:
        def put_object(self, **kwargs):
            self.put = kwargs

        def get_object(self, **kwargs):
            self.stream = io.BytesIO(self.put["Body"])
            return {"Body": self.stream, "ContentLength": len(self.put["Body"])}

    client = Client()
    store = S3SnapshotStore("private", client)
    body = b"synthetic snapshot"
    key = "a" * 32 + "-" + hashlib.sha256(body).hexdigest()
    store.put(key, body)
    assert client.put["Key"] == f"policy/{key}"
    assert client.put["IfNoneMatch"] == "*" and client.put["ServerSideEncryption"] == "AES256"
    assert store.get(key) == body and client.stream.closed
