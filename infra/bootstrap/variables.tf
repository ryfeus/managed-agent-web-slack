variable "state_bucket" {
  description = "Globally unique S3 bucket name for Terraform remote state."
  type        = string
}

variable "aws_region" {
  description = "AWS region where the state bucket is provisioned."
  type        = string
  default     = "us-west-2"
}
