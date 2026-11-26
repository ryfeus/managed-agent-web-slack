variable "mcp_enabled" {
  description = "Provision the personal OAuth MCP gateway. Identity linking and restricted DSQL grants are explicit admin steps."
  type        = bool
  default     = false
}

variable "mcp_claude_callback_urls" {
  type    = list(string)
  default = ["http://localhost:8766/callback"]
}

variable "mcp_codex_callback_urls" {
  description = "Exact callback URL reported by the installed Codex client; configure its listener on port 8767."
  type        = list(string)
  default     = ["http://127.0.0.1:8767/callback"]
}

locals {
  mcp_function_name = "${local.name}-mcp"
  mcp_function_arn  = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:web-function/${local.mcp_function_name}"
  mcp_origin        = coalesce(var.public_app_url, "https://unconfigured.invalid")
  mcp_url           = "${local.mcp_origin}/mcp"
  mcp_callbacks = var.mcp_enabled ? {
    claude = var.mcp_claude_callback_urls
    codex  = var.mcp_codex_callback_urls
  } : {}
}

resource "aws_cognito_user_pool" "mcp" {
  count                    = var.mcp_enabled ? 1 : 0
  name                     = local.mcp_function_name
  deletion_protection      = "ACTIVE"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  admin_create_user_config { allow_admin_create_user_only = true }
  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }
  user_pool_tier = "ESSENTIALS"
}

resource "aws_cognito_user_pool_domain" "mcp" {
  count                 = var.mcp_enabled ? 1 : 0
  domain                = substr("${local.mcp_function_name}-${data.aws_caller_identity.current.account_id}", 0, 63)
  user_pool_id          = aws_cognito_user_pool.mcp[0].id
  managed_login_version = 2
}

resource "aws_cognito_resource_server" "mcp" {
  count        = var.mcp_enabled ? 1 : 0
  user_pool_id = aws_cognito_user_pool.mcp[0].id
  identifier   = local.mcp_url
  name         = local.mcp_function_name
  scope {
    scope_name        = "invoke"
    scope_description = "Use the managed agent within linked thread ownership"
  }
}

resource "aws_cognito_user_pool_client" "mcp" {
  for_each                             = local.mcp_callbacks
  name                                 = "${local.mcp_function_name}-${each.key}"
  user_pool_id                         = aws_cognito_user_pool.mcp[0].id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "${aws_cognito_resource_server.mcp[0].identifier}/invoke"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = each.value
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  enable_token_revocation              = true
  prevent_user_existence_errors        = "ENABLED"
  access_token_validity                = 15
  id_token_validity                    = 15
  refresh_token_validity               = 30
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_managed_login_branding" "mcp" {
  for_each                    = local.mcp_callbacks
  user_pool_id                = aws_cognito_user_pool.mcp[0].id
  client_id                   = aws_cognito_user_pool_client.mcp[each.key].id
  use_cognito_provided_values = true
  depends_on                  = [aws_cognito_user_pool_domain.mcp]
}

resource "aws_iam_role" "mcp_function" {
  count = var.mcp_enabled ? 1 : 0
  name  = local.mcp_function_name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "mcp_logs" {
  count      = var.mcp_enabled ? 1 : 0
  role       = aws_iam_role.mcp_function[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "mcp_function" {
  count = var.mcp_enabled ? 1 : 0
  role  = aws_iam_role.mcp_function[0].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["dsql:DbConnect"], Resource = aws_dsql_cluster.app.arn }]
  })
}

resource "aws_cloudwatch_log_group" "mcp_function" {
  count             = var.mcp_enabled ? 1 : 0
  name              = "/application/${local.mcp_function_name}"
  retention_in_days = 14
}

resource "aws_cloudformation_stack" "mcp_function" {
  count         = var.mcp_enabled ? 1 : 0
  name          = local.mcp_function_name
  template_body = file("${path.module}/mcp-function.yaml")
  capabilities  = ["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"]
  parameters = {
    Name            = local.mcp_function_name
    ArtifactBucket  = aws_s3_object.lambda_artifact["mcp-function.zip"].bucket
    ArtifactKey     = aws_s3_object.lambda_artifact["mcp-function.zip"].key
    ArtifactVersion = aws_s3_object.lambda_artifact["mcp-function.zip"].version_id
    ExecutionRole   = aws_iam_role.mcp_function[0].arn
    LogGroup        = aws_cloudwatch_log_group.mcp_function[0].name
    ConnectorArn    = aws_cloudformation_stack.web_function.outputs["ConnectorVersionArn"]
    DsqlEndpoint    = local.dsql_endpoint
    A2aEndpoint     = local.private_a2a_url
    PublicOrigin    = local.mcp_origin
    OAuthIssuer     = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.mcp[0].id}"
    ClientIds       = join(",", [for client in aws_cognito_user_pool_client.mcp : client.id])
  }
  depends_on = [aws_iam_role_policy.mcp_function]
  lifecycle {
    precondition {
      condition     = var.aws_region == "us-west-2" && can(regex("^https://[^/]+$", var.public_app_url))
      error_message = "MCP requires us-west-2 and the existing application's exact HTTPS public_app_url."
    }
  }
}

resource "aws_cloudformation_stack" "mcp_function_policy" {
  count         = var.mcp_enabled ? 1 : 0
  name          = "${local.mcp_function_name}-policy"
  template_body = file("${path.module}/web-function-policy.yaml")
  parameters = {
    FunctionArn     = aws_cloudformation_stack.mcp_function[0].outputs["FunctionArn"]
    EndpointArn     = aws_cloudformation_stack.mcp_function[0].outputs["EndpointArn"]
    DistributionArn = aws_cloudfront_distribution.app.arn
  }
}

# Authorization must be in the cache policy even for GET. no-store responses and
# zero minimum/default TTL prevent authenticated response caching.
resource "aws_cloudfront_cache_policy" "mcp" {
  count       = var.mcp_enabled ? 1 : 0
  name        = local.mcp_function_name
  min_ttl     = 0
  default_ttl = 0
  max_ttl     = 1
  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config { cookie_behavior = "none" }
    headers_config {
      header_behavior = "whitelist"
      headers { items = ["Authorization"] }
    }
    query_strings_config { query_string_behavior = "none" }
  }
}

output "mcp_url" { value = var.mcp_enabled ? local.mcp_url : null }
output "mcp_user_pool_id" { value = var.mcp_enabled ? aws_cognito_user_pool.mcp[0].id : null }
output "mcp_oauth_issuer" { value = var.mcp_enabled ? "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.mcp[0].id}" : null }
output "mcp_client_ids" { value = { for name, client in aws_cognito_user_pool_client.mcp : name => client.id } }
output "mcp_runtime_role_arn" {
  value       = var.mcp_enabled ? aws_iam_role.mcp_function[0].arn : null
  description = "Bind only to mcp_runtime with the explicit mcp-admin grant-role command, never app_runtime."
}
