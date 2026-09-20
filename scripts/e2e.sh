#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.uv-cache}"
export APP_ENV=e2e DATABASE_MODE=postgres PYTHONDONTWRITEBYTECODE=1
export DATABASE_URL="postgresql://managed_agents:managed_agents@127.0.0.1:${E2E_POSTGRES_PORT:-54321}/managed_agents_e2e"
export CLAUDE_AGENT_ID=agent_e2e CLAUDE_ENVIRONMENT_ID=env_e2e
export WEB_ACCESS_TOKEN=e2e-access-token WEB_COOKIE_SECRET=e2e-cookie-secret-at-least-32-bytes
export DEV_PRINCIPAL_ID=00000000-0000-4000-8000-000000000001
export PUBLIC_APP_URL=http://127.0.0.1:3000 NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:3001
export SLACK_TEAM_ID= SLACK_USER_ID=
export SLACK_BOUND_THREAD_REPLIES=true SLACK_AGENT_VIEW_ENABLED=true SLACK_STREAMING_ENABLED=true
export SLACK_TOOL_APPROVALS_ENABLED=true SLACK_RECEIPT_REACTION_ENABLED=true SLACK_SOURCE_LINKS_ENABLED=true SLACK_TASK_CARDS_ENABLED=true
export SLACK_FEEDBACK_ENABLED=true SLACK_SHORTCUTS_ENABLED=true SLACK_UNFURLS_ENABLED=true SLACK_ACTIVE_CONTEXT_ENABLED=true
export LOCAL_EVENT_BUS_AUTO_DRAIN=false
export COMPOSE_PROJECT_NAME="managed-agents-e2e-$$"
artifacts="$PWD/test-results/services"
mkdir -p "$artifacts"
backend_pid= frontend_pid=
compose_started=false
cleanup() {
  result=$?
  trap - EXIT INT TERM
  if [[ $result -ne 0 ]]; then
    curl --max-time 5 -fsS http://127.0.0.1:3001/_test/state > "$artifacts/state.json" 2>/dev/null || true
  fi
  [[ -z "$frontend_pid" ]] || kill "$frontend_pid" 2>/dev/null || true
  [[ -z "$backend_pid" ]] || kill "$backend_pid" 2>/dev/null || true
  wait 2>/dev/null || true
  if [[ "$compose_started" == true ]]; then
    docker compose -f docker-compose.e2e.yml logs > "$artifacts/postgres.log" 2>&1 || true
    docker compose -f docker-compose.e2e.yml down -v > "$artifacts/cleanup.log" 2>&1 || true
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Refuse to reuse unrelated processes or databases.
node --input-type=module - <<'JS'
import net from 'node:net';
for (const port of [3000,3001,Number(process.env.E2E_POSTGRES_PORT || 54321)]) {
  await new Promise((resolve,reject) => {
    const server=net.createServer();
    server.once('error',reject);
    server.listen(port,'127.0.0.1',()=>server.close(resolve));
  });
}
JS
npm exec playwright install chromium > "$artifacts/browser-install.log" 2>&1
docker info > /dev/null
compose_started=true
docker compose -f docker-compose.e2e.yml up -d --wait --wait-timeout 60 > "$artifacts/postgres-start.log" 2>&1
uv run --directory backend python -m managed_agents_app.operations migrate > "$artifacts/migrations.log" 2>&1
RUN_POSTGRES_TESTS=1 uv run --directory backend pytest tests/integration/test_postgres.py > "$artifacts/postgres-tests.log" 2>&1
if [[ "${1:-}" == "--dev" ]]; then export LOCAL_EVENT_BUS_AUTO_DRAIN=true; fi
uv run --directory backend uvicorn managed_agents_app.local_api:app --host 127.0.0.1 --port 3001 > "$artifacts/backend.log" 2>&1 &
backend_pid=$!
node node_modules/next/dist/bin/next dev apps/web --hostname 127.0.0.1 --port 3000 > "$artifacts/frontend.log" 2>&1 &
frontend_pid=$!
node scripts/e2e-ready.mjs
curl -fsS -X POST http://127.0.0.1:3001/_test/reset > /dev/null
if [[ "${1:-}" == "--dev" ]]; then
  echo 'E2E development ready at http://127.0.0.1:3000 (token: e2e-access-token)'
  wait "$backend_pid" "$frontend_pid"
else
  npm exec playwright test -- "$@"
fi
