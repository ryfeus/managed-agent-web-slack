from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from managed_agents_app.policy.models import PolicyError, ToolPolicyRequest
from managed_agents_app.policy.openappa import OpenAppaPolicyEngine

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "backend/src/managed_agents_app/policy/appa.toml"
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "{}",
        "[]",
        "not json",
        '{"version":1,"result":{"decision":"allow","reason":null}}',
        '{"version":3,"result":{"decision":"surprise","reason":null}}',
        '{"version":3,"result":{"decision":"allow","reason":null},"extra":true}',
        '{"version":3,"result":{"decision":"allow","reason":null,"extra":true}}',
        '{"version":3,"error":"invalid_policy","result":{"decision":"allow","reason":null}}',
        "x" * 65537,
    ],
)
async def test_invalid_native_response_fails_closed(tmp_path, response):
    engine = helper(tmp_path, "print(" + repr(response) + ")\n")
    with pytest.raises(PolicyError):
        await engine.evaluate_tool_call(REQUEST, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_valid_native_results_do_not_expose_arbitrary_reason(tmp_path, decision):
    engine = helper(
        tmp_path,
        "print("
        + repr(json.dumps({"version": 3, "result": {"decision": decision, "reason": "private"}}))
        + ")\n",
    )
    result = await engine.evaluate_tool_call(REQUEST, 10)
    assert result.decision == decision and "private" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancellation_reaps_helper_without_exposing_arguments(
    tmp_path, monkeypatch, caplog, cancel
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
    task = asyncio.create_task(engine.evaluate_tool_call(REQUEST, 10 if cancel else 0.05))
    await started.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else PolicyError):
        await task
    assert processes[0].returncode is not None
    assert "secret" not in caplog.text


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
    # CMA owns result delivery; no result mediation or cross-call history.
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
@pytest.mark.parametrize("extra", [{"version": 1}, {"database": "/tmp/obsolete.db"}, {"extra": True}])
async def test_native_protocol_rejects_old_version_and_unknown_fields(extra):
    from dataclasses import asdict

    process = await asyncio.create_subprocess_exec(
        str(BINARY), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )
    body = {"version": 3, "policy": POLICY.read_text(), "request": asdict(REQUEST), **extra}
    stdout, _ = await process.communicate(json.dumps(body).encode())
    assert json.loads(stdout) == {"version": 3, "error": "invalid_request"}


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
