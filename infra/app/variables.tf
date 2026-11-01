variable "app_name" {
  type    = string
  default = "claude-managed-agents-ui-eda"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "aws_region" {
  type    = string
  default = "us-west-2"
}

variable "claude_agent_id" {
  type = string
}

variable "claude_agent_version" {
  type = number
  validation {
    condition     = var.claude_agent_version >= 1 && floor(var.claude_agent_version) == var.claude_agent_version
    error_message = "claude_agent_version must be a positive integer."
  }
}

variable "claude_environment_id" {
  type = string
}

variable "slack_team_allowlist" {
  description = "Optional development-only Slack workspace filter; not an authentication credential."
  type        = string
  default     = ""
}

variable "slack_user_allowlist" {
  description = "Optional development-only Slack user filter; not an authentication credential."
  type        = string
  default     = ""
}

variable "slack_user_bot_id" {
  description = "Optional exact bot ID for locally posted Slack user smoke messages. Requires slack_user_app_id."
  type        = string
  default     = ""
}

variable "slack_user_app_id" {
  description = "Optional exact app ID for locally posted Slack user smoke messages. Requires slack_user_bot_id."
  type        = string
  default     = ""
}

variable "public_app_url" {
  description = "Public CloudFront origin used in Slack session links."
  type        = string
  default     = ""
}

variable "slack_receipt_reaction" {
  type    = string
  default = "eyes"
}

variable "openappa_timeout_seconds" {
  description = "Maximum native policy evaluation budget within the scheduler lease."
  type        = number
  default     = 10
  validation {
    condition     = var.openappa_timeout_seconds > 0 && var.openappa_timeout_seconds <= 30
    error_message = "OpenAPPA timeout must be greater than zero and at most 30 seconds."
  }
}
