#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

plan_only=false
if [[ $# -gt 0 ]]; then
  if [[ "$1" == "--plan-only" && $# -eq 1 ]]; then
    plan_only=true
  else
    echo "Usage: ./scripts/deploy.sh [--plan-only]" >&2
    exit 2
  fi
fi

if [[ ! -f .env ]]; then
  echo "Missing .env; copy .env.example and configure a real deployment first." >&2
  exit 1
fi
set -a
source .env
set +a

aws_profile_value="${AWS_PROFILE:-default}"
aws_region_value="${AWS_REGION:-us-west-2}"
app_name_value="${APP_NAME:-managed-agent-web-slack}"
environment_value="${DEPLOYMENT_ENVIRONMENT:-dev}"
agent_id_value="${CLAUDE_AGENT_ID:-${AGENT_ID:-}}"
export AWS_REGION="$aws_region_value"
export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_SDK_LOAD_CONFIG=1

require_value() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" ]]; then
    echo "Missing required deployment configuration: $name" >&2
    return 1
  fi
}

validate_deployment_configuration() {
  require_value "CLAUDE_AGENT_ID (AGENT_ID is a deprecated alias)" "$agent_id_value"
  require_value "CLAUDE_ENVIRONMENT_ID" "${CLAUDE_ENVIRONMENT_ID:-}"
  require_value "ANTHROPIC_API_KEY" "${ANTHROPIC_API_KEY:-}"
  require_value "WEB_ACCESS_TOKEN" "${WEB_ACCESS_TOKEN:-}"
  require_value "WEB_COOKIE_SECRET" "${WEB_COOKIE_SECRET:-}"
}

discover_aws_account() {
  if ! account_id="$(aws sts get-caller-identity --profile "$aws_profile_value" --query Account --output text 2>/dev/null)"; then
    echo "AWS SSO session is unavailable; opening login for profile $aws_profile_value." >&2
    aws sso login --profile "$aws_profile_value" >&2
    account_id="$(aws sts get-caller-identity --profile "$aws_profile_value" --query Account --output text)"
  fi
  printf '%s\n' "$account_id"
}

render_backend_configuration() {
  local account_id="$1"
  local args=(
    --account-id "$account_id"
    --app-name "$app_name_value"
    --environment "$environment_value"
    --region "$aws_region_value"
    --output .generated/terraform/backend.hcl
  )
  if [[ -n "${EXPECTED_AWS_ACCOUNT_ID:-}" ]]; then
    args+=(--expected-account-id "$EXPECTED_AWS_ACCOUNT_ID")
  fi
  if [[ -n "${TF_STATE_BUCKET:-}" ]]; then
    args+=(--state-bucket "$TF_STATE_BUCKET")
  fi
  python3 scripts/render_terraform_backend.py "${args[@]}"
}

bootstrap_state_bucket() {
  local state_bucket
  state_bucket="$(awk -F '"' '/^bucket/ { print $2 }' .generated/terraform/backend.hcl)"
  if aws s3api head-bucket --bucket "$state_bucket" >/dev/null 2>&1; then
    return
  fi
  terraform -chdir=infra/bootstrap init -backend=false
  terraform -chdir=infra/bootstrap apply -auto-approve \
    -var="state_bucket=$state_bucket" \
    -var="aws_region=$aws_region_value"
}

require_state_bucket() {
  local state_bucket
  state_bucket="$(awk -F '"' '/^bucket/ { print $2 }' .generated/terraform/backend.hcl)"
  if ! aws s3api head-bucket --bucket "$state_bucket" >/dev/null 2>&1; then
    echo "State bucket '$state_bucket' does not exist or is inaccessible; --plan-only will not bootstrap it." >&2
    return 1
  fi
}

initialize_application_terraform() {
  terraform -chdir=infra/app init -reconfigure -backend-config=../../.generated/terraform/backend.hcl
}

configure_terraform_inputs() {
  local existing_url
  existing_url="$(terraform -chdir=infra/app output -raw application_url 2>/dev/null || true)"
  export TF_VAR_app_name="$app_name_value"
  export TF_VAR_environment="$environment_value"
  export TF_VAR_aws_region="$aws_region_value"
  export TF_VAR_claude_agent_id="$agent_id_value"
  export TF_VAR_claude_environment_id="${CLAUDE_ENVIRONMENT_ID:-}"
  export TF_VAR_slack_team_allowlist="${SLACK_TEAM_ID:-}"
  export TF_VAR_slack_user_allowlist="${SLACK_USER_ID:-}"
  export TF_VAR_public_app_url="${PUBLIC_APP_URL:-$existing_url}"
  export TF_VAR_slack_bound_thread_replies_enabled="${SLACK_BOUND_THREAD_REPLIES:-false}"
  export TF_VAR_slack_agent_view_enabled="${SLACK_AGENT_VIEW_ENABLED:-false}"
  export TF_VAR_slack_streaming_enabled="${SLACK_STREAMING_ENABLED:-false}"
  export TF_VAR_slack_approvals_enabled="${SLACK_TOOL_APPROVALS_ENABLED:-false}"
  export TF_VAR_slack_feedback_enabled="${SLACK_FEEDBACK_ENABLED:-false}"
  export TF_VAR_slack_shortcuts_enabled="${SLACK_SHORTCUTS_ENABLED:-false}"
  export TF_VAR_slack_active_context_enabled="${SLACK_ACTIVE_CONTEXT_ENABLED:-false}"
  export TF_VAR_slack_unfurls_enabled="${SLACK_UNFURLS_ENABLED:-false}"
  export TF_VAR_slack_receipt_reaction_enabled="${SLACK_RECEIPT_REACTION_ENABLED:-false}"
  export TF_VAR_slack_receipt_reaction="${SLACK_RECEIPT_REACTION:-eyes}"
  export TF_VAR_slack_source_links_enabled="${SLACK_SOURCE_LINKS_ENABLED:-false}"
  export TF_VAR_slack_task_cards_enabled="${SLACK_TASK_CARDS_ENABLED:-false}"
}

build_and_plan() {
  npm run lint
  npm run typecheck
  npm test
  npm run build:web
  ./scripts/build_python_lambdas.sh

  mkdir -p dist
  terraform -chdir=infra/app plan -out=../../dist/application.tfplan
  block_destructive_core_changes
}

block_destructive_core_changes() {
  local destructive
  destructive="$(terraform -chdir=infra/app show -json ../../dist/application.tfplan | jq -r '
    .resource_changes[]?
    | select((.change.actions | index("delete")) != null)
    | select(
        (.type | startswith("aws_dsql_"))
        or (.type | startswith("aws_s3_"))
        or (.type | startswith("aws_cloudfront_"))
        or (.type | startswith("aws_api_gateway_"))
        or (.type | startswith("aws_lambda_"))
        or (.type | startswith("aws_sqs_"))
        or (.type | startswith("aws_cloudwatch_event_"))
        or (.type | startswith("aws_secretsmanager_"))
        or (.type | startswith("aws_iam_"))
      )
    | "\(.type).\(.name): \(.change.actions | join(" -> "))"
  ')"
  if [[ -n "$destructive" ]]; then
    echo "Refusing a plan with destructive core infrastructure changes:" >&2
    echo "$destructive" >&2
    return 1
  fi
}

deploy_application() {
  build_and_plan
  terraform -chdir=infra/app apply -auto-approve ../../dist/application.tfplan

  npm run secrets:sync
  export DSQL_ENDPOINT="$(terraform -chdir=infra/app output -raw dsql_endpoint)"
  export DSQL_RUNTIME_ROLE_ARNS="$(terraform -chdir=infra/app output -json dsql_runtime_role_arns | jq -r 'join(",")')"
  npm run db:migrate
  npm run db:seed

  local web_bucket distribution_id
  web_bucket="$(terraform -chdir=infra/app output -raw web_bucket_name)"
  distribution_id="$(terraform -chdir=infra/app output -raw cloudfront_distribution_id)"
  aws s3 sync apps/web/out "s3://$web_bucket" --delete
  aws cloudfront create-invalidation --distribution-id "$distribution_id" --paths '/*' >/dev/null
  python3 scripts/render_slack_manifest.py
  terraform -chdir=infra/app output
}

validate_deployment_configuration
account_id="$(discover_aws_account)"
render_backend_configuration "$account_id"
if [[ "$plan_only" == true ]]; then
  echo "Planning app '$app_name_value' in '$environment_value' for AWS account $account_id, region $aws_region_value."
else
  echo "Deploying app '$app_name_value' in '$environment_value' to AWS account $account_id, region $aws_region_value."
fi
eval "$(aws configure export-credentials --profile "$aws_profile_value" --format env)"
unset AWS_PROFILE AWS_DEFAULT_PROFILE
if [[ "$plan_only" == true ]]; then
  require_state_bucket
else
  bootstrap_state_bucket
fi
initialize_application_terraform
configure_terraform_inputs
if [[ "$plan_only" == true ]]; then
  build_and_plan
  echo "Terraform plan saved at dist/application.tfplan; no infrastructure was changed."
  exit 0
fi
deploy_application
