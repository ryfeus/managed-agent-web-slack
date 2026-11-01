"""Exercise the same jq deletion policy used by deploy.sh."""

import json
import subprocess
import unittest
from pathlib import Path

POLICY = Path(__file__).with_name("deploy_guard.jq")


def blocked(*addresses: str, actions: tuple[str, ...] = ("delete",)) -> set[str]:
    plan = {
        "resource_changes": [
            {
                "address": address,
                "type": address.split(".", 1)[0],
                "change": {"actions": list(actions)},
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
    def test_phase_two_push_maintenance_retirement_allows_only_deletion(self) -> None:
        retired = {
            "aws_cloudwatch_event_rule.cma_push_maintenance",
            "aws_cloudwatch_event_target.cma_push_maintenance",
            "aws_lambda_permission.cma_push_maintenance",
        }
        self.assertEqual(blocked(*retired), set())
        for actions in (("delete", "create"), ("create", "delete")):
            self.assertEqual(blocked(*retired, actions=actions), retired)

    def test_other_schedules_and_push_workers_remain_protected(self) -> None:
        protected = {
            "aws_cloudwatch_event_rule.cma_pending_input_gc",
            "aws_cloudwatch_event_target.cma_pending_input_gc",
            "aws_lambda_permission.cma_pending_input_gc",
            "aws_cloudwatch_event_rule.cma_push_maintenance_other",
            'aws_lambda_function.app["cma-push"]',
            "aws_sqs_queue.cma_push",
        }
        self.assertEqual(blocked(*protected), protected)

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

    def test_snapshot_retirement_never_authorizes_data_or_iam_deletion(self) -> None:
        retained = {
            "aws_s3_bucket.openappa_snapshots",
            "aws_s3_bucket_public_access_block.openappa_snapshots",
            "aws_s3_bucket_server_side_encryption_configuration.openappa_snapshots",
            "aws_s3_bucket_policy.openappa_snapshots",
        }
        self.assertEqual(blocked(*retained, actions=("forget",)), set())
        self.assertEqual(blocked(*retained), retained)
        self.assertEqual(
            blocked("aws_iam_role_policy.openappa_snapshots"),
            {"aws_iam_role_policy.openappa_snapshots"},
        )
        retirement = POLICY.parents[1] / "infra/app/openappa.tf"
        import re

        blocks = re.findall(
            r"removed\s*\{\s*from\s*=\s*(\S+)\s+lifecycle\s*\{\s*destroy\s*=\s*false\s*\}\s*\}",
            retirement.read_text(),
        )
        self.assertEqual(set(blocks), retained)

    def test_unrelated_core_deletions_remain_blocked(self) -> None:
        candidates = {
            "aws_dsql_cluster.app",
            "aws_cloudformation_stack.web_function",
            "aws_appsync_api.preview",
            "aws_subnet.web_private",
            'aws_lambda_function.app["web-api"]',
            'aws_lambda_function.app["agui-bridge"]',
            'aws_s3_object.lambda_artifact["agui-bridge.zip"]',
            "aws_s3_bucket.lambda_artifacts",
            "aws_s3_bucket_versioning.lambda_artifacts",
            'aws_s3_object.lambda_artifact["python-buffered.zip"]',
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
