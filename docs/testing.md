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
3. Deterministic semantic E2E uses Playwright, the real HTTP/application layers,
   PostgreSQL, `LocalEventBus`, `FakeManagedAgent`, and `RecordingSlack`. Put
   duplicates, retries, fault injection, and cross-surface behavior here.
4. Live contract/smoke tests cover only provider behavior that cannot be proven
   locally, such as installed Slack scopes, Anthropic wire compatibility, or
   DSQL-specific semantics. They are small, sandboxed, and explicitly opted in.

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
Managed Agent projection and Next.js UI. Slack and Anthropic are in-memory
adapters; AWS is not emulated or contacted. No `.env` file or service credentials
are needed. Canonical transcripts remain exclusively in the fake agent's memory.

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

Import the isolated fixture from `e2e/helpers/fixtures.ts`; every test starts with
reset state and uses real HTTP ingress. Use `slack.ts` to sign realistic payloads
and interactions with the synthetic E2E signing secret. `slackTurn` performs a
complete controlled streamed turn. No Slack HTTP emulator is needed: RecordingSlack
exercises the real Slack wrapper and records its outgoing SDK payloads.

```ts
await control(request, 'agent/script', {
  prompt: 'hello', automatic: false,
  events: answerEvents('Hello from Claude'),
});
await inject(request, mention('hello'));
const delivery = control(request, 'events/drain');
await ready(request); // real subscription registered, not a timing sleep
const session = (await state(request)).agent.sessions[0];
await control(request, 'agent/advance', {session_id: session.id, count: 2});
// Assert the intermediate text before releasing the final message/idle.
await control(request, 'agent/advance', {session_id: session.id});
await delivery;
```

Script events use the existing Managed Agent event wire format. Each turn must
have unique final event IDs. Canonical history excludes stream-only start/delta
events. Subscribers receive independent broadcasts and never advance a turn or
replay canonical history. `automatic: true` completes during input submission;
unmatched prompts produce `Echo: <prompt>` in manual development. Automatic
completion notifications are flushed after input handling, modelling a webhook
arriving after acceptance. Explicit signed webhook injection tests alternative
arrival timing. Manually advanced turns flush notifications immediately.

Approval scripts use `agent/script-tool-approval` with `tool_id`, `approved`,
`events`, and optional `automatic`. Interrupt scripts use `agent/script-interrupt`
with `events`; interrupts discard queued successful continuation. Reset releases
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
pauses both before sending, then releases B before A. It verifies unique input
and documents the lack of production per-session sequencing. FIFO local delivery
is not evidence of ordered production execution.

Inspect `GET /_test/state`, `/events`, `/agent/events?session_id=...`, and
`/slack/messages`, `/slack/streams`, `/slack/reactions`. State includes fake calls
and the existing database metadata, never configuration or credentials.
Convenience `POST /_test/slack/event` and `/slack/interaction` sign payloads and
still invoke signature-verifying ingress. `agent/emit` emits an individual event.
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
semantic E2E, but runs only a small golden suite covering authentication and a
turn, existing-session hydration, and static routing/assets. It never uses
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

The live test creates a labelled bot root and bot receipt messages in the chosen
sandbox, then injects timestamp-signed synthetic human events against deployed
ingress. Real DSQL, Anthropic, Slack and browser responses cover mention,
bound-thread continuation, Allow, stop, and shared-session continuity. This does
not validate Slack's delivery of human-originated events; the existing manual
acceptance runbook covers that separately. Live sandbox messages and sessions are
retained for inspection. The suite requires explicit environment configuration,
never silently reads `.env`, and does not deploy infrastructure.
