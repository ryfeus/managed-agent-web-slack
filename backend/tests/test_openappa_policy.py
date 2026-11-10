from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from managed_agents_app.policy.models import PolicyError, ToolAuthorization, ToolPolicyRequest
from managed_agents_app.policy.openappa import OpenAppaPolicyEngine

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "services/openappa/tests/baseline.toml"
BINARY = ROOT / ".generated/openappa-bin/openappa-cma"
REQUEST = ToolPolicyRequest("context", "event", "application", "builtin", "web_fetch", {"url": "secret"})


@pytest.mark.asyncio
async def test_native_allow_deny_and_independent_calls():
    engine = OpenAppaPolicyEngine(str(BINARY), str(POLICY))
    for name, expected in [("web_fetch", "allow"), ("unknown", "deny"), ("web_search", "allow")]:
        # Even identical context/event IDs cannot retain prior labels or receipts.
        decision = await engine.evaluate_tool_call(replace(REQUEST, tool_name=name), 10)
        assert decision.decision == expected
        assert decision.reason == ("Denied by OpenAPPA policy." if expected == "deny" else None)
    assert (
        await engine.evaluate_tool_call(replace(REQUEST, arguments={"changed": True}), 10)
    ).decision == "allow"


@pytest.mark.asyncio
async def test_native_mcp_and_current_policy_in_existing_context(tmp_path):
    policy = tmp_path / "appa.toml"
    policy.write_text((ROOT / "backend/tests/fixtures/openappa-mcp.toml").read_text())
    engine = OpenAppaPolicyEngine(str(BINARY), str(policy))
    request = replace(REQUEST, tool_kind="mcp", tool_name="mcp/fixture/lookup")
    assert (await engine.evaluate_tool_call(request, 10)).decision == "allow"
    assert (
        await engine.evaluate_tool_call(replace(request, tool_name="mcp/fixture/undeclared"), 10)
    ).decision == "deny"
    policy.write_text(POLICY.read_text())
    assert (await engine.evaluate_tool_call(request, 10)).decision == "deny"
    assert (await engine.evaluate_tool_call(REQUEST, 10)).decision == "allow"


def helper(tmp_path, program):
    path = tmp_path / "helper"
    path.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n" + program)
    path.chmod(0o755)
    return OpenAppaPolicyEngine(str(path), str(POLICY))


async def evaluate_boundary(engine, result, timeout):
    if result:
        return await engine.evaluate_tool_result(
            replace(REQUEST, tool_kind="custom", tool_name="text_metrics"),
            ToolAuthorization(POLICY.read_text(), "a" * 64),
            "private result",
            False,
            timeout,
        )
    return await engine.evaluate_tool_call(REQUEST, timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [False, True])
@pytest.mark.parametrize(
    "response",
    [
        "{}",
        "[]",
        "not json",
        '{"version":1,"result":{"decision":"allow","reason":null}}',
        '{"version":3,"result":{"decision":"allow","reason":null}}',
        '{"version":6,"result":{"decision":"surprise","reason":null}}',
        '{"version":6,"result":{"decision":"deliver"}}',
        '{"version":6,"result":{"decision":"allow","reason":null},"extra":true}',
        '{"version":6,"result":{"decision":"allow","reason":null,"extra":true}}',
        '{"version":6,"error":"invalid_policy","result":{"decision":"allow","reason":null}}',
        "x" * 65537,
    ],
)
async def test_invalid_native_response_fails_closed(tmp_path, response, result):
    engine = helper(tmp_path, "print(" + repr(response) + ")\n")
    with pytest.raises(PolicyError):
        await evaluate_boundary(engine, result, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_valid_native_results_do_not_expose_arbitrary_reason(tmp_path, decision):
    engine = helper(
        tmp_path,
        "print("
        + repr(json.dumps({"version": 6, "result": {"decision": decision, "reason": "private"}}))
        + ")\n",
    )
    result = await engine.evaluate_tool_call(REQUEST, 10)
    assert result.decision == decision and "private" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("result", [False, True])
async def test_timeout_or_cancellation_reaps_helper_without_exposing_arguments(
    tmp_path, monkeypatch, caplog, cancel, result
):
    engine = helper(tmp_path, "import time\ntime.sleep(60)\n")
    started = asyncio.Event()
    original = asyncio.create_subprocess_exec
    processes = []

    async def start(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    task = asyncio.create_task(evaluate_boundary(engine, result, 10 if cancel else 0.05))
    await started.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else PolicyError):
        await task
    assert processes[0].returncode is not None
    assert "secret" not in caplog.text
    assert "private result" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "program",
    ["raise SystemExit(1)\n", "print('private diagnostic', file=sys.stderr)\nraise SystemExit(1)\n"],
)
async def test_helper_failure_is_operational(tmp_path, caplog, program):
    with pytest.raises(PolicyError):
        await helper(tmp_path, program).evaluate_tool_call(REQUEST, 10)
    assert "private diagnostic" not in caplog.text


@pytest.mark.asyncio
async def test_missing_helper_is_operational(tmp_path):
    with pytest.raises(PolicyError, match="policy-unavailable"):
        await OpenAppaPolicyEngine(str(tmp_path / "missing"), str(POLICY)).evaluate_tool_call(REQUEST, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,code",
    [
        ("broken toml", "policy-invalid-policy"),
        (
            POLICY.read_text().replace("context_control = false", "context_control = true"),
            "policy-unsupported-policy",
        ),
        (
            POLICY.read_text().replace("confined_results = []", 'confined_results = ["host/cma/web_fetch"]'),
            "policy-unsupported-policy",
        ),
        (
            POLICY.read_text().replace("delta = {}", 'delta = {trust = "untrusted"}'),
            "policy-unsupported-policy",
        ),
        (
            POLICY.read_text().replace("delta = {}", 'requires = {effects = {contains = ["read"]}}'),
            "policy-unsupported-policy",
        ),
    ],
)
async def test_invalid_or_stateful_policy_is_operational(tmp_path, policy, code):
    # CMA-hosted results remain unmediated; no tool has cross-call native history.
    path = tmp_path / "appa.toml"
    path.write_text(policy)
    with pytest.raises(PolicyError, match=code):
        await OpenAppaPolicyEngine(str(BINARY), str(path)).evaluate_tool_call(REQUEST, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("runtime", "other"), ("tool_kind", "other"), ("tool_name", ""), ("arguments", [])]
)
async def test_invalid_request_fails_closed(field, value):
    with pytest.raises(PolicyError, match="policy-invalid-request"):
        await OpenAppaPolicyEngine(str(BINARY), str(POLICY)).evaluate_tool_call(
            replace(REQUEST, **{field: value}), 10
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"version": 1},
        {"version": 3},
        {"version": 4},
        {"unexpected_database": "/tmp/obsolete.db"},
        {"extra": True},
    ],
)
async def test_native_protocol_rejects_old_version_and_unknown_fields(extra):
    from dataclasses import asdict

    process = await asyncio.create_subprocess_exec(
        str(BINARY), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )
    body = {
        "version": 6,
        "policy": POLICY.read_text(),
        "request": {k: v for k, v in asdict(REQUEST).items() if k != "state"},
        **extra,
    }
    stdout, _ = await process.communicate(json.dumps(body).encode())
    assert json.loads(stdout) == {"version": 6, "error": "invalid_request"}


@pytest.mark.asyncio
async def test_native_review_and_resolution_are_bound_to_exact_call_and_policy(tmp_path):
    policy = tmp_path / "policy.toml"
    original = (ROOT / "services/openappa/tests/human-review.toml").read_text()
    policy.write_text(original)
    engine = OpenAppaPolicyEngine(str(BINARY), str(policy))
    request = replace(REQUEST, tool_name="web_search")
    decision = await engine.evaluate_tool_call(request, 10)
    assert decision.decision == "human_review" and decision.review
    review = decision.review
    assert review.authority == "user"
    assert "user-approval" in review.description and "secret" in review.description
    for ruling in ("allow", "deny"):
        assert (await engine.resolve_tool_call(request, review, ruling, 10)).decision == ruling
    with pytest.raises(PolicyError, match="policy-stale-review"):
        await engine.resolve_tool_call(replace(request, arguments={"changed": True}), review, "allow", 10)
    policy.write_text(original + "\n# updated")
    with pytest.raises(PolicyError, match="policy-stale-review"):
        await engine.resolve_tool_call(request, review, "allow", 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("review", [False, True])
async def test_custom_call_and_result_reconstruct_native_authorization(tmp_path, review):
    from managed_agents_app.cma_controller.tool_execution import AuthorizationRecord

    path = tmp_path / "policy.toml"
    path.write_text(
        (ROOT / "services/openappa/tests/human-review.toml").read_text() if review else POLICY.read_text()
    )
    engine = OpenAppaPolicyEngine(str(BINARY), str(path), custom_tools=("text_metrics",))
    request = replace(REQUEST, tool_kind="custom", tool_name="text_metrics", arguments={"text": "private"})
    decision = await engine.evaluate_tool_call(request, 10)
    if review:
        assert decision.decision == "human_review"
        decision = await engine.resolve_tool_call(request, decision.review, "allow", 10)
    assert decision.decision == "allow"
    assert decision.authorization is not None
    record = AuthorizationRecord(
        invocation_id="invocation", call_digest="digest", authorization=decision.authorization
    )
    restored = AuthorizationRecord.model_validate_json(record.model_dump_json())
    # A deployed policy change does not rewrite an already-committed authorization.
    path.write_text("not the policy used by this invocation")
    assert (
        await engine.evaluate_tool_result(request, restored.authorization, "private result", False, 10)
    ).decision == "deliver"
    assert (
        await engine.evaluate_tool_result(
            request, restored.authorization, "The custom tool failed.", True, 10
        )
    ).decision == "deliver"
    for changed in (replace(request, invocation_id="other"), replace(request, arguments={"text": "changed"})):
        with pytest.raises(PolicyError, match="stale-review"):
            await engine.evaluate_tool_result(changed, restored.authorization, "private result", False, 10)
    with pytest.raises(PolicyError, match="invalid-request"):
        await engine.evaluate_tool_result(request, restored.authorization, "private exception", True, 10)


@pytest.mark.asyncio
async def test_native_custom_result_withholding_and_namespace_isolation(tmp_path):
    path = tmp_path / "policy.toml"
    path.write_text(POLICY.read_text().replace("max_body_bytes = 65536", "max_body_bytes = 256"))
    engine = OpenAppaPolicyEngine(str(BINARY), str(path), custom_tools=("text_metrics", "web_fetch"))
    request = replace(REQUEST, tool_kind="custom", tool_name="text_metrics")
    allowed = await engine.evaluate_tool_call(request, 10)
    assert (
        await engine.evaluate_tool_result(request, allowed.authorization, "private" * 100, False, 10)
    ).decision == "withhold"
    assert (await engine.evaluate_tool_call(replace(request, tool_name="web_fetch"), 10)).decision == "deny"
    with pytest.raises(PolicyError, match="invalid-request"):
        await engine.evaluate_tool_call(replace(request, tool_name="unregistered"), 10)


@pytest.mark.asyncio
async def test_admitted_text_bound_is_decoded_utf8_not_json_envelope(tmp_path):
    # JSON escaping can use six wire bytes per admitted control character.
    text = "\x00" * 65536
    engine = helper(
        tmp_path,
        "print(" + repr(json.dumps({"version": 6, "result": {"decision": "deliver", "text": text}})) + ")\n",
    )
    assert (await evaluate_boundary(engine, True, 10)).text == text
    for oversized in ["x" * 65537, "é" * 32769]:
        engine = helper(
            tmp_path,
            "print("
            + repr(json.dumps({"version": 6, "result": {"decision": "deliver", "text": oversized}}))
            + ")\n",
        )
        with pytest.raises(PolicyError, match="invalid-response"):
            await evaluate_boundary(engine, True, 10)
