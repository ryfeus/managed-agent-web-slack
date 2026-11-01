# No AWS credentials or network calls: exercise the real graph with provider mocks.
mock_provider "aws" {
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
      length([for function in aws_lambda_function.app : function.s3_key if function.s3_key == "lambda/python-buffered.zip"]) == 7
    )
    error_message = "The seven buffered functions share a ZIP; two independent TypeScript artifacts are added."
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
          contains(["cma-controller", "cma-scheduler"], name) &&
          statement.Resource == "${aws_s3_bucket.cma_pending_input.arn}/pending/*"
        )
      ])
    ])
    error_message = "Temporary review/input writes must stay controller-private and scoped to the pending prefix."
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
