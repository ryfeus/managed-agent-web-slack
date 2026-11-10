# No AWS credentials or network calls: exercise the real graph with provider mocks.
mock_provider "aws" {
  mock_resource "aws_subnet" {
    override_during = plan
  }
  mock_resource "aws_route_table" {
    override_during = plan
    defaults        = { id = "rtb-fixture" }
  }
  mock_resource "aws_nat_gateway" {
    override_during = plan
    defaults        = { id = "nat-fixture" }
  }
  # Resolve the dependencies of the generated IAM policy during the mock plan.
  mock_resource "aws_cloudformation_stack" {
    override_during = plan
    defaults        = { outputs = { FunctionArn = "arn:aws:lambda:us-west-2:000000000000:web-function/fixture", EndpointArn = "arn:aws:lambda:us-west-2:000000000000:web-function/fixture/endpoint/prod", EndpointDomain = "fixture.lambda-web.us-west-2.on.aws", RevisionId = "rev-fixture", ConnectorVersionArn = "arn:aws:lambda:us-west-2:000000000000:network-connector:fixture:1", OacId = "fixture" } }
  }
  mock_resource "aws_appsync_api" {
    override_during = plan
    defaults        = { api_arn = "arn:aws:appsync:us-west-2:000000000000:apis/fixture", dns = { HTTP = "fixture.appsync-api.us-west-2.amazonaws.com", REALTIME = "fixture.appsync-realtime-api.us-west-2.amazonaws.com" } }
  }
  mock_resource "aws_appsync_channel_namespace" {
    override_during = plan
    defaults        = { channel_namespace_arn = "arn:aws:appsync:us-west-2:000000000000:apis/fixture/channelNamespace/preview" }
  }
  mock_resource "aws_dsql_cluster" {
    override_during = plan
    defaults        = { arn = "arn:aws:dsql:us-west-2:000000000000:cluster/fixture" }
  }
  mock_resource "aws_cloudwatch_event_bus" {
    override_during = plan
    defaults        = { arn = "arn:aws:events:us-west-2:000000000000:event-bus/fixture" }
  }
  mock_resource "aws_sqs_queue" {
    override_during = plan
    defaults        = { arn = "arn:aws:sqs:us-west-2:000000000000:fixture" }
  }
  mock_resource "aws_secretsmanager_secret" {
    override_during = plan
    defaults        = { arn = "arn:aws:secretsmanager:us-west-2:000000000000:secret:fixture-ABCDEF" }
  }
  mock_resource "aws_s3_bucket" {
    override_during = plan
  }
  mock_resource "aws_s3_object" {
    override_during = plan
    defaults        = { version_id = "fixture-object-version" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-west-2a", "us-west-2b"] }
  }
}

variables {
  claude_agent_id       = "agent_fixture"
  claude_agent_version  = 1
  claude_environment_id = "env_fixture"
}

run "shared_versioned_artifacts" {
  command = plan

  assert {
    condition     = !contains(keys(aws_lambda_function.app["cma-scheduler"].environment[0].variables), "OPENAPPA_SNAPSHOT_BUCKET")
    error_message = "Stateless policy evaluation must not depend on a snapshot bucket."
  }

  assert {
    condition = (
      length(aws_s3_object.lambda_artifact) == 6 &&
      length([for function in aws_lambda_function.app : function.s3_key if function.s3_key == "lambda/python-buffered.zip"]) == 6
    )
    error_message = "The six Python buffered functions share a ZIP; three independent TypeScript artifacts are added."
  }

  assert {
    condition = alltrue([for name, function in aws_lambda_function.app :
      function.filename == null &&
      function.s3_bucket == aws_s3_bucket.lambda_artifacts.id &&
      function.s3_key == "lambda/${local.lambda_artifacts[name]}" &&
      function.s3_object_version == "fixture-object-version" &&
      function.source_code_hash == filebase64sha256("${path.module}/../../dist/lambdas/${local.lambda_artifacts[name]}")
    ])
    error_message = "Every function must select its exact versioned S3 package with the correct code hash."
  }

  assert {
    condition = alltrue([for name, artifact in aws_s3_object.lambda_artifact :
      artifact.source_hash == filesha256("${path.module}/../../dist/lambdas/${name}")
    ])
    error_message = "Object uploads must track ZIP content changes, independent of multipart ETags."
  }

  assert {
    condition = (
      aws_s3_bucket.lambda_artifacts.force_destroy == false &&
      aws_s3_bucket_public_access_block.lambda_artifacts.block_public_acls &&
      aws_s3_bucket_public_access_block.lambda_artifacts.block_public_policy &&
      aws_s3_bucket_public_access_block.lambda_artifacts.ignore_public_acls &&
      aws_s3_bucket_public_access_block.lambda_artifacts.restrict_public_buckets &&
      aws_s3_bucket_versioning.lambda_artifacts.versioning_configuration[0].status == "Enabled" &&
      one(aws_s3_bucket_server_side_encryption_configuration.lambda_artifacts.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256" &&
      jsondecode(aws_s3_bucket_policy.lambda_artifacts.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    )
    error_message = "Deployment artifacts must be private, encrypted, versioned and require TLS."
  }
}

run "slack_preview_isolation" {
  command = plan
  assert {
    condition = (
      !var.slack_preview_enabled && !aws_lambda_event_source_mapping.slack_preview.enabled &&
      aws_lambda_event_source_mapping.slack_preview.batch_size == 1 &&
      aws_lambda_event_source_mapping.slack_preview.scaling_config[0].maximum_concurrency == 4 &&
      aws_sqs_queue.slack_preview.fifo_queue && aws_sqs_queue.slack_preview.sqs_managed_sse_enabled &&
      aws_sqs_queue.slack_preview.message_retention_seconds == 60 &&
      aws_sqs_queue.slack_preview.visibility_timeout_seconds >= 6 * aws_lambda_function.slack_preview.timeout &&
      aws_lambda_function.slack_preview.runtime == "nodejs24.x" &&
      tolist(aws_lambda_function.slack_preview.architectures) == tolist(["arm64"]) &&
      aws_lambda_function.slack_preview.reserved_concurrent_executions == 4 &&
      aws_lambda_function.slack_preview.s3_key == "lambda/slack-preview.zip" &&
      aws_lambda_function.slack_preview.s3_object_version == "fixture-object-version" &&
      aws_lambda_function.slack_preview.vpc_config[0].subnet_ids == toset([aws_subnet.a2a_private.id, aws_subnet.web_private.id]) &&
      aws_dsql_cluster.app.deletion_protection_enabled
    )
    error_message = "Slack preview must default off, use bounded FIFO concurrency, existing private network/versioned artifacts, and preserve DSQL."
  }
  assert {
    condition = (
      toset(flatten([for s in jsondecode(aws_iam_role_policy.slack_preview.policy).Statement : s.Action])) == toset([
        "appsync:EventConnect", "appsync:EventSubscribe", "dsql:DbConnect", "secretsmanager:GetSecretValue",
        "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"
      ]) &&
      alltrue([for s in jsondecode(aws_iam_role_policy.slack_preview.policy).Statement :
        !contains(s.Action, "secretsmanager:GetSecretValue") || s.Resource == aws_secretsmanager_secret.slack_bot_token.arn
      ]) &&
      !contains(keys(aws_lambda_function.slack_preview.environment[0].variables), "ANTHROPIC_API_KEY_SECRET_ARN") &&
      !contains(keys(aws_lambda_function.slack_preview.environment[0].variables), "CMA_A2A_ENDPOINT") &&
      jsondecode(aws_iam_role_policy.slack_preview_start.policy).Statement[0].Action == ["sqs:SendMessage"] &&
      jsondecode(aws_iam_role_policy.slack_preview_start.policy).Statement[0].Resource == aws_sqs_queue.slack_preview.arn
    )
    error_message = "Slack subscriber may read only the bot secret and subscribe, never publish, read CMA credentials or mutate tasks."
  }
}

run "slack_preview_requires_producer" {
  command = plan
  variables { slack_preview_enabled = true }
  assert {
    condition     = !aws_lambda_event_source_mapping.slack_preview.enabled
    error_message = "Enabling only the Slack consumer cannot start subscriptions."
  }
}

run "slack_preview_explicit_enable" {
  command = plan
  variables {
    slack_preview_enabled = true
    preview_enabled       = true
  }
  assert {
    condition     = aws_lambda_event_source_mapping.slack_preview.enabled
    error_message = "Both explicit P1 flags enable the separate Slack mapping."
  }
}

run "human_review_permissions" {
  command = plan

  assert {
    condition = length([for statement in jsondecode(aws_iam_role_policy.lambda_access["cma-scheduler"].policy).Statement : statement
      if contains(statement.Action, "s3:PutObject") && statement.Resource == "${aws_s3_bucket.cma_pending_input.arn}/pending/*"
    ]) == 1
    error_message = "The scheduler must publish human-review descriptions to the existing private pending-input prefix."
  }

  assert {
    condition = alltrue([for name, policy in aws_iam_role_policy.lambda_access :
      alltrue([for statement in jsondecode(policy.policy).Statement :
        !contains(statement.Action, "s3:PutObject") || (
          (contains(["cma-controller", "cma-scheduler"], name) &&
          statement.Resource == "${aws_s3_bucket.cma_pending_input.arn}/pending/*") ||
          (name == "cma-scheduler" && statement.Resource == "${aws_s3_bucket.cma_pending_input.arn}/policy/*")
        )
      ])
    ])
    error_message = "Temporary input writes stay controller-private; only the scheduler can write policy checkpoints."
  }
  assert {
    condition = alltrue([for name, policy in aws_iam_role_policy.lambda_access :
      alltrue([for statement in jsondecode(policy.policy).Statement :
        statement.Resource != "${aws_s3_bucket.cma_pending_input.arn}/policy/*" || name == "cma-scheduler"
      ])
    ])
    error_message = "Native policy snapshots are scheduler-private."
  }

}

run "web_function_network_and_trust_contract" {
  command = plan
  variables {
    web_function_rollback_revision = "rev-reviewed-rollback"
  }
  assert {
    condition = (
      aws_subnet.web_private.availability_zone != aws_subnet.a2a_private.availability_zone &&
      aws_route_table_association.web_private.route_table_id == aws_route_table.a2a_private.id &&
      aws_route.a2a_private_default.nat_gateway_id == aws_nat_gateway.a2a.id &&
      aws_vpc_endpoint.a2a_execute_api.private_dns_enabled &&
      aws_vpc_security_group_egress_rule.a2a_callers_dsql.to_port == 5432 &&
      aws_vpc_security_group_ingress_rule.a2a_endpoint_https.from_port == 443
    )
    error_message = "Both connector AZs must use the existing NAT route and private A2A/DSQL network boundary."
  }
  assert {
    condition = (
      strcontains(aws_cloudformation_stack.web_function.template_body, "ComputeTypes: [WebFunction]") &&
      strcontains(aws_cloudformation_stack.web_function.template_body, "EgressNetworkConnectorArn: !GetAtt Connector.LatestVersionArn") &&
      strcontains(aws_cloudformation_stack.web_function.template_body, "EndpointType: HomeRegion") &&
      strcontains(aws_cloudformation_stack.web_function.template_body, "AuthType: IamAuth") &&
      strcontains(aws_cloudformation_stack.web_function.template_body, "SigningBehavior: always-amz-auth") &&
      strcontains(aws_cloudformation_stack.web_function.template_body, "SigningProtocol: sigv4") &&
      strcontains(aws_cloudformation_stack.web_function.template_body, "UpdateReplacePolicy: Retain") &&
      strcontains(aws_cloudformation_stack.web_function.template_body, "\"rev-reviewed-rollback\": 100") &&
      strcontains(aws_cloudformation_stack.web_function_policy.template_body, "aws:SourceArn: !Ref DistributionArn")
    )
    error_message = "Web Function requires a version-qualified HomeRegion connector, IAM/OAC, and retained-revision rollback."
  }
  assert {
    condition = alltrue([for behavior in aws_cloudfront_distribution.app.ordered_cache_behavior :
      behavior.target_origin_id == (behavior.path_pattern == "api/*" ? "web-function" : "api-gateway") &&
      behavior.cache_policy_id == data.aws_cloudfront_cache_policy.disabled.id &&
      behavior.origin_request_policy_id == data.aws_cloudfront_origin_request_policy.all_except_host.id &&
      !behavior.compress
    ])
    error_message = "Only /api/* moves; API cookies must survive, Host must not, and SSE must bypass caching/compression."
  }
  assert {
    condition = (
      !var.preview_enabled &&
      aws_lambda_event_source_mapping.cma_preview.enabled == false &&
      alltrue([for s in jsondecode(aws_iam_role_policy.web_function.policy).Statement :
        !contains(s.Action, "appsync:EventPublish") && !contains(s.Action, "sqs:SendMessage")
      ]) &&
      alltrue([for s in jsondecode(aws_iam_role_policy.cma_preview.policy).Statement :
        !contains(s.Action, "appsync:EventSubscribe") && !contains(s.Action, "dsql:DbConnect")
      ]) &&
      aws_appsync_api.preview.event_config[0].auth_provider[0].auth_type == "AWS_IAM" &&
      aws_appsync_channel_namespace.preview.name == "preview"
    )
    error_message = "P1 is opt-in, with distinct publisher/subscriber roles, IAM-only channels and no observer DB writes."
  }
}
