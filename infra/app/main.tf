data "aws_caller_identity" "current" {}

locals {
  name          = "${var.app_name}-${var.environment}"
  dsql_endpoint = "${aws_dsql_cluster.app.identifier}.dsql.${var.aws_region}.on.aws"
  lambda_names  = toset(["web-api", "web-stream", "slack-ingress", "agent-input", "anthropic-webhook", "slack-projector"])
  lambda_handlers = {
    web-api           = "managed_agents_app.handlers.web_api.handler"
    web-stream        = "run.sh"
    slack-ingress     = "managed_agents_app.handlers.slack_ingress.handler"
    agent-input       = "managed_agents_app.handlers.agent_input_sqs.handler"
    anthropic-webhook = "managed_agents_app.handlers.anthropic_webhook.handler"
    slack-projector   = "managed_agents_app.handlers.slack_projector.handler"
  }
  lambda_runtimes = {
    web-api           = "python3.14"
    web-stream        = "python3.14"
    slack-ingress     = "python3.14"
    agent-input       = "python3.14"
    anthropic-webhook = "python3.14"
    slack-projector   = "python3.14"
  }
  lambda_artifacts = {
    web-api           = "python-buffered.zip"
    web-stream        = "web-stream.zip"
    slack-ingress     = "python-buffered.zip"
    agent-input       = "python-buffered.zip"
    anthropic-webhook = "python-buffered.zip"
    slack-projector   = "python-buffered.zip"
  }
  dsql_lambdas  = toset(["web-api", "web-stream", "agent-input", "slack-projector"])
  event_lambdas = toset(["slack-ingress", "agent-input", "anthropic-webhook"])
  secret_access = {
    web-api           = [aws_secretsmanager_secret.anthropic_api_key.arn, aws_secretsmanager_secret.web_access_token.arn, aws_secretsmanager_secret.web_cookie_secret.arn]
    web-stream        = [aws_secretsmanager_secret.anthropic_api_key.arn, aws_secretsmanager_secret.web_cookie_secret.arn]
    slack-ingress     = [aws_secretsmanager_secret.slack_signing_secret.arn, aws_secretsmanager_secret.slack_bot_token.arn]
    agent-input       = [aws_secretsmanager_secret.anthropic_api_key.arn, aws_secretsmanager_secret.slack_bot_token.arn]
    anthropic-webhook = [aws_secretsmanager_secret.anthropic_webhook_signing_key.arn]
    slack-projector   = [aws_secretsmanager_secret.anthropic_api_key.arn, aws_secretsmanager_secret.slack_bot_token.arn]
  }
  common_environment = {
    APP_ENV                        = "production"
    APP_NAME                       = var.app_name
    EVENT_BUS_NAME                 = aws_cloudwatch_event_bus.app.name
    CLAUDE_AGENT_ID                = var.claude_agent_id
    CLAUDE_ENVIRONMENT_ID          = var.claude_environment_id
    DSQL_ENDPOINT                  = local.dsql_endpoint
    DSQL_DATABASE                  = "postgres"
    DSQL_ROLE                      = "app_runtime"
    DEV_PRINCIPAL_ID               = "00000000-0000-4000-8000-000000000001"
    SLACK_TEAM_ID                  = var.slack_team_allowlist
    SLACK_USER_ID                  = var.slack_user_allowlist
    PUBLIC_APP_URL                 = var.public_app_url
    SLACK_AGENT_VIEW_ENABLED       = tostring(var.slack_agent_view_enabled)
    SLACK_BOUND_THREAD_REPLIES     = tostring(var.slack_bound_thread_replies_enabled)
    SLACK_STREAMING_ENABLED        = tostring(var.slack_streaming_enabled)
    SLACK_TOOL_APPROVALS_ENABLED   = tostring(var.slack_approvals_enabled)
    SLACK_FEEDBACK_ENABLED         = tostring(var.slack_feedback_enabled)
    SLACK_SHORTCUTS_ENABLED        = tostring(var.slack_shortcuts_enabled)
    SLACK_ACTIVE_CONTEXT_ENABLED   = tostring(var.slack_active_context_enabled)
    SLACK_UNFURLS_ENABLED          = tostring(var.slack_unfurls_enabled)
    SLACK_RECEIPT_REACTION_ENABLED = tostring(var.slack_receipt_reaction_enabled)
    SLACK_RECEIPT_REACTION         = var.slack_receipt_reaction
    SLACK_SOURCE_LINKS_ENABLED     = tostring(var.slack_source_links_enabled)
    SLACK_TASK_CARDS_ENABLED       = tostring(var.slack_task_cards_enabled)
    ANTHROPIC_API_KEY_SECRET_ARN   = aws_secretsmanager_secret.anthropic_api_key.arn
  }
  function_environment = {
    web-api = merge(local.common_environment, {
      WEB_ACCESS_TOKEN_SECRET_ARN  = aws_secretsmanager_secret.web_access_token.arn
      WEB_COOKIE_SECRET_SECRET_ARN = aws_secretsmanager_secret.web_cookie_secret.arn
    })
    web-stream = merge(local.common_environment, {
      WEB_COOKIE_SECRET_SECRET_ARN = aws_secretsmanager_secret.web_cookie_secret.arn
      AWS_LAMBDA_EXEC_WRAPPER      = "/opt/bootstrap"
      AWS_LWA_INVOKE_MODE          = "response_stream"
      PORT                         = "8080"
    })
    slack-ingress = {
      APP_ENV                         = "production"
      EVENT_BUS_NAME                  = aws_cloudwatch_event_bus.app.name
      SLACK_SIGNING_SECRET_SECRET_ARN = aws_secretsmanager_secret.slack_signing_secret.arn
      SLACK_BOT_TOKEN_SECRET_ARN      = aws_secretsmanager_secret.slack_bot_token.arn
    }
    agent-input = merge(local.common_environment, {
      SLACK_BOT_TOKEN_SECRET_ARN = aws_secretsmanager_secret.slack_bot_token.arn
    })
    anthropic-webhook = {
      APP_ENV                                  = "production"
      EVENT_BUS_NAME                           = aws_cloudwatch_event_bus.app.name
      ANTHROPIC_WEBHOOK_SIGNING_KEY_SECRET_ARN = aws_secretsmanager_secret.anthropic_webhook_signing_key.arn
    }
    slack-projector = merge(local.common_environment, {
      SLACK_BOT_TOKEN_SECRET_ARN = aws_secretsmanager_secret.slack_bot_token.arn
    })
  }
}

resource "aws_dsql_cluster" "app" {
  deletion_protection_enabled = true
  kms_encryption_key          = "AWS_OWNED_KMS_KEY"
}

resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name = "${local.name}/anthropic-api-key"
}

resource "aws_secretsmanager_secret" "anthropic_webhook_signing_key" {
  name = "${local.name}/anthropic-webhook-signing-key"
}

resource "aws_secretsmanager_secret" "slack_signing_secret" {
  name = "${local.name}/slack-signing-secret"
}

resource "aws_secretsmanager_secret" "slack_bot_token" {
  name = "${local.name}/slack-bot-token"
}

resource "aws_secretsmanager_secret" "web_access_token" {
  name = "${local.name}/web-access-token"
}

resource "aws_secretsmanager_secret" "web_cookie_secret" {
  name = "${local.name}/web-cookie-secret"
}

resource "aws_iam_role" "lambda" {
  for_each = local.lambda_names
  name     = "${local.name}-${each.key}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  for_each   = local.lambda_names
  role       = aws_iam_role.lambda[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_access" {
  for_each = local.lambda_names
  name     = "application-access"
  role     = aws_iam_role.lambda[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      contains(local.dsql_lambdas, each.key) ? [{
        Effect = "Allow", Action = ["dsql:DbConnect"], Resource = aws_dsql_cluster.app.arn
      }] : [],
      contains(local.event_lambdas, each.key) ? [{
        Effect = "Allow", Action = ["events:PutEvents"], Resource = aws_cloudwatch_event_bus.app.arn
      }] : [],
      each.key == "agent-input" ? [{
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.agent_input.arn
      }] : [],
      length(local.secret_access[each.key]) > 0 ? [{
        Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = local.secret_access[each.key]
      }] : []
    )
  })
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = local.lambda_names
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = 14
}

resource "aws_lambda_function" "app" {
  for_each         = local.lambda_names
  function_name    = "${local.name}-${each.key}"
  role             = aws_iam_role.lambda[each.key].arn
  runtime          = local.lambda_runtimes[each.key]
  handler          = local.lambda_handlers[each.key]
  architectures    = ["x86_64"]
  layers           = each.key == "web-stream" ? ["arn:aws:lambda:${var.aws_region}:753240598075:layer:LambdaAdapterLayerX86:28"] : []
  filename         = "${path.module}/../../dist/lambdas/${local.lambda_artifacts[each.key]}"
  source_code_hash = filebase64sha256("${path.module}/../../dist/lambdas/${local.lambda_artifacts[each.key]}")
  timeout          = contains(["web-stream", "slack-projector"], each.key) ? 900 : 60
  memory_size      = each.key == "web-stream" ? 1024 : 512

  environment { variables = local.function_environment[each.key] }
  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy_attachment.lambda_logs]
}

resource "aws_cloudwatch_event_bus" "app" {
  name = local.name
}

resource "aws_sqs_queue" "event_dlq" {
  name                      = "${local.name}-event-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "agent_input_dlq" {
  name                      = "${local.name}-agent-input-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "agent_input" {
  name                       = "${local.name}-agent-input"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 360
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.agent_input_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue_policy" "agent_input" {
  queue_url = aws_sqs_queue.agent_input.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.agent_input.arn
      Condition = { ArnEquals = { "aws:SourceArn" = [
        aws_cloudwatch_event_rule.slack_message.arn,
        aws_cloudwatch_event_rule.slack_action.arn
      ] } }
    }]
  })
}

resource "aws_sqs_queue_policy" "event_dlq" {
  queue_url = aws_sqs_queue.event_dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.event_dlq.arn
      Condition = { ArnEquals = { "aws:SourceArn" = [
        aws_cloudwatch_event_rule.slack_message.arn,
        aws_cloudwatch_event_rule.slack_action.arn,
        aws_cloudwatch_event_rule.session_changed.arn,
        aws_cloudwatch_event_rule.control_reply.arn,
        aws_cloudwatch_event_rule.projection_requested.arn
      ] } }
    }]
  })
}

resource "aws_cloudwatch_event_rule" "slack_message" {
  name           = "${local.name}-slack-message"
  event_bus_name = aws_cloudwatch_event_bus.app.name
  event_pattern = jsonencode({
    source      = ["app.slack"]
    detail-type = ["SlackMessageReceived"]
  })
}

resource "aws_cloudwatch_event_rule" "session_changed" {
  name           = "${local.name}-session-changed"
  event_bus_name = aws_cloudwatch_event_bus.app.name
  event_pattern = jsonencode({
    source      = ["app.managed-agent"]
    detail-type = ["ManagedAgentSessionChanged"]
  })
}

resource "aws_cloudwatch_event_rule" "slack_action" {
  name           = "${local.name}-slack-action"
  event_bus_name = aws_cloudwatch_event_bus.app.name
  event_pattern = jsonencode({
    source = ["app.slack"]
    detail-type = [
      "SlackSessionStopRequested",
      "SlackToolConfirmationRequested",
      "SlackFeedbackReceived",
      "SlackShortcutReceived",
      "SlackSessionLinkRequested",
      "SlackSessionLinkShared"
    ]
  })
}

resource "aws_cloudwatch_event_rule" "control_reply" {
  name           = "${local.name}-control-reply"
  event_bus_name = aws_cloudwatch_event_bus.app.name
  event_pattern = jsonencode({
    source      = ["app.slack"]
    detail-type = ["SlackControlReplyRequested"]
  })
}

resource "aws_cloudwatch_event_rule" "projection_requested" {
  name           = "${local.name}-projection-requested"
  event_bus_name = aws_cloudwatch_event_bus.app.name
  event_pattern = jsonencode({
    source      = ["app.slack"]
    detail-type = ["SlackProjectionRequested", "SlackWorkStarted"]
  })
}

resource "aws_cloudwatch_event_target" "agent_input" {
  rule           = aws_cloudwatch_event_rule.slack_message.name
  event_bus_name = aws_cloudwatch_event_bus.app.name
  arn            = aws_sqs_queue.agent_input.arn
  dead_letter_config { arn = aws_sqs_queue.event_dlq.arn }
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 8
  }
}

resource "aws_cloudwatch_event_target" "agent_action" {
  rule           = aws_cloudwatch_event_rule.slack_action.name
  event_bus_name = aws_cloudwatch_event_bus.app.name
  arn            = aws_sqs_queue.agent_input.arn
  dead_letter_config { arn = aws_sqs_queue.event_dlq.arn }
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 8
  }
}

resource "aws_cloudwatch_event_target" "session_changed" {
  rule           = aws_cloudwatch_event_rule.session_changed.name
  event_bus_name = aws_cloudwatch_event_bus.app.name
  arn            = aws_lambda_function.app["slack-projector"].arn
  dead_letter_config { arn = aws_sqs_queue.event_dlq.arn }
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 8
  }
}

resource "aws_cloudwatch_event_target" "control_reply" {
  rule           = aws_cloudwatch_event_rule.control_reply.name
  event_bus_name = aws_cloudwatch_event_bus.app.name
  arn            = aws_lambda_function.app["slack-projector"].arn
  dead_letter_config { arn = aws_sqs_queue.event_dlq.arn }
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 8
  }
}

resource "aws_cloudwatch_event_target" "projection_requested" {
  rule           = aws_cloudwatch_event_rule.projection_requested.name
  event_bus_name = aws_cloudwatch_event_bus.app.name
  arn            = aws_lambda_function.app["slack-projector"].arn
  dead_letter_config { arn = aws_sqs_queue.event_dlq.arn }
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 8
  }
}

resource "aws_lambda_event_source_mapping" "agent_input" {
  event_source_arn        = aws_sqs_queue.agent_input.arn
  function_name           = aws_lambda_function.app["agent-input"].arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
}

resource "aws_lambda_permission" "eventbridge_projector" {
  for_each = {
    session    = aws_cloudwatch_event_rule.session_changed.arn
    control    = aws_cloudwatch_event_rule.control_reply.arn
    projection = aws_cloudwatch_event_rule.projection_requested.arn
  }
  statement_id  = "Allow${title(each.key)}EventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.app["slack-projector"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = each.value
}

resource "aws_cloudwatch_metric_alarm" "event_dlq" {
  alarm_name          = "${local.name}-event-dlq-visible"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  dimensions          = { QueueName = aws_sqs_queue.event_dlq.name }
}

resource "aws_cloudwatch_metric_alarm" "agent_input_dlq" {
  alarm_name          = "${local.name}-agent-input-dlq-visible"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  dimensions          = { QueueName = aws_sqs_queue.agent_input_dlq.name }
}
