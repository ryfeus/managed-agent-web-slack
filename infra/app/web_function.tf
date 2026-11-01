locals {
  web_function_name = "${local.name}-web"
  web_function_arn  = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:web-function/${local.web_function_name}"
}

variable "web_function_rollback_revision" {
  description = "Optional retained immutable revision ID. Null deploys the current artifact; set only in a reviewed rollback plan."
  type        = string
  default     = null
  validation {
    condition     = var.web_function_rollback_revision == null ? true : can(regex("^[A-Za-z0-9-]{1,128}$", var.web_function_rollback_revision))
    error_message = "Rollback must reference an existing immutable revision ID."
  }
}

resource "aws_iam_role" "web_function" {
  name = local.web_function_name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "web_function_logs" {
  role       = aws_iam_role.web_function.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "web_function" {
  role = aws_iam_role.web_function.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dsql:DbConnect"], Resource = aws_dsql_cluster.app.arn },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_secretsmanager_secret.web_access_token.arn, aws_secretsmanager_secret.web_cookie_secret.arn] },
      { Effect = "Allow", Action = ["appsync:EventConnect"], Resource = aws_appsync_api.preview.api_arn },
      { Effect = "Allow", Action = ["appsync:EventSubscribe"], Resource = aws_appsync_channel_namespace.preview.channel_namespace_arn }
    ]
  })
}

resource "aws_cloudwatch_log_group" "web_function" {
  name              = "/application/${local.web_function_name}"
  retention_in_days = 14
}

# Provider 6.62 has neither Web Function resources nor always-amz-auth OAC support.
# SAM owns only the resources missing from Terraform, under this managed stack.
resource "aws_cloudformation_stack" "web_function" {
  name = local.web_function_name
  template_body = var.web_function_rollback_revision == null ? file("${path.module}/web-function.yaml") : replace(
    file("${path.module}/web-function.yaml"), "AutoDeploymentMode: LatestRevision",
    "AutoDeploymentMode: Disabled\n      RevisionWeights:\n        ${jsonencode(var.web_function_rollback_revision)}: 100"
  )
  capabilities = ["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"]
  parameters = {
    Name                = local.web_function_name
    ArtifactBucket      = aws_s3_object.lambda_artifact["web-function.zip"].bucket
    ArtifactKey         = aws_s3_object.lambda_artifact["web-function.zip"].key
    ArtifactVersion     = aws_s3_object.lambda_artifact["web-function.zip"].version_id
    ExecutionRole       = aws_iam_role.web_function.arn
    LogGroup            = aws_cloudwatch_log_group.web_function.name
    SubnetA             = aws_subnet.a2a_private.id
    SubnetB             = aws_subnet.web_private.id
    SecurityGroup       = aws_security_group.a2a_callers.id
    DsqlEndpoint        = local.dsql_endpoint
    A2aEndpoint         = local.private_a2a_url
    PublicOrigin        = coalesce(var.public_app_url, "https://unconfigured.invalid")
    AccessTokenSecret   = aws_secretsmanager_secret.web_access_token.arn
    CookieSecret        = aws_secretsmanager_secret.web_cookie_secret.arn
    PreviewEnabled      = tostring(var.preview_enabled)
    AppSyncHttpUrl      = local.preview_environment.APPSYNC_HTTP_URL
    AppSyncWebsocketUrl = local.preview_environment.APPSYNC_WEBSOCKET_URL
  }
  depends_on = [aws_route.a2a_private_default, aws_route_table_association.web_private, aws_iam_role_policy.web_function]
  lifecycle {
    precondition {
      condition     = var.aws_region == "us-west-2"
      error_message = "This application's HomeRegion Web Function is pinned to us-west-2."
    }
  }
}

resource "aws_cloudformation_stack" "web_function_policy" {
  name          = "${local.web_function_name}-policy"
  template_body = file("${path.module}/web-function-policy.yaml")
  parameters = {
    FunctionArn     = aws_cloudformation_stack.web_function.outputs["FunctionArn"]
    EndpointArn     = aws_cloudformation_stack.web_function.outputs["EndpointArn"]
    DistributionArn = aws_cloudfront_distribution.app.arn
  }
}

output "web_function_url" {
  value       = "https://${aws_cloudformation_stack.web_function.outputs["EndpointDomain"]}"
  description = "IAM-protected standalone endpoint; browser access uses CloudFront."
}
output "web_function_revision_id" {
  value = aws_cloudformation_stack.web_function.outputs["RevisionId"]
}
output "web_function_connector_version_arn" {
  value = aws_cloudformation_stack.web_function.outputs["ConnectorVersionArn"]
}
