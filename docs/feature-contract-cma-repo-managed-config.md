# Feature Contract — Repository-managed CMA configuration

## Goal

Manage the current agent/environment through ant apply and create every new CMA
session with the exact repository-selected agent ID and version.

## Non-goals

No A2A, AG-UI, Slack protocol, persistence, transcript, or webhook redesign.
No custom provider reconciliation, force/prune, CI deployment bot, or old-resource deletion.

## User-visible behavior

Developers edit CMA definitions, preview with `npm run cma:plan`, then deploy
normally. Human-entered provider IDs no longer select application configuration.

## Inputs

Tracked `cma/agents/application.md`, `cma/environments/application.yaml`, and
genuine `cma/claude-lock.json`; local deployment secrets; verified AWS target.

## Outputs / side effects

Explicit ant reconciliation may create agent versions, update the environment,
and update lock state. Deployment injects the resolved identity/version only into
controller components. `show` and ordinary verification are provider-free.

## Invariants

- FC-CMA-001: Source definitions preserve the existing provider configuration.
- FC-CMA-002: Deployment identity comes exclusively from validated lock state.
- FC-CMA-003: Both session creation paths send `{type: agent, id, version}`.
- FC-CMA-004: Fast/full verification requires no provider credentials or calls.
- FC-CMA-005: Remote drift fails apply; scripts never force or prune.
- FC-CMA-006: Deployment plan-only reads the current lock without CMA mutation.
- FC-CMA-007: A newly applied agent version is not selected by previously pinned
  Lambdas until their configuration changes. Environment updates are ID-based
  and may affect subsequent sessions before AWS rollout.
- FC-CMA-008: Secrets remain outside Git, including source definitions and lock.
- FC-CMA-009: Provider IDs/version remain private controller configuration.

## Failure semantics

- Before an external side effect: malformed/missing lock or deployment secrets
  stop normal deployment; explicit bootstrap alone permits an absent lock.
- After an external side effect: retain partial ant lock updates. AWS failure
  does not cause automatic provider rollback or lock deletion.
- Retry behavior: rerun reviewed ant apply/deployment against genuine lock state.
  A dirty-lock warning is emitted after successful or failed deployment.
- CLI dry-run success is not proof that its displayed plan is unblocked.

## Concurrency / idempotency semantics

- Duplicate delivery: application Task and message semantics are unchanged.
- Ordering: reconcile CMA and resolve its lock before AWS planning/application.
- Concurrent execution: operators serialize CMA applies/deployments; ant has no
  lockfile concurrency lock. No automation schedules concurrent reconciliation.

## Security / authorization constraints

Keep API keys in local input/secret stores. Validate origin and safe source paths.
Provider credential authorization and workspace matching remain ant's responsibility.
Public-readiness identity handling applies only to the exact two lock resource
ID fields; all secret checks and other private-identifier checks remain active.

## Persistence constraints

No migrations. DSQL continues to store metadata, opaque keys, and correlations;
Anthropic owns canonical transcripts and provider execution state.

## Compatibility constraints

Require ant >= 1.30.0 and lock format 1. Accept CLI numeric-string versions,
returning a positive integer. Runtime uses environment configuration, never Git
files. Missing provider pin is allowed for surface-only components but rejected
at provider construction. Synthetic E2E configuration supplies version 1.

Migration: **create replacement resources and cut over**. The existing resources
were inspected through the SDK. No export exists locally and browser export access
was unavailable. Preserve the current Haiku 4.5 model, system prompt, managed
toolset, web-fetch/search approval policies, empty integrations, and cloud
environment networking/packages/scope. Keep old resources untouched.

## Verification mapping

| Requirement | Test / verifier |
|---|---|
| FC-CMA-001/002 | Lock parser tests, source/provider comparison, command tests |
| FC-CMA-003 | Both ManagedAgentClient create argument tests; live session pin test |
| FC-CMA-004/006 | Fast/full verifiers; stubbed plan-only command test |
| FC-CMA-005 | Exact command flags and CLI failure tests; provider preview |
| FC-CMA-007 | Partial-failure lock retention tests; controller Terraform pin |
| FC-CMA-008 | Source/lock secret checks and public-readiness checker self-tests |
| FC-CMA-009 | Terraform least-privilege tests and architecture verifier |

## Live-contract requirements

After full deterministic verification, deploy through deploy.sh, verify provider
session response agent ID/version/environment against the lock, and run real
Slack-user smoke. Record only nonsecret identity/state evidence below. Session
listing and Terraform configuration alone do not prove version compatibility.

## Known limitations

Environment configuration is not version-pinned. Existing application limitations
in `knowledge/known-limitations.json` remain unchanged.

## Completion checklist

- [x] Each behavioral requirement maps to a test or verifier.
- [x] Failure, duplicate, concurrency, and authorization behavior is explicit.
- [x] `./scripts/verify --fast` passes.
- [x] `./scripts/verify --full` passes.
- [x] Live provider pin and real-Slack-user acceptance are recorded separately.
- [x] Final genuine lock state is included in the source change.

## Acceptance evidence

Accepted in the developer sandbox on **2026-09-30**:

- Official ant **1.36.0** created replacement resources and genuine format-1
  lock state. The final reviewed `npm run cma:plan` reported **2 unchanged**
  resources. Agent version **1** is selected by the included lock.
- Independent SDK reads matched the original agent's name, description, model,
  system prompt, tools/permissions, MCP servers, skills, and metadata, and the
  environment's name, configuration, scope, and metadata. Comparison omitted
  unset SDK fields consistently. Both original resources still exist untouched.
- Fast verification passed **330** Python unit/contract tests. Full verification
  passed those tests, **61** PostgreSQL integration tests, **3** production-built
  web tests, **26** semantic E2E tests, architecture/security rails, formatting,
  type checks, Lambda artifact checks, and Terraform validation. No live service
  calls were part of either verifier.
- `./scripts/deploy.sh` completed after verified AWS identity and concrete
  credentials. The three controller components received the lock's ID, version,
  and environment; the other seven Lambda components had no provider identity
  configuration.
- Configured `npm run e2e:slack-user` passed run **dc5dc73d1504**: one new
  application thread, two Tasks sharing its context, and one final response per
  turn, including the bound-thread continuation.
- All **3** opt-in real-service contracts passed. The exact-pin test selected
  the newly deployed Slack application's context and retrieved its provider
  session. The provider-reported agent ID and environment ID matched the lock,
  and the provider-reported agent version was exactly **1**. This proves the
  running application's selection rather than session listing alone.
- Agent-input, push, and EventBridge dead-letter queues were empty. The scheduler
  queue contained two existing dead letters timestamped **2026-09-28**, before
  this rollout; they were inspected without deletion or redrive.

An initial deployment exposed a packaging regression: the Lambda launcher tried
to load repository configuration. A separate local launcher and a standalone
packaged-launcher regression test fixed that boundary. Full verification and
deployment were repeated before the successful Slack and provider acceptance.
No provider resources were removed, and no changes were committed or pushed.
