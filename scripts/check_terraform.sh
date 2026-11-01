#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
terraform_data_root="$PWD/.terraform/verify"

terraform fmt -check -recursive infra
UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.uv-cache}" uv run --no-project --with-requirements scripts/requirements-iac.txt python scripts/validate_sam.py
TF_VAR_state_bucket=terraform-validation-state-bucket \
  TF_DATA_DIR="$terraform_data_root/bootstrap" terraform -chdir=infra/bootstrap init -backend=false
TF_VAR_state_bucket=terraform-validation-state-bucket \
  TF_DATA_DIR="$terraform_data_root/bootstrap" terraform -chdir=infra/bootstrap validate
TF_DATA_DIR="$terraform_data_root/app" terraform -chdir=infra/app init -backend=false
TF_DATA_DIR="$terraform_data_root/app" terraform -chdir=infra/app validate
TF_DATA_DIR="$terraform_data_root/app" terraform -chdir=infra/app test
