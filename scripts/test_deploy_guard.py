"""Exercise the same jq deletion policy used by deploy.sh."""

import json
import subprocess
import unittest
from pathlib import Path

POLICY = Path(__file__).with_name("deploy_guard.jq")


def blocked(*addresses: str) -> set[str]:
    plan = {
        "resource_changes": [
            {
                "address": address,
                "type": address.split(".", 1)[0],
                "change": {"actions": ["delete"]},
            }
            for address in addresses
        ]
    }
    result = subprocess.run(
        ["jq", "-r", "-f", str(POLICY)],
        input=json.dumps(plan),
        text=True,
        capture_output=True,
        check=True,
    )
    return {line.split(":", 1)[0] for line in result.stdout.splitlines()}


class DeploymentGuardTests(unittest.TestCase):
    def test_existing_phase_four_retirements_are_exact(self) -> None:
        self.assertEqual(
            blocked(
                "aws_cloudwatch_event_rule.session_changed",
                "aws_cloudwatch_event_target.session_changed",
                'aws_lambda_permission.eventbridge_projector["session"]',
                "aws_api_gateway_deployment.private_a2a",
            ),
            set(),
        )

    def test_exact_phase_five_retirements_are_allowed(self) -> None:
        self.assertEqual(
            blocked(
                "aws_api_gateway_deployment.app",
                "aws_api_gateway_integration.stream",
                "aws_api_gateway_method.stream",
                "aws_api_gateway_resource.session",
                "aws_api_gateway_resource.sessions",
                "aws_api_gateway_resource.stream",
                'aws_cloudwatch_log_group.lambda["web-stream"]',
                'aws_iam_role.lambda["web-stream"]',
                'aws_iam_role_policy.lambda_access["web-stream"]',
                'aws_iam_role_policy_attachment.lambda_logs["web-stream"]',
                'aws_lambda_function.app["web-stream"]',
                'aws_lambda_permission.api_gateway["web-stream"]',
            ),
            set(),
        )

    def test_unrelated_core_deletions_remain_blocked(self) -> None:
        candidates = {
            "aws_dsql_cluster.app",
            'aws_lambda_function.app["cma-controller"]',
            'aws_lambda_function.app["unrelated"]',
            "aws_api_gateway_rest_api.private_a2a",
            "aws_iam_role.unrelated",
            "aws_sqs_queue.agent_input",
            'aws_cloudwatch_log_group.lambda["unrelated"]',
        }
        self.assertEqual(blocked(*candidates), candidates)


if __name__ == "__main__":
    unittest.main()
