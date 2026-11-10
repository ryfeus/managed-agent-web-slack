"""Real native hooks, fresh helper and restored standalone SQLite at every boundary."""

from dataclasses import replace
from pathlib import Path

import pytest

from managed_agents_app.cma_controller.custom_tools import DEMO_REPORT, default_custom_tools
from managed_agents_app.policy.models import PolicyError, PolicyState, ToolPolicyRequest
from managed_agents_app.policy.openappa import OpenAppaPolicyEngine
from managed_agents_app.policy.snapshots import FileSnapshotStore

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "backend/src/managed_agents_app/policy/appa.toml"


class Demo:
    def __init__(self, directory, policy=POLICY):
        self.engine = OpenAppaPolicyEngine(
            str(ROOT / ".generated/openappa-bin/openappa-cma"),
            str(policy),
            custom_tools=default_custom_tools().names,
            snapshots=FileSnapshotStore(directory),
        )
        self.state = PolicyState()
        self.sequence = 0

    def advance(self, decision):
        checkpoint = decision.checkpoint
        self.state = PolicyState(checkpoint.key, checkpoint.expected_revision + 1, checkpoint.policy_digest)

    async def call(self, name, arguments=None):
        self.sequence += 1
        request = ToolPolicyRequest(
            "root", str(self.sequence), "demo", "custom", name, arguments or {}, state=self.state
        )
        decision = await self.engine.evaluate_tool_call(request, 20)
        self.advance(decision)
        return request, decision

    async def resolve(self, request, decision, ruling="allow"):
        result = await self.engine.resolve_tool_call(
            replace(request, state=self.state), decision.review, ruling, 20
        )
        self.advance(result)
        return result

    async def result(self, request, decision, text="Synthetic result", is_error=False):
        result = await self.engine.evaluate_tool_result(
            replace(request, state=self.state), decision.authorization, text, is_error, 20
        )
        self.advance(result)
        return result

    async def prepare(self):
        request, decision = await self.call("demo_prepare_release")
        assert decision.decision == "allow"
        await self.result(request, decision)


@pytest.mark.asyncio
async def test_redaction_exact_bytes_and_effect_history_across_reopening(tmp_path):
    demo = Demo(tmp_path)
    request, allowed = await demo.call("demo_read_contact_report")
    assert allowed.decision == "allow"
    result = await demo.result(request, allowed, DEMO_REPORT)
    assert result.text == DEMO_REPORT.replace("alex@example.com", "[redacted-email]")
    # Receipt replay returns the same admitted bytes, without rerunning a sanitizer.
    replay = await demo.result(request, allowed, DEMO_REPORT)
    assert replay.text == result.text
    await demo.prepare()
    request, review = await demo.call("demo_publish_report", {"audience": "public", "text": result.text})
    assert review.decision == "human_review" and "effects" not in review.review.description
    allowed = await demo.resolve(request, review)
    await demo.result(request, allowed)
    request, repeat = await demo.call("demo_publish_report", {"audience": "public", "text": result.text})
    assert repeat.decision == "human_review"
    assert "demo.report_published" in repeat.review.description
    assert repeat.review.fingerprint != review.review.fingerprint
    assert (await demo.resolve(request, repeat, "deny")).decision == "deny"
    # Native human denials persist for the exact rendered call, across invocations.
    _, denied_again = await demo.call("demo_publish_report", {"audience": "public", "text": result.text})
    assert denied_again.decision == "deny"
    request, changed = await demo.call(
        "demo_publish_report", {"audience": "public", "text": "Synthetic follow-up announcement"}
    )
    assert changed.decision == "human_review"
    assert "demo.report_published" in changed.review.description
    allowed = await demo.resolve(request, changed)
    assert (await demo.result(request, allowed)).decision == "deliver"


@pytest.mark.asyncio
async def test_private_state_and_missing_prerequisite_cannot_be_waived(tmp_path):
    demo = Demo(tmp_path)
    _, missing = await demo.call("demo_publish_report", {"audience": "public", "text": "example"})
    assert missing.decision == "deny"
    request, review = await demo.call("demo_read_private_report")
    assert review.decision == "human_review"
    stale = replace(review.review, fingerprint="0" * 64)
    with pytest.raises(PolicyError, match="stale-review"):
        await demo.engine.resolve_tool_call(replace(request, state=demo.state), stale, "allow", 20)
    allowed = await demo.resolve(request, review)
    assert (await demo.result(request, allowed, DEMO_REPORT)).text == DEMO_REPORT
    await demo.prepare()
    _, blocked = await demo.call("demo_publish_report", {"audience": "public", "text": "example"})
    assert blocked.decision == "deny"
    _, blocked = await demo.call("demo_read_contact_report")
    assert blocked.decision == "deny"
    request, review = await demo.call("demo_publish_report", {"audience": "internal", "text": "example"})
    assert review.decision == "human_review"
    allowed = await demo.resolve(request, review)
    await demo.result(request, allowed)
    request, repeat = await demo.call("demo_publish_report", {"audience": "internal", "text": "example"})
    assert "demo.report_published" in repeat.review.description
    allowed = await demo.resolve(request, repeat)
    assert (await demo.result(request, allowed)).decision == "deliver"


@pytest.mark.asyncio
async def test_failed_preparation_does_not_record_success_and_unknown_outcome_stays_pending(tmp_path):
    demo = Demo(tmp_path)
    request, allowed = await demo.call("demo_prepare_release")
    await demo.result(request, allowed, "The custom tool failed.", True)
    _, denied = await demo.call("demo_publish_report", {"audience": "public", "text": "example"})
    assert denied.decision == "deny"
    await demo.prepare()
    request, review = await demo.call("demo_publish_report", {"audience": "public", "text": "example"})
    await demo.resolve(request, review)  # No result: native reservation survives reopening.
    _, pending = await demo.call("demo_publish_report", {"audience": "public", "text": "example"})
    assert "demo.report_published" in pending.review.description


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["input", "external", "ambiguous"])
async def test_unsupported_or_ambiguous_sanitizers_hold(tmp_path, change):
    policy = POLICY.read_text()
    if change == "input":
        policy = policy.replace('on = ["tool_output"]', 'on = ["tool_input"]')
    elif change == "external":
        policy = policy.replace('builtin = "redact-email"', 'command = ["false"]')
    else:
        start = policy.index("[[policy.sanitizer]]")
        end = policy.index("[[policy.tool]]", start)
        policy += policy[start:end].replace("demo-redact-email", "second-redactor")
    path = tmp_path / "policy.toml"
    path.write_text(policy)
    with pytest.raises(PolicyError):
        await Demo(tmp_path / "objects", path).call("demo_read_contact_report")


@pytest.mark.asyncio
async def test_native_secret_output_sanitizer_and_fixed_failures(tmp_path):
    path = tmp_path / "secrets.toml"
    path.write_text(POLICY.read_text().replace('builtin = "redact-email"', 'builtin = "redact-secrets"'))
    demo = Demo(tmp_path / "objects", path)
    request, allowed = await demo.call("demo_read_contact_report")
    result = await demo.result(request, allowed, "password=" + "synthetic-demo-value")
    assert result.text == "password=[redacted-secret]"
    request, allowed = await demo.call("demo_read_contact_report")
    failed = await demo.result(request, allowed, "The custom tool failed.", True)
    assert failed.text == "The custom tool failed."


@pytest.mark.asyncio
async def test_output_limit_failure_withholds_without_raw_fallback(tmp_path):
    path = tmp_path / "bounded.toml"
    path.write_text(POLICY.read_text().replace("max_body_bytes = 65536", "max_body_bytes = 16"))
    demo = Demo(tmp_path / "objects", path)
    request, allowed = await demo.call("demo_read_contact_report")
    result = await demo.result(request, allowed, DEMO_REPORT)
    assert result.decision == "withhold" and result.text is None


@pytest.mark.asyncio
async def test_changed_call_cannot_reuse_a_stored_human_offer(tmp_path):
    demo = Demo(tmp_path)
    await demo.prepare()
    request, first = await demo.call("demo_publish_report", {"audience": "public", "text": "first"})
    changed = replace(request, arguments={"audience": "public", "text": "changed"}, state=demo.state)
    second = await demo.engine.evaluate_tool_call(changed, 20)
    demo.advance(second)
    assert second.review.fingerprint != first.review.fingerprint
    with pytest.raises(PolicyError, match="stale-review"):
        await demo.engine.resolve_tool_call(replace(changed, state=demo.state), first.review, "allow", 20)
    assert (await demo.resolve(changed, second)).decision == "allow"
