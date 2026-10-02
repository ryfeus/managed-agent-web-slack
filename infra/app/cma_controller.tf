resource "aws_s3_bucket" "cma_pending_input" {
  bucket_prefix = substr("${local.name}-cma-", 0, 37)
}

resource "aws_s3_bucket_public_access_block" "cma_pending_input" {
  bucket                  = aws_s3_bucket.cma_pending_input.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cma_pending_input" {
  bucket = aws_s3_bucket.cma_pending_input.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_iam_policy_document" "cma_pending_input_tls" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.cma_pending_input.arn, "${aws_s3_bucket.cma_pending_input.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "cma_pending_input_tls" {
  bucket     = aws_s3_bucket.cma_pending_input.id
  policy     = data.aws_iam_policy_document.cma_pending_input_tls.json
  depends_on = [aws_s3_bucket_public_access_block.cma_pending_input]
}

resource "aws_sqs_queue" "cma_scheduler_dlq" {
  name                      = "${local.name}-cma-scheduler-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "cma_scheduler" {
  name                       = "${local.name}-cma-scheduler"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 360
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.cma_scheduler_dlq.arn
    maxReceiveCount     = 8
  })
}

resource "aws_sqs_queue" "cma_push_dlq" {
  name                      = "${local.name}-cma-push-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "cma_push" {
  name                       = "${local.name}-cma-push"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 360
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.cma_push_dlq.arn
    maxReceiveCount     = 8
  })
}

resource "aws_lambda_event_source_mapping" "cma_push" {
  event_source_arn        = aws_sqs_queue.cma_push.arn
  function_name           = aws_lambda_function.app["cma-push"].arn
  batch_size              = 10
  function_response_types = ["ReportBatchItemFailures"]
}

resource "aws_cloudwatch_event_rule" "cma_push_maintenance" {
  name                = "${local.name}-cma-push-maintenance"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "cma_push_maintenance" {
  rule  = aws_cloudwatch_event_rule.cma_push_maintenance.name
  arn   = aws_lambda_function.app["cma-push"].arn
  input = jsonencode({ maintenance = "a2a-push" })
}

resource "aws_lambda_permission" "cma_push_maintenance" {
  statement_id  = "AllowCmaPushMaintenance"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.app["cma-push"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.cma_push_maintenance.arn
}

resource "aws_cloudwatch_metric_alarm" "cma_push_dlq" {
  alarm_name          = "${local.name}-cma-push-dlq-visible"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  dimensions          = { QueueName = aws_sqs_queue.cma_push_dlq.name }
}

resource "aws_cloudwatch_log_metric_filter" "cma_push_permanent_failure" {
  name           = "${local.name}-cma-push-permanent-failure"
  log_group_name = aws_cloudwatch_log_group.lambda["cma-push"].name
  pattern        = "a2a_push_permanent_failure"
  metric_transformation {
    name      = "PushPermanentFailure"
    namespace = "${local.name}/CmaController"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "cma_push_permanent_failure" {
  alarm_name          = "${local.name}-cma-push-permanent-failure"
  namespace           = "${local.name}/CmaController"
  metric_name         = "PushPermanentFailure"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "cma_push_worker_errors" {
  alarm_name          = "${local.name}-cma-push-worker-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.app["cma-push"].function_name }
}

resource "aws_lambda_event_source_mapping" "cma_scheduler" {
  event_source_arn        = aws_sqs_queue.cma_scheduler.arn
  function_name           = aws_lambda_function.app["cma-scheduler"].arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
}

resource "aws_cloudwatch_event_rule" "cma_pending_input_gc" {
  name                = "${local.name}-cma-pending-input-gc"
  schedule_expression = "rate(1 day)"
}

resource "aws_cloudwatch_event_target" "cma_pending_input_gc" {
  rule  = aws_cloudwatch_event_rule.cma_pending_input_gc.name
  arn   = aws_lambda_function.app["cma-scheduler"].arn
  input = jsonencode({ maintenance = "pending-input-gc" })
}

resource "aws_lambda_permission" "cma_pending_input_gc" {
  statement_id  = "AllowCmaPendingInputGc"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.app["cma-scheduler"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.cma_pending_input_gc.arn
}

resource "aws_cloudwatch_metric_alarm" "cma_scheduler_dlq" {
  alarm_name          = "${local.name}-cma-scheduler-dlq-visible"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  dimensions          = { QueueName = aws_sqs_queue.cma_scheduler_dlq.name }
}

resource "aws_cloudwatch_log_metric_filter" "cma_stale_pending_input" {
  name           = "${local.name}-cma-stale-pending-input"
  log_group_name = aws_cloudwatch_log_group.lambda["cma-scheduler"].name
  pattern        = "cma_stale_pending_input"
  metric_transformation {
    name      = "StalePendingInput"
    namespace = "${local.name}/CmaController"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "cma_stale_pending_input" {
  alarm_name          = "${local.name}-cma-stale-pending-input"
  namespace           = "${local.name}/CmaController"
  metric_name         = "StalePendingInput"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_log_metric_filter" "cma_ambiguous_side_effect" {
  for_each = toset([
    "cma_ambiguous_create",
    "cma_ambiguous_create_held",
    "cma_ambiguous_send",
    "cma_ambiguous_dispatch_held",
    "cma_ambiguous_confirmation",
    "cma_ambiguous_confirmation_held",
    "cma_ambiguous_interrupt",
    "cma_ambiguous_interrupt_held",
  ])
  name           = "${local.name}-${each.value}"
  log_group_name = aws_cloudwatch_log_group.lambda["cma-scheduler"].name
  pattern        = each.value
  metric_transformation {
    name      = "AmbiguousSideEffect"
    namespace = "${local.name}/CmaController"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "cma_ambiguous_side_effect" {
  alarm_name          = "${local.name}-cma-ambiguous-side-effect"
  namespace           = "${local.name}/CmaController"
  metric_name         = "AmbiguousSideEffect"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}
