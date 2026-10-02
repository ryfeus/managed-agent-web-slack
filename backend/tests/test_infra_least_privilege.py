from pathlib import Path

from managed_agents_app.domain import SLACK_THREAD_STOP_REQUESTED

MAIN = (Path(__file__).resolve().parents[2] / "infra/app/main.tf").read_text()


def _section(start: str, end: str) -> str:
    return MAIN.split(start, 1)[1].split(end, 1)[0]


def test_surface_lambdas_cannot_read_anthropic_secret() -> None:
    grants = _section("  secret_access = {", "  common_environment = {")
    for name in ("web-api", "agui-bridge", "slack-ingress", "agent-input", "slack-projector"):
        line = next(value for value in grants.splitlines() if value.strip().startswith(f"{name} "))
        assert "anthropic_api_key" not in line
    for name in ("cma-scheduler", "cma-push", "cma-controller"):
        line = next(value for value in grants.splitlines() if value.strip().startswith(f"{name} "))
        assert "anthropic_api_key" in line


def test_provider_configuration_stays_in_controller_environment() -> None:
    common = _section("  common_environment = {", "  controller_environment = merge(")
    controller = _section("  controller_environment = merge(", "  function_environment = {")
    for name in (
        "CLAUDE_AGENT_ID",
        "CLAUDE_AGENT_VERSION",
        "CLAUDE_ENVIRONMENT_ID",
        "CMA_SCHEDULER_QUEUE_URL",
        "CMA_PUSH_QUEUE_URL",
        "CMA_PENDING_INPUT_BUCKET",
    ):
        assert name not in common
        assert name in controller


def test_slack_stop_eventbridge_rule_matches_the_domain_event() -> None:
    action_rule = _section(
        'resource "aws_cloudwatch_event_rule" "slack_action" {',
        'resource "aws_cloudwatch_event_rule" "control_reply" {',
    )
    assert f'"{SLACK_THREAD_STOP_REQUESTED}"' in action_rule
    assert '"SlackSessionStopRequested"' not in action_rule
