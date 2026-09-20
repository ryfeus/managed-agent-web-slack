#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${RUN_LIVE_E2E:-}" != 1 ]]; then
  echo 'Live tests send messages to a real developer sandbox. Set RUN_LIVE_E2E=1 and configure the variables in docs/testing.md.' >&2
  exit 1
fi
# Environment is supplied explicitly; do not infer a production target from .env.
for variable in E2E_LIVE_BASE_URL E2E_LIVE_SLACK_CHANNEL_ID E2E_LIVE_SLACK_TEAM_ID E2E_LIVE_SLACK_USER_ID SLACK_BOT_TOKEN SLACK_SIGNING_SECRET WEB_ACCESS_TOKEN; do
  if [[ -z "${!variable:-}" ]]; then echo "Missing $variable" >&2; exit 1; fi
done
exec npm exec playwright test -- --config=playwright.live.config.ts "$@"
