variable "app_name" {
  type    = string
  default = "managed-agent-web-slack"
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

variable "public_app_url" {
  description = "Public CloudFront origin used in Slack session links."
  type        = string
  default     = ""
}

variable "slack_agent_view_enabled" {
  type    = bool
  default = false
}

variable "slack_bound_thread_replies_enabled" {
  type    = bool
  default = false
}

variable "slack_streaming_enabled" {
  type    = bool
  default = false
}

variable "slack_approvals_enabled" {
  type    = bool
  default = false
}

variable "slack_feedback_enabled" {
  type    = bool
  default = false
}

variable "slack_shortcuts_enabled" {
  type    = bool
  default = false
}

variable "slack_active_context_enabled" {
  type    = bool
  default = false
}

variable "slack_unfurls_enabled" {
  type    = bool
  default = false
}

variable "slack_receipt_reaction_enabled" {
  type    = bool
  default = false
}

variable "slack_receipt_reaction" {
  type    = string
  default = "eyes"
}

variable "slack_source_links_enabled" {
  type    = bool
  default = false
}

variable "slack_task_cards_enabled" {
  type    = bool
  default = false
}
