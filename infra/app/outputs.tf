output "application_url" {
  value = "https://${aws_cloudfront_distribution.app.domain_name}"
}

output "private_a2a_url" {
  value       = local.private_a2a_url
  description = "Private API URL, reachable only through the application's VPC endpoint."
}

output "private_a2a_vpc_endpoint_id" {
  value = aws_vpc_endpoint.a2a_execute_api.id
}

output "slack_events_url" {
  value = "https://${aws_cloudfront_distribution.app.domain_name}/slack/events"
}

output "slack_interactions_url" {
  value = "https://${aws_cloudfront_distribution.app.domain_name}/slack/interactions"
}

output "anthropic_webhook_url" {
  value = "https://${aws_cloudfront_distribution.app.domain_name}/anthropic/webhook"
}

output "dsql_endpoint" {
  value = local.dsql_endpoint
}

output "cma_push_queue_url" {
  value = aws_sqs_queue.cma_push.id
}

output "dsql_runtime_role_arns" {
  value = [for name in local.dsql_lambdas : aws_iam_role.lambda[name].arn]
}

output "web_bucket_name" {
  value = aws_s3_bucket.web.id
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.app.id
}

output "anthropic_api_key_secret_arn" {
  value = aws_secretsmanager_secret.anthropic_api_key.arn
}

output "anthropic_webhook_signing_key_secret_arn" {
  value = aws_secretsmanager_secret.anthropic_webhook_signing_key.arn
}

output "slack_signing_secret_arn" {
  value = aws_secretsmanager_secret.slack_signing_secret.arn
}

output "slack_bot_token_secret_arn" {
  value = aws_secretsmanager_secret.slack_bot_token.arn
}

output "web_access_token_secret_arn" {
  value = aws_secretsmanager_secret.web_access_token.arn
}

output "web_cookie_secret_secret_arn" {
  value = aws_secretsmanager_secret.web_cookie_secret.arn
}
