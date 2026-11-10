# Knowledge Update Log

## 2026-11-10

- **Persistent native policy migration**: Updated [policy operations](areas/openappa-cma-policy-state.md) for protocol 6, migrations 013–015, private SQLite/S3 checkpoints, stored native review offers and admitted custom results. Preserved historical snapshot-retention guidance and linked the public upgrade procedure. The note is stable within its documented mediation and recovery limits; historical sandbox acceptance remains separate from this public migration's local verification.

## 2026-10-31

- **Phase 4 Slack manifest applied**: With the supplied local configuration token, exported/validated/applied the bot cleanup and verified the result: seven bot scopes, five event types, no unfurls, Socket Mode disabled. Preserved existing org readiness and user OAuth/MCP settings. Genuine mention/reply, automatic tool, clarification and Stop verification passed after the update. The installed bot token retains five removed scopes; [Slack setup](../docs/slack-setup.md) distinguishes manifest configuration from credential revocation/reinstallation.

- **Phase 4 sandbox acceptance**: Deployed with additive migration 012 under scheduler quiescence, preserving 213 prior Tasks and 76 prior requests. Fixed DSQL column portability, scoped scheduler review-object publication and Slack argument rewriting. Genuine delivery and native Web Allow/signed Slack Deny/Stop passed against real CMA; default policy/artifact restored with hash verification. Updated [policy operations](areas/openappa-cma-policy-state.md) and [live evidence](../docs/testing.md#optional-live-evidence), retaining the unavailable manifest-configuration token and unverified native Slack-client interactions as explicit limits.

- **Phase 4 simplification**: Consolidated Slack/CMA setup and acceptance guidance into current docs; removed redundant knowledge mirrors, machine registries and historical feature contracts. Kept DSQL IAM and policy rollout/retention notes. Added native stateless HITL semantics and explicit live-evidence limitations. Historical acceptance below remains historical, not verification of the new review path.

- **Stateless OpenAPPA authorization**: Replaced native trajectories and snapshots with fresh per-call evaluation and a fenced request-row decision. Updated [operations](areas/openappa-cma-policy-state.md) for current-policy behavior, migration 011, confirmation recovery and retained historical S3 data. Historical live results remain separate from Phase 3 local verification.

- **Phase 3 sandbox cutover**: Deployed after explicit authorization with the scheduler quiesced through migration 011. Preserved 73 prior approval records, 206 Task identities/states and all 33 historical snapshot objects/security settings. Genuine API delivery passed mention/reply, automatic allow, linked-web clarification, Stop, isolated deny and restored-baseline allow. All Lambda hashes matched; no new dead letters were added. [current limitations and evidence](../docs/known-limitations.md) records canonical evidence, recovered contention retries and unsupported native Slack interactions separately from historical stateful results.

## 2026-10-05

- **Live latency root causes**: [current limitations and evidence](../docs/known-limitations.md) correlates genuine run `7c2aa2af23b147c69ea42886b4c6c734` with executor spans, scheduler claims, native status timestamps, and Slack card metadata. Two canonical reads waited 236 seconds behind six native stream readers, establishing KL-007; its enabled adapter reproduction uses bounded local fixtures. Two terminal webhooks raced scheduler release and waited for 30-second recovery. Normal projection closure with WORKING state and nine error cards expose a distinct completion-acceptance gap. Diagnostic probes are deployed; behavior fixes remain unimplemented.

## 2026-10-04

- **Latency diagnosis and cancellation coverage**: Added content-free executor queue/execution and stream lifecycle timings, described in [current limitations and evidence](../docs/known-limitations.md). Recorded KL-006 for an intermittent Web Stop panel/history-refresh race; E2E-010C remains enabled, with no retries or skips. Genuine provider probes confirmed that cancelled synchronous CMA stream readers keep running. Live causal attribution remains separate from local mechanism reproduction.

- **CMA clarification and live acceptance**: Added custom `ask_user` to the repository agent and deployed genuine exact version 2. Fixed stopped-stream pointer reuse and idempotent Slack stop recovery exposed by real clarification. Final fast/full verification passed (414 unit/contract tests, 85 PostgreSQL tests, 30 semantic E2E cases). Genuine API-only run `b47828f14af44d03a8c0560cb217f70b` passed mention/reply, automatic allow, clarification through the linked web continuation API, and explicit Stop. Recorded CMA's direct termination of a custom call on interrupt; native Slack answer controls remain unsupported.

- **Embedded OpenAPPA policy state**: Added scheduler-owned native snapshots, durable publication and receipt replay rules, policy pinning, retryable holds, and orphan collection. Fast/full verification and genuine Slack/CMA allow/deny acceptance passed; baseline policy was restored and checked in a fresh context. Recorded DSQL migration portability, Lambda package-size guards, and KL-005 for absent result mediation and tools bypassing approvals. The initial version-1 probe lacked custom `ask_user`; the follow-up acceptance below closes that provider configuration gap.

## 2026-09-30

- **Repository-managed CMA configuration**: Documented source definitions and
  genuine lock ownership, exact session-version selection, explicit provider
  previews, drift refusal, serialized apply, partial-failure lock retention, and
  the environment's ID-based rollout/rollback caveat. The configuration feature
  contract records passing fast/full verification, sandbox deployment, real
  Slack continuation, and provider-reported exact version 1 for the newly created
  application session. Original provider resources remain intact; two older
  scheduler dead letters were left untouched.

## 2026-09-28

- **Final A2A provider boundary**: Updated Managed Agent operations and the application acceptance runbook to keep provider sessions, event reads, webhook wakeups, and ambiguous-send resolution behind the controller. Application threads and A2A Tasks are now the Web and Slack identities; the old direct-session projection procedure is retired.
- **Phase 6 acceptance**: Recorded the dev migration, live Web and mapped-human Slack checks, cross-surface approval and cancellation, private A2A denial, deployed IAM, and empty dead letter queues in the final feature contract and runbook.

## 2026-09-18

- **Portable deployment and public-readiness rails**: Documented deployment-scoped
  Terraform state rendering, Secrets Manager references for web credentials,
  generated Slack manifests, configurable AWS account assertions, and the
  current-tree guard that keeps generated material and deployment identifiers out
  of distributable files.

## 2026-09-07

- **Persistence schema contract**: Added the machine-checked inventory of migration-owned metadata tables and columns, alongside closed SDK boundaries and test-disable rails.

## 2026-09-06

- **Autonomous development rails**: Added the machine-readable known-limitations registry and linked the current per-session sequencing gap to its deterministic concurrency test.

## 2026-09-05

- **Slack native agent projection**: Added durable operating rules for immediate processing status, exact-message receipt reactions, source permalinks, chunk-mode task streams, pending approval suspension, and canonical-event reconciliation. Captured the two important race lessons: Managed Agent streams may miss already-completed turns, and webhook recovery must defer while a live stream lease is valid.
- **Slack native working-state acceptance**: Added the sandbox verification procedure and recorded successful channel mention, bound-thread reply, DM/Agent View, tool Allow, native Stop, permalink-failure, chunk-recovery, live-service, deployment-health, and DLQ checks without retaining credentials or message content.

## 2026-09-02

- **Managed Agent application operations**: Initialized the repository OKF bundle with reusable Slack Events API, Anthropic Managed Agents, and Aurora DSQL IAM runbooks plus an end-to-end deployment and verification procedure. Grounded service behavior in official Slack, Anthropic, and AWS documentation and retained the observed Socket Mode, region-resolution, event-derived identity, webhook, and projection failure modes without recording secrets.
