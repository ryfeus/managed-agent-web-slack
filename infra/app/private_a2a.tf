data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  private_a2a_url = "https://${aws_api_gateway_rest_api.private_a2a.id}.execute-api.${var.aws_region}.amazonaws.com/${var.environment}"
}

resource "aws_vpc" "a2a" {
  cidr_block           = "10.77.0.0/20"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${local.name}-a2a" }
}

resource "aws_internet_gateway" "a2a" {
  vpc_id = aws_vpc.a2a.id
  tags   = { Name = "${local.name}-a2a" }
}

resource "aws_subnet" "a2a_public" {
  vpc_id                  = aws_vpc.a2a.id
  cidr_block              = "10.77.0.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.name}-a2a-public" }
}

resource "aws_subnet" "a2a_private" {
  vpc_id            = aws_vpc.a2a.id
  cidr_block        = "10.77.1.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]
  tags              = { Name = "${local.name}-a2a-private" }
}

resource "aws_route_table" "a2a_public" {
  vpc_id = aws_vpc.a2a.id
  tags   = { Name = "${local.name}-a2a-public" }
}

resource "aws_route" "a2a_public_default" {
  route_table_id         = aws_route_table.a2a_public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.a2a.id
}

resource "aws_route_table_association" "a2a_public" {
  subnet_id      = aws_subnet.a2a_public.id
  route_table_id = aws_route_table.a2a_public.id
}

resource "aws_eip" "a2a_nat" {
  domain = "vpc"
  tags   = { Name = "${local.name}-a2a-nat" }
}

resource "aws_nat_gateway" "a2a" {
  allocation_id = aws_eip.a2a_nat.id
  subnet_id     = aws_subnet.a2a_public.id
  depends_on    = [aws_internet_gateway.a2a]
  tags          = { Name = "${local.name}-a2a" }
}

resource "aws_route_table" "a2a_private" {
  vpc_id = aws_vpc.a2a.id
  tags   = { Name = "${local.name}-a2a-private" }
}

resource "aws_route" "a2a_private_default" {
  route_table_id         = aws_route_table.a2a_private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.a2a.id
}

resource "aws_route_table_association" "a2a_private" {
  subnet_id      = aws_subnet.a2a_private.id
  route_table_id = aws_route_table.a2a_private.id
}

resource "aws_security_group" "a2a_callers" {
  name_prefix = "${local.name}-a2a-callers-"
  description = "Private A2A caller Lambdas"
  vpc_id      = aws_vpc.a2a.id
  tags        = { Name = "${local.name}-a2a-callers" }
}

resource "aws_vpc_security_group_egress_rule" "a2a_callers_https" {
  security_group_id = aws_security_group.a2a_callers.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "a2a_callers_dsql" {
  security_group_id = aws_security_group.a2a_callers.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432
}

resource "aws_security_group" "a2a_endpoint" {
  name_prefix = "${local.name}-a2a-endpoint-"
  description = "Private API Gateway endpoint"
  vpc_id      = aws_vpc.a2a.id
  tags        = { Name = "${local.name}-a2a-endpoint" }
}

resource "aws_vpc_security_group_ingress_rule" "a2a_endpoint_https" {
  security_group_id            = aws_security_group.a2a_endpoint.id
  referenced_security_group_id = aws_security_group.a2a_callers.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_endpoint" "a2a_execute_api" {
  vpc_id              = aws_vpc.a2a.id
  service_name        = "com.amazonaws.${var.aws_region}.execute-api"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [aws_subnet.a2a_private.id]
  security_group_ids  = [aws_security_group.a2a_endpoint.id]
  tags                = { Name = "${local.name}-a2a-execute-api" }
}

resource "aws_api_gateway_rest_api" "private_a2a" {
  name = "${local.name}-private-a2a"
  endpoint_configuration {
    types            = ["PRIVATE"]
    vpc_endpoint_ids = [aws_vpc_endpoint.a2a_execute_api.id]
  }
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = "execute-api:Invoke"
      Resource  = "execute-api:/*"
      Condition = { StringEquals = { "aws:SourceVpce" = aws_vpc_endpoint.a2a_execute_api.id } }
    }]
  })
}

resource "aws_api_gateway_resource" "a2a_controller_proxy" {
  rest_api_id = aws_api_gateway_rest_api.private_a2a.id
  parent_id   = aws_api_gateway_rest_api.private_a2a.root_resource_id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "a2a_controller_proxy" {
  rest_api_id   = aws_api_gateway_rest_api.private_a2a.id
  resource_id   = aws_api_gateway_resource.a2a_controller_proxy.id
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "a2a_controller_proxy" {
  rest_api_id             = aws_api_gateway_rest_api.private_a2a.id
  resource_id             = aws_api_gateway_resource.a2a_controller_proxy.id
  http_method             = aws_api_gateway_method.a2a_controller_proxy.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.app["cma-controller"].response_streaming_invoke_arn
  response_transfer_mode  = "STREAM"
  timeout_milliseconds    = 900000
}

resource "aws_api_gateway_resource" "a2a_internal" {
  rest_api_id = aws_api_gateway_rest_api.private_a2a.id
  parent_id   = aws_api_gateway_rest_api.private_a2a.root_resource_id
  path_part   = "internal"
}

resource "aws_api_gateway_resource" "a2a_internal_a2a" {
  rest_api_id = aws_api_gateway_rest_api.private_a2a.id
  parent_id   = aws_api_gateway_resource.a2a_internal.id
  path_part   = "a2a"
}

resource "aws_api_gateway_resource" "a2a_internal_events" {
  rest_api_id = aws_api_gateway_rest_api.private_a2a.id
  parent_id   = aws_api_gateway_resource.a2a_internal_a2a.id
  path_part   = "events"
}

resource "aws_api_gateway_resource" "a2a_internal_agent" {
  rest_api_id = aws_api_gateway_rest_api.private_a2a.id
  parent_id   = aws_api_gateway_resource.a2a_internal_events.id
  path_part   = "{agent_id}"
}

resource "aws_api_gateway_method" "a2a_internal_agent" {
  rest_api_id   = aws_api_gateway_rest_api.private_a2a.id
  resource_id   = aws_api_gateway_resource.a2a_internal_agent.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "a2a_internal_agent" {
  rest_api_id             = aws_api_gateway_rest_api.private_a2a.id
  resource_id             = aws_api_gateway_resource.a2a_internal_agent.id
  http_method             = aws_api_gateway_method.a2a_internal_agent.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.app["a2a-event-sink"].invoke_arn
  timeout_milliseconds    = 29000
}

resource "aws_lambda_permission" "private_a2a_gateway" {
  for_each      = toset(["cma-controller", "a2a-event-sink"])
  statement_id  = "AllowPrivateA2AGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.app[each.key].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.private_a2a.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "private_a2a" {
  rest_api_id = aws_api_gateway_rest_api.private_a2a.id
  triggers = {
    redeployment = filesha1("${path.module}/private_a2a.tf")
  }
  depends_on = [aws_api_gateway_integration.a2a_controller_proxy, aws_api_gateway_integration.a2a_internal_agent]
  lifecycle { create_before_destroy = true }
}

resource "aws_api_gateway_stage" "private_a2a" {
  rest_api_id   = aws_api_gateway_rest_api.private_a2a.id
  deployment_id = aws_api_gateway_deployment.private_a2a.id
  stage_name    = var.environment
}
