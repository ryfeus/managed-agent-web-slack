# Testing and verification

## Authoritative verification

Use one of the repository-owned verification entrypoints instead of assembling a
completion check by hand:

```sh
./scripts/verify --fast
./scripts/verify --full
```

`--fast` is the repeatable coding loop: architecture and repository rails, lint,
format checks, typechecks, and unit/contract tests without Docker, browsers, or
live services. `--full` includes the fast checks plus the production static web
artifact, Lambda ZIPs, both Terraform roots, real local PostgreSQL behavior, and
both browser layers. Running `./scripts/verify` without an argument means `--full`.

Both modes validate CMA source/lock structure locally without ant or credentials.
Neither mode deploys or uses AWS, DSQL, Slack, or Anthropic credentials. Live
sandbox tests remain a separate explicit command.

Prerequisites are Node 22+, `uv` with Python 3.14, Docker with Compose v2,
Terraform, and Chromium for Playwright. Install dependencies with `npm ci` and
`uv sync --directory backend --locked`. On Linux, install browser dependencies
once with `npx playwright install --with-deps chromium`.

## Test taxonomy

Use the innermost layer capable of proving a property:

1. Unit and pure contract tests cover reducers, normalization, serialization,
   authorization helpers, protocol transformations, architecture checks, and
   small state machines. They are deterministic and need no Docker or network.
2. Local integration tests use real PostgreSQL for repositories, migrations,
   transactions, claims, leases, rollbacks, and database races.
3. Deterministic semantic E2E uses Playwright, the real AG-UI HTTP bridge,
   `ThreadAgentService`, local A2A controller, PostgreSQL, `FakeCmaProvider`,
   `LocalEventBus`, and `RecordingSlack`. Put
   duplicates, retries, fault injection, and cross-surface behavior here.
4. Live contract/smoke tests cover only provider behavior that cannot be proven
   locally, such as installed Slack scopes, Anthropic wire compatibility, or
   DSQL-specific semantics. They are small, sandboxed, and explicitly opted in.
5. The local real-Slack-user smoke posts as a mapped human through Slack's Web
   API, observes Slack-generated event delivery to the deployed app, and checks
   one Slack thread with two completed A2A Tasks. It is opt-in and not a CI gate.

Do not move a test outward merely because production uses a cloud service.
Application idempotency belongs locally; a provider's installed-token behavior
belongs in a live smoke test.

For distributed, protocol, persistence, authorization, or external-integration
changes, start from `docs/feature-contract-template.md`. Map each behavioral
requirement to a test and define duplicate, ordering, retry, crash-boundary,
authorization, persistence, and authoritative-state semantics before completion.

Material correctness gaps belong in `knowledge/known-limitations.json`. IDs are
stable, referenced by the executable test demonstrating current behavior, and
validated by `./scripts/verify --fast`. When a limitation is fixed, update the
behavior test and remove or resolve its registry entry in the same change.

## Enforced repository rails

The fast verifier runs three closed-by-default policy checks before linting:

- Provider SDK imports are limited to exact adapter and operations paths. `boto3`
  is allowed only in configuration, EventBridge delivery, and administrative
  operations; `botocore` only in configuration; `slack_sdk` only in the Slack
  client/signature adapters and their direct retry test; and `anthropic` only in
  the Managed Agent client and the narrow webhook-signature verifier. Ports and
  neutral domain/model/wire modules cannot import provider implementations.
- `knowledge/persistence-schema-contract.json` lists every DSQL/PostgreSQL table
  and column owned by migrations. Any persistence change must update that
  contract deliberately. Contract approval does not override the separate ban on
  transcript, message-body, tool-output, or reasoning fields.
- Test disabling is an error by default. Playwright/Vitest skip and fixme forms,
  pytest skip/skipif/xfail forms, and unittest skip forms require an active
  `open` or `accepted` known limitation whose `test` path is that file. A
  `resolved` entry cannot keep a test disabled. The only permanent exceptions are
  the exact `RUN_POSTGRES_TESTS` and `RUN_INTEGRATION_TESTS` integration gates.

Adding an SDK exception or permanent skip exception requires an intentional
checker policy change and corresponding checker self-test. Do not suppress these
checks with a broader path or pattern.

## Deterministic semantic E2E

Local E2E runs the actual HTTP handlers, authorization, PostgreSQL repositories,
AG-UI bridge, A2A controller, Slack projection, and Next.js UI. Slack and
Anthropic are in-memory adapters; AWS is not emulated or contacted. No `.env`
file or service credentials are needed. Canonical transcripts remain in the
fake CMA provider and controller, outside PostgreSQL metadata.

### Run

```sh
npm run e2e
npm run e2e -- --grep E2E-005
npm run e2e:ui
npm run e2e:debug
npm run dev:e2e
```

The runner installs Chromium if absent, creates an isolated Compose project using
PostgreSQL 17, applies both production migrations, runs real SQL repository tests,
starts services, seeds identities, runs Playwright, and tears down on exit or
Ctrl-C. Ports 3000, 3001 and 54321 must be free; override the PostgreSQL port with
`E2E_POSTGRES_PORT`. It refuses to reuse existing services. UI/debug mode keeps
services alive until Playwright exits. `dev:e2e` enables asynchronous automatic
queue draining and stays running until interrupted.

Web: `http://127.0.0.1:3000`; access token: `e2e-access-token`.
Controls: `http://127.0.0.1:3001/_test/`. The seeded Slack identity `T001/U001`
shares the web principal. `T001/U002` belongs to another principal for ownership
rejection tests. Use the same hostname consistently for cookies.

### Write a scenario

Import the isolated fixture from `e2e/helpers/fixtures.ts`; every test starts
with reset state and uses real HTTP ingress. Web tests exercise the AG-UI bridge,
controller, and provider through browser actions and `a2a/...` controls. Use
`slack.ts` to sign realistic Slack payloads and interactions with the synthetic
E2E signing secret. `slackTurn` performs a complete controlled turn. RecordingSlack
records outgoing SDK payloads.

```ts
await control(request, 'a2a/script', {
  prompt: 'hello', response: 'Hello from Claude',
});
await login(page);
await send(page, 'hello');
await expect(page.locator('.assistant-message')).toContainText('Hello from Claude');
expect((await state(request)).database.agent_tasks).toHaveLength(1);
```

`a2a/script` seeds the controller-private fake CMA provider with existing Managed Agent event
wire data; the Web path still enters through AG-UI and A2A. Each turn needs
unique final event IDs. Canonical A2A Task history excludes stream-only
start/delta events. Use `a2a/automatic` to pause execution, `a2a/emit` to add
provider events, and `a2a/reconcile` to advance the controller deterministically.
Subscribers do not replay canonical history; test reconnect with a controlled
`/history` → `/resume` barrier rather than a timing sleep.

Approval and clarification scripts use `a2a/script` with tool events followed
by `session.status_idle` and `requires_action` event IDs. Reset releases
fault barriers, cancels subscriptions, waits for queue delivery, resets fake
counters, truncates metadata tables, and reseeds identities. Migration receipts
are preserved. Reset is restricted to loopback PostgreSQL named
`managed_agents_e2e`, with no connection query overrides.

### Faults and diagnostics

```ts
await control(request, 'fail/slack', {
  operation: 'chat.appendStream', error: 'rate_limited',
});
await control(request, 'fail/slack', {
  operation: 'chat.startStream', after: true, skip: 0,
  error: 'crash after the external side effect',
});
await control(request, 'fail/event', {
  detail_type: 'SlackWorkStarted', times: 1,
});
await control(request, 'faults', {
  point: 'before_agent_send', key: 'EvA', behavior: 'pause',
});
await control(request, 'faults/release', {point:'before_agent_send', key:'EvA'});
```

Slack operation failures work before or after the recorded external side effect;
`skip` selects a later occurrence. General fault points are `before_agent_send`,
`after_agent_send`, `before_ingress_complete`, and `before_projection_marked`.
They support `raise` or a bounded `pause`, optional request `key`, and `times`.
Faults default to a no-op in production. Post-send crash tests can expose remaining
exactly-once gaps; the harness does not invent upstream idempotency guarantees.

`events/drain` accepts `max_events` (default 100) and `workers` (default 1).
It returns pending and delivery history; hitting the bound leaves work queued.
Failures remain visible while independent work continues. Retry deliberately with
`events/retry` and the failed delivery `id`. Reinject the same signed Slack event
to exercise duplicate delivery. The concurrency scenario runs two actual handlers,
pauses both before sending, then releases B before A. It verifies one logical
input and controller FIFO Task execution despite reordered surface delivery.

Inspect `GET /_test/state`, `/events`, and
`/slack/messages`, `/slack/streams`, `/slack/reactions`. State includes fake calls
and the existing database metadata, never configuration or credentials.
Convenience `POST /_test/slack/event` and `/slack/interaction` sign payloads and
still invoke signature-verifying ingress. `a2a/emit` scripts a provider event
behind the local controller.
None of these routes is mounted in production/development; Lambda entrypoints
refuse E2E runtime configuration.

Failure artifacts are in `test-results/` and `playwright-report/`: screenshots,
traces, browser console, state snapshots, backend/frontend logs, migration/SQL
checks and PostgreSQL logs. Use `npx playwright show-report` or
`npx playwright show-trace <trace.zip>`. CI uploads these artifacts on failure.

When adding asynchronous behavior, include deterministic scenarios for the
relevant failure boundaries: duplicate delivery, failure before an external side
effect, failure after a recorded side effect, retry, concurrency, and ordering.
Use barriers and controlled event advancement rather than timing sleeps. Extend
the existing real application adapters and their recording/fake provider
boundaries instead of creating a second test architecture.

## Production-built web E2E

```sh
./scripts/e2e-production-web.sh
```

This runner builds the Next.js static export with the E2E API URL compiled in,
checks `apps/web/out/index.html`, and serves that exact artifact over loopback.
It uses the same local FastAPI runtime and disposable PostgreSQL composition as
semantic E2E, but runs only a small golden suite covering authentication and an
AG-UI turn, thread history hydration, and static routing/assets. It never uses
`next start`, `.env`, deployment commands, or live credentials. Failures are
captured under `test-results/` and `playwright-report-production/`.

## Live sandbox smoke tests

`npm run e2e:live` is separately opted in and is never run by PR CI or the local
runner. Export `RUN_LIVE_E2E=1`, `E2E_LIVE_BASE_URL`,
`E2E_LIVE_SLACK_CHANNEL_ID`, `E2E_LIVE_SLACK_TEAM_ID`,
`E2E_LIVE_SLACK_USER_ID`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, and
`WEB_ACCESS_TOKEN` from your sandbox configuration. Do not commit their values.
The configured human must already map to the web principal; the agent must have
approval-gated `web_fetch`, and Slack streaming/Agent View/approvals must be enabled.

Also export the Terraform `dsql_endpoint` as `DSQL_ENDPOINT` and concrete AWS
credentials for the live Task inspection helper. The live test creates a labelled
bot root and receipt messages in the sandbox, then injects timestamp-signed
synthetic human events against deployed ingress. Real DSQL, Anthropic, Slack and
browser responses cover A2A Task creation, duplicate delivery, bound-thread
continuation, Allow and denial reason, active Task stop, push projection, and Web
continuation of the same A2A thread. The Phase 6 suite also races conflicting
Web and Slack approval responses, checks one resolved controller request, and
checks that Slack Stop clears a Web pending approval after reload. It does not
validate Slack's delivery of human-originated
events; send one message from a mapped human account separately. Live sandbox
messages and sessions are retained for inspection. The suite requires explicit
environment configuration, never silently reads `.env`, and does not deploy
infrastructure.

## Local real Slack user smoke

For a focused tool-approval check, run
`npm run e2e:live -- slack-approval.spec.ts` with the same live configuration plus
`SLACK_USER_TOKEN`. This posts one genuine user mention, uses `SLACK_BOT_TOKEN`
to read the approval and final reply, independently checks the canonical tool
name and complete input, and submits Allow through the existing signed interaction
fixture. It verifies one completed Task and an answer in both visible Slack blocks
and Web history. It does not exercise a Slack client's button click. Notification
fallback text alone cannot satisfy the visible-answer assertion.

`npm run e2e:slack-user` is a separate, local-only smoke. It reads the existing
`SLACK_USER_TOKEN` and `SLACK_BOT_TOKEN` from `.env` (unless already exported),
posts a real user-authored mention, then posts an unmentioned continuation in the
same thread. Slack itself generates both inbound events. The helper polls real
Slack replies and calls `scripts/live_a2a_inspect.py` to check the binding and
two completed Slack-originated A2A Tasks. It neither calls `/slack/events` nor
constructs Slack signatures.

Configure `E2E_LIVE_SLACK_CHANNEL_ID` and optionally
`E2E_LIVE_SLACK_TEAM_ID`. The human returned by `whoami` must be mapped to the
application principal; the app must be installed in the channel, Socket Mode
must be disabled, and deployed `SLACK_BOUND_THREAD_REPLIES` must be enabled.
If the user token's posts carry Slack `bot_id` and `app_id` fields, configure
`SLACK_USER_BOT_ID` and `SLACK_USER_APP_ID` as an exact pair and redeploy ingress;
all other bot-originated messages remain ignored.
Export the deployed Terraform `dsql_endpoint` as `DSQL_ENDPOINT` and provide
concrete AWS credentials for the inspector. See `docs/slack-setup.md` for setup.

```sh
UV_CACHE_DIR=.uv-cache uv run --directory backend python ../scripts/slack_user.py whoami
npm run e2e:slack-user
```

The command reports PASS with the run ID, Slack thread, application thread,
context, Task IDs, and push receipt counts. FAIL names the first observable
boundary and retains the Slack thread for diagnosis. It does not deploy or run
from `./scripts/verify` or CI. Use the deterministic suite for semantic
regressions and this smoke for actual deployed Slack delivery.

## Live CMA configuration contract

After deployment and the real Slack smoke, set `CMA_ACCEPTANCE_CONTEXT_ID` to
the smoke's reported context. With concrete AWS credentials, the Terraform DSQL
endpoint, and local Anthropic credentials configured, run:

```sh
RUN_INTEGRATION_TESTS=1 UV_CACHE_DIR=.uv-cache uv run --directory backend \
  pytest tests/integration/test_real_services.py
```

The tests resolve provider ID/version/environment from the repository lock,
overriding stale `.env` selections. The exact-pin test retrieves the provider
session for that application context and checks all three fields against the
lock. Without `CMA_ACCEPTANCE_CONTEXT_ID`, it creates a labelled session through
the controller adapter; that proves wire compatibility but does not prove the
deployed application's configuration. Sessions remain available for inspection.
This credentialed test is separate from both ordinary verifier modes.
