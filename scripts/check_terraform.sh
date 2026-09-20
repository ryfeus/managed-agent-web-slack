#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
terraform_data_root="$PWD/.terraform/verify"

terraform fmt -check -recursive infra
TF_VAR_state_bucket=terraform-validation-state-bucket \
  TF_DATA_DIR="$terraform_data_root/bootstrap" terraform -chdir=infra/bootstrap init -backend=false
TF_VAR_state_bucket=terraform-validation-state-bucket \
  TF_DATA_DIR="$terraform_data_root/bootstrap" terraform -chdir=infra/bootstrap validate
TF_DATA_DIR="$terraform_data_root/app" terraform -chdir=infra/app init -backend=false
TF_DATA_DIR="$terraform_data_root/app" terraform -chdir=infra/app validate
