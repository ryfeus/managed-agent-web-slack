resource "aws_api_gateway_rest_api" "app" {
  name = local.name
  endpoint_configuration { types = ["REGIONAL"] }
}

resource "aws_api_gateway_resource" "api" {
  rest_api_id = aws_api_gateway_rest_api.app.id
  parent_id   = aws_api_gateway_rest_api.app.root_resource_id
  path_part   = "api"
}

resource "aws_api_gateway_resource" "api_proxy" {
  rest_api_id = aws_api_gateway_rest_api.app.id
  parent_id   = aws_api_gateway_resource.api.id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "api_proxy" {
  rest_api_id   = aws_api_gateway_rest_api.app.id
  resource_id   = aws_api_gateway_resource.api_proxy.id
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "api_proxy" {
  rest_api_id             = aws_api_gateway_rest_api.app.id
  resource_id             = aws_api_gateway_resource.api_proxy.id
  http_method             = aws_api_gateway_method.api_proxy.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.app["web-api"].invoke_arn
  timeout_milliseconds    = 29000
}

resource "aws_api_gateway_resource" "agui" {
  rest_api_id = aws_api_gateway_rest_api.app.id
  parent_id   = aws_api_gateway_resource.api.id
  path_part   = "agui"
}

resource "aws_api_gateway_resource" "agui_proxy" {
  rest_api_id = aws_api_gateway_rest_api.app.id
  parent_id   = aws_api_gateway_resource.agui.id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "agui" {
  rest_api_id   = aws_api_gateway_rest_api.app.id
  resource_id   = aws_api_gateway_resource.agui.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "agui" {
  rest_api_id             = aws_api_gateway_rest_api.app.id
  resource_id             = aws_api_gateway_resource.agui.id
  http_method             = aws_api_gateway_method.agui.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.app["agui-bridge"].response_streaming_invoke_arn
  response_transfer_mode  = "STREAM"
  timeout_milliseconds    = 900000
}

resource "aws_api_gateway_method" "agui_proxy" {
  rest_api_id   = aws_api_gateway_rest_api.app.id
  resource_id   = aws_api_gateway_resource.agui_proxy.id
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "agui_proxy" {
  rest_api_id             = aws_api_gateway_rest_api.app.id
  resource_id             = aws_api_gateway_resource.agui_proxy.id
  http_method             = aws_api_gateway_method.agui_proxy.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.app["agui-bridge"].response_streaming_invoke_arn
  response_transfer_mode  = "STREAM"
  timeout_milliseconds    = 900000
}

resource "aws_api_gateway_resource" "slack" {
  rest_api_id = aws_api_gateway_rest_api.app.id
  parent_id   = aws_api_gateway_rest_api.app.root_resource_id
  path_part   = "slack"
}

resource "aws_api_gateway_resource" "slack_events" {
  rest_api_id = aws_api_gateway_rest_api.app.id
  parent_id   = aws_api_gateway_resource.slack.id
  path_part   = "events"
}

resource "aws_api_gateway_method" "slack_events" {
  rest_api_id   = aws_api_gateway_rest_api.app.id
  resource_id   = aws_api_gateway_resource.slack_events.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "slack_events" {
  rest_api_id             = aws_api_gateway_rest_api.app.id
  resource_id             = aws_api_gateway_resource.slack_events.id
  http_method             = aws_api_gateway_method.slack_events.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.app["slack-ingress"].invoke_arn
}

resource "aws_api_gateway_resource" "slack_interactions" {
  rest_api_id = aws_api_gateway_rest_api.app.id
  parent_id   = aws_api_gateway_resource.slack.id
  path_part   = "interactions"
}

resource "aws_api_gateway_method" "slack_interactions" {
  rest_api_id   = aws_api_gateway_rest_api.app.id
  resource_id   = aws_api_gateway_resource.slack_interactions.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "slack_interactions" {
  rest_api_id             = aws_api_gateway_rest_api.app.id
  resource_id             = aws_api_gateway_resource.slack_interactions.id
  http_method             = aws_api_gateway_method.slack_interactions.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.app["slack-ingress"].invoke_arn
}

resource "aws_api_gateway_resource" "anthropic" {
  rest_api_id = aws_api_gateway_rest_api.app.id
  parent_id   = aws_api_gateway_rest_api.app.root_resource_id
  path_part   = "anthropic"
}

resource "aws_api_gateway_resource" "anthropic_webhook" {
  rest_api_id = aws_api_gateway_rest_api.app.id
  parent_id   = aws_api_gateway_resource.anthropic.id
  path_part   = "webhook"
}

resource "aws_api_gateway_method" "anthropic_webhook" {
  rest_api_id   = aws_api_gateway_rest_api.app.id
  resource_id   = aws_api_gateway_resource.anthropic_webhook.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "anthropic_webhook" {
  rest_api_id             = aws_api_gateway_rest_api.app.id
  resource_id             = aws_api_gateway_resource.anthropic_webhook.id
  http_method             = aws_api_gateway_method.anthropic_webhook.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.app["anthropic-webhook"].invoke_arn
}

resource "aws_lambda_permission" "api_gateway" {
  for_each      = toset(["web-api", "agui-bridge", "slack-ingress", "anthropic-webhook"])
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.app[each.key].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.app.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "app" {
  rest_api_id = aws_api_gateway_rest_api.app.id
  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_integration.api_proxy.id,
      aws_api_gateway_integration.agui.id,
      aws_api_gateway_integration.agui_proxy.id,
      aws_api_gateway_integration.slack_events.id,
      aws_api_gateway_integration.slack_interactions.id,
      aws_api_gateway_integration.anthropic_webhook.id
    ]))
  }
  lifecycle { create_before_destroy = true }
}

resource "aws_api_gateway_stage" "app" {
  rest_api_id   = aws_api_gateway_rest_api.app.id
  deployment_id = aws_api_gateway_deployment.app.id
  stage_name    = var.environment
}

resource "aws_api_gateway_method_settings" "app" {
  rest_api_id = aws_api_gateway_rest_api.app.id
  stage_name  = aws_api_gateway_stage.app.stage_name
  method_path = "*/*"
  settings {
    metrics_enabled = true
    logging_level   = "OFF"
  }
}
