variable "slack_preview_enabled" {
  description = "Opt in to disposable Slack task-card titles; requires preview_enabled."
  type        = bool
  default     = false
}

resource "aws_sqs_queue" "slack_preview" {
  name                        = "${local.name}-slack-preview.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  message_retention_seconds   = 60
  visibility_timeout_seconds  = 780
  sqs_managed_sse_enabled     = true
}

resource "aws_lambda_event_source_mapping" "slack_preview" {
  event_source_arn = aws_sqs_queue.slack_preview.arn
  function_name    = aws_lambda_function.slack_preview.arn
  enabled          = var.preview_enabled && var.slack_preview_enabled
  batch_size       = 1
  scaling_config { maximum_concurrency = 4 }
}

resource "aws_iam_role" "slack_preview" {
  name = "${local.name}-slack-preview"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "slack_preview_vpc" {
  role       = aws_iam_role.slack_preview.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "slack_preview" {
  role = aws_iam_role.slack_preview.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["appsync:EventConnect"], Resource = aws_appsync_api.preview.api_arn },
      { Effect = "Allow", Action = ["appsync:EventSubscribe"], Resource = aws_appsync_channel_namespace.preview.channel_namespace_arn },
      { Effect = "Allow", Action = ["dsql:DbConnect"], Resource = aws_dsql_cluster.app.arn },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.slack_bot_token.arn },
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.slack_preview.arn }
    ]
  })
}

resource "aws_iam_role_policy" "slack_preview_start" {
  role = aws_iam_role.lambda["slack-projector"].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.slack_preview.arn }]
  })
}

resource "aws_cloudwatch_log_group" "slack_preview" {
  name              = "/aws/lambda/${local.name}-slack-preview"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_metric_filter" "slack_preview" {
  for_each       = toset(["started", "stopped", "update", "claim_skipped", "rate_limited", "error", "unavailable", "subscription_error"])
  name           = "slack-preview-${each.key}"
  log_group_name = aws_cloudwatch_log_group.slack_preview.name
  pattern        = "{ $.event = \"slack_preview_${each.key}\" }"
  metric_transformation {
    name      = "SlackPreview-${each.key}"
    namespace = local.name
    value     = "1"
    unit      = "Count"
  }
  # Active concurrency is also available as Lambda's native ConcurrentExecutions metric.
}

resource "aws_lambda_function" "slack_preview" {
  function_name                  = "${local.name}-slack-preview"
  role                           = aws_iam_role.slack_preview.arn
  runtime                        = "nodejs24.x"
  handler                        = "index.handler"
  architectures                  = ["arm64"]
  s3_bucket                      = aws_s3_object.lambda_artifact["slack-preview.zip"].bucket
  s3_key                         = aws_s3_object.lambda_artifact["slack-preview.zip"].key
  s3_object_version              = aws_s3_object.lambda_artifact["slack-preview.zip"].version_id
  source_code_hash               = filebase64sha256("${path.module}/../../dist/lambdas/slack-preview.zip")
  timeout                        = 130
  memory_size                    = 256
  reserved_concurrent_executions = 4
  vpc_config {
    subnet_ids         = [aws_subnet.a2a_private.id, aws_subnet.web_private.id]
    security_group_ids = [aws_security_group.a2a_callers.id]
  }
  environment {
    variables = merge(local.preview_environment, {
      APP_ENV                    = "production"
      SLACK_PREVIEW_ENABLED      = tostring(var.slack_preview_enabled)
      SLACK_BOT_TOKEN_SECRET_ARN = aws_secretsmanager_secret.slack_bot_token.arn
      DSQL_ENDPOINT              = local.dsql_endpoint
      DSQL_DATABASE              = "postgres"
      DSQL_ROLE                  = "app_runtime"
    })
  }
  depends_on = [aws_cloudwatch_log_group.slack_preview, aws_iam_role_policy_attachment.slack_preview_vpc]
}
