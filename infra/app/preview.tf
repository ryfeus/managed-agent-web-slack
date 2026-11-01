variable "preview_enabled" {
  description = "Enable disposable CMA previews only after explicit live acceptance. P0 never depends on this flag."
  type        = bool
  default     = false
}

resource "aws_appsync_api" "preview" {
  name = "${local.name}-preview"
  event_config {
    auth_provider { auth_type = "AWS_IAM" }
    connection_auth_mode { auth_type = "AWS_IAM" }
    default_publish_auth_mode { auth_type = "AWS_IAM" }
    default_subscribe_auth_mode { auth_type = "AWS_IAM" }
    # No event-body logging. Workloads emit metadata counters only.
  }
}

resource "aws_appsync_channel_namespace" "preview" {
  api_id = aws_appsync_api.preview.api_id
  name   = "preview"
  publish_auth_mode { auth_type = "AWS_IAM" }
  subscribe_auth_mode { auth_type = "AWS_IAM" }
}

locals {
  preview_environment = {
    PREVIEW_ENABLED       = tostring(var.preview_enabled)
    APPSYNC_HTTP_URL      = "https://${aws_appsync_api.preview.dns["HTTP"]}/event"
    APPSYNC_WEBSOCKET_URL = "wss://${aws_appsync_api.preview.dns["REALTIME"]}/event/realtime"
  }
}

resource "aws_sqs_queue" "cma_preview" {
  name                        = "${local.name}-preview.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  message_retention_seconds   = 60
  visibility_timeout_seconds  = 780
  sqs_managed_sse_enabled     = true
  # Disposable hints expire instead of retaining private session IDs in a DLQ.
}

resource "aws_lambda_event_source_mapping" "cma_preview" {
  event_source_arn = aws_sqs_queue.cma_preview.arn
  function_name    = aws_lambda_function.cma_preview.arn
  enabled          = var.preview_enabled
  batch_size       = 1
  scaling_config { maximum_concurrency = 4 }
}

resource "aws_iam_role" "cma_preview" {
  name = "${local.name}-preview"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "cma_preview_vpc" {
  role       = aws_iam_role.cma_preview.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "cma_preview" {
  role = aws_iam_role.cma_preview.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["appsync:EventPublish"], Resource = aws_appsync_channel_namespace.preview.channel_namespace_arn },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.anthropic_api_key.arn },
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.cma_preview.arn }
    ]
  })
}

resource "aws_iam_role_policy" "preview_start" {
  role = aws_iam_role.lambda["cma-scheduler"].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.cma_preview.arn }]
  })
}

resource "aws_cloudwatch_log_group" "cma_preview" {
  name              = "/aws/lambda/${local.name}-preview"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_metric_filter" "preview_failures" {
  for_each = {
    reader = aws_cloudwatch_log_group.cma_preview.name
    web    = aws_cloudwatch_log_group.web_function.name
  }
  name           = "preview-failures"
  log_group_name = each.value
  pattern        = "{ $.event = \"preview_publish_failed\" || $.event = \"preview_reader_failed\" || $.event = \"preview_unavailable\" || $.event = \"preview_socket_error\" || $.event = \"preview_connect_timeout\" }"
  metric_transformation {
    name      = "${each.key}PreviewFailures"
    namespace = local.name
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_lambda_function" "cma_preview" {
  function_name                  = "${local.name}-preview"
  role                           = aws_iam_role.cma_preview.arn
  runtime                        = "nodejs24.x"
  handler                        = "index.handler"
  architectures                  = ["arm64"]
  s3_bucket                      = aws_s3_object.lambda_artifact["cma-preview.zip"].bucket
  s3_key                         = aws_s3_object.lambda_artifact["cma-preview.zip"].key
  s3_object_version              = aws_s3_object.lambda_artifact["cma-preview.zip"].version_id
  source_code_hash               = filebase64sha256("${path.module}/../../dist/lambdas/cma-preview.zip")
  timeout                        = 130
  memory_size                    = 256
  reserved_concurrent_executions = 4
  vpc_config {
    subnet_ids         = [aws_subnet.a2a_private.id, aws_subnet.web_private.id]
    security_group_ids = [aws_security_group.a2a_callers.id]
  }
  environment {
    variables = merge(local.preview_environment, {
      APP_ENV                      = "production"
      ANTHROPIC_API_KEY_SECRET_ARN = aws_secretsmanager_secret.anthropic_api_key.arn
      CMA_A2A_ENDPOINT             = local.private_a2a_url
    })
  }
  depends_on = [aws_cloudwatch_log_group.cma_preview, aws_iam_role_policy_attachment.cma_preview_vpc]
}
