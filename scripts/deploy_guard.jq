.resource_changes[]?
| select((.change.actions | index("delete")) != null)
# Phase 4 and Phase 5 retirements are reviewed exact addresses. API deployments are immutable snapshots.
| select(.address as $address | [
    "aws_cloudwatch_event_rule.session_changed",
    "aws_cloudwatch_event_target.session_changed",
    "aws_lambda_permission.eventbridge_projector[\"session\"]",
    "aws_api_gateway_deployment.private_a2a",
    "aws_api_gateway_deployment.app",
    "aws_api_gateway_integration.stream",
    "aws_api_gateway_method.stream",
    "aws_api_gateway_resource.session",
    "aws_api_gateway_resource.sessions",
    "aws_api_gateway_resource.stream",
    "aws_cloudwatch_log_group.lambda[\"web-stream\"]",
    "aws_iam_role.lambda[\"web-stream\"]",
    "aws_iam_role_policy.lambda_access[\"web-stream\"]",
    "aws_iam_role_policy_attachment.lambda_logs[\"web-stream\"]",
    "aws_lambda_function.app[\"web-stream\"]",
    "aws_lambda_permission.api_gateway[\"web-stream\"]"
  ] | index($address) == null)
| select(
    (.type | startswith("aws_dsql_"))
    or (.type | startswith("aws_s3_"))
    or (.type | startswith("aws_cloudfront_"))
    or (.type | startswith("aws_api_gateway_"))
    or (.type | startswith("aws_lambda_"))
    or (.type | startswith("aws_sqs_"))
    or (.type | startswith("aws_cloudwatch_event_"))
    or (.type | startswith("aws_secretsmanager_"))
    or (.type | startswith("aws_iam_"))
    or (.type == "aws_cloudwatch_log_group")
  )
| "\(.address): \(.change.actions | join(" -> "))"
