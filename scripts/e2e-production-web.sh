#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.uv-cache}"

# This runner is intentionally sealed from live provider credentials.
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE
export ANTHROPIC_API_KEY=e2e-agent ANTHROPIC_API_KEY_SECRET_ARN=
export ANTHROPIC_WEBHOOK_SIGNING_KEY=whsec_ZTJlLXdlYmhvb2stc2VjcmV0
export ANTHROPIC_WEBHOOK_SIGNING_KEY_SECRET_ARN=
export SLACK_BOT_TOKEN=e2e-bot SLACK_BOT_TOKEN_SECRET_ARN=
export SLACK_SIGNING_SECRET=e2e-slack-secret SLACK_SIGNING_SECRET_SECRET_ARN=
export DSQL_ENDPOINT=

export APP_ENV=e2e DATABASE_MODE=postgres PYTHONDONTWRITEBYTECODE=1
export DATABASE_URL="postgresql://managed_agents:managed_agents@127.0.0.1:54321/managed_agents_e2e"
export CLAUDE_AGENT_ID=agent_e2e CLAUDE_ENVIRONMENT_ID=env_e2e
export WEB_ACCESS_TOKEN=e2e-access-token WEB_COOKIE_SECRET=e2e-cookie-secret-at-least-32-bytes
export DEV_PRINCIPAL_ID=00000000-0000-4000-8000-000000000001
export PUBLIC_APP_URL=http://127.0.0.1:3000 NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:3001
export SLACK_TEAM_ID= SLACK_USER_ID=
export SLACK_BOUND_THREAD_REPLIES=true SLACK_AGENT_VIEW_ENABLED=true SLACK_STREAMING_ENABLED=true
export SLACK_TOOL_APPROVALS_ENABLED=true SLACK_RECEIPT_REACTION_ENABLED=true
export SLACK_SOURCE_LINKS_ENABLED=true SLACK_TASK_CARDS_ENABLED=true
export SLACK_FEEDBACK_ENABLED=true SLACK_SHORTCUTS_ENABLED=true
export SLACK_UNFURLS_ENABLED=true SLACK_ACTIVE_CONTEXT_ENABLED=true
export LOCAL_EVENT_BUS_AUTO_DRAIN=false
export COMPOSE_PROJECT_NAME="managed-agents-production-web-e2e-$$"

artifacts="$PWD/test-results/production-web-services"
mkdir -p "$artifacts"
backend_pid= static_pid=
compose_started=false
next_env_backup="$(mktemp)"
cp apps/web/next-env.d.ts "$next_env_backup"

cleanup() {
  result=$?
  trap - EXIT INT TERM
  if [[ $result -ne 0 ]]; then
    curl --max-time 5 -fsS http://127.0.0.1:3001/_test/state > "$artifacts/state.json" 2>/dev/null || true
  fi
  [[ -z "$static_pid" ]] || kill "$static_pid" 2>/dev/null || true
  [[ -z "$backend_pid" ]] || kill "$backend_pid" 2>/dev/null || true
  wait 2>/dev/null || true
  if [[ "$compose_started" == true ]]; then
    docker compose -f docker-compose.e2e.yml logs > "$artifacts/postgres.log" 2>&1 || true
    docker compose -f docker-compose.e2e.yml down -v > "$artifacts/cleanup.log" 2>&1 || true
  fi
  cp "$next_env_backup" apps/web/next-env.d.ts
  rm -f "$next_env_backup"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

node --input-type=module - <<'JS'
import net from 'node:net';
for (const port of [3000, 3001, 54321]) {
  await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => server.close(resolve));
  });
}
JS

npm run build:web > "$artifacts/frontend-build.log" 2>&1
if [[ ! -s apps/web/out/index.html ]]; then
  echo "Static web build did not produce apps/web/out/index.html" >&2
  exit 1
fi

npm exec playwright install chromium > "$artifacts/browser-install.log" 2>&1
docker info > /dev/null
compose_started=true
docker compose -f docker-compose.e2e.yml up -d --wait --wait-timeout 60 > "$artifacts/postgres-start.log" 2>&1
uv run --directory backend python -m managed_agents_app.operations migrate > "$artifacts/migrations.log" 2>&1
uv run --directory backend uvicorn managed_agents_app.local_api:app \
  --host 127.0.0.1 --port 3001 > "$artifacts/backend.log" 2>&1 &
backend_pid=$!
python3 -m http.server 3000 --bind 127.0.0.1 \
  --directory apps/web/out > "$artifacts/static-web.log" 2>&1 &
static_pid=$!

node scripts/e2e-ready.mjs
curl -fsS -X POST http://127.0.0.1:3001/_test/reset > /dev/null
npm exec playwright test -- --config=playwright.production.config.ts
