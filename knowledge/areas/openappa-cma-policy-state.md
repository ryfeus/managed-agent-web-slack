---
type: Runbook
title: "OpenAPPA CMA Policy State"
description: "Operate stateless embedded tool authorization, fenced decisions, and uncertain confirmation recovery."
tags: [openappa, cma, tool-policy]
status: draft
sources:
  - id: implementation-contract
    resource: "../../docs/architecture.md"
    title: "Current native OpenAPPA architecture"
  - id: upstream-embedding
    resource: "https://github.com/archestra-ai/OpenAPPA/blob/c7c1e1ef36aa07604b421c48be266d7397557656/appa-runtime/src/api/mod.rs"
    title: "Pinned upstream embedding API"
  - id: live-review-acceptance
    resource: "../../e2e/live/human-review.spec.ts"
    title: "Native review acceptance against deployed Web, Slack APIs and CMA"
---

# OpenAPPA CMA Policy State

The scheduler evaluates each intercepted call in fresh native memory. CMA owns execution and canonical tool arguments. Fenced request-row decisions and confirmation-attempt markers remain durable in DSQL; native event logs and tool bodies do not. See [architecture](../../docs/architecture.md) for the single-authority HITL remedy and [known limitations](../../docs/known-limitations.md) for its enforcement boundary.

Migration 012 adds review fingerprint, authority, temporary-object key and human ruling to existing requests. Review text uses the same private temporary-input store and orphan sweep. Quiesce scheduler consumers through schema/code rollout. Old automatic-only schedulers must not run while human reviews are pending; settle/cancel under new code before rollback. Never discard attempted confirmations or resend them without canonical evidence. Stop is independent of policy availability.

The authorized 2026-10-31 Phase 4 rollout preserved 213 prior Tasks and 76 prior requests. DSQL requires the added ruling column without an inline constraint; the repository validates allow/deny. The scheduler needs narrowly scoped `s3:PutObject` on the existing pending prefix to publish review descriptions. Live acceptance verified Web reload/Allow, signed Slack Deny/duplicates, and pending-review Stop against canonical CMA evidence, then restored the default policy/artifact and verified its hash. Slack tool arguments require verbatim text to prevent URL rewriting. See [testing](../../docs/testing.md#optional-live-evidence) for evidence and remaining manifest/native-client limits.

## Historical snapshot transition

Migration 011 verifies legacy publication before removing snapshot columns. It preserves resolved/attempted rows and valid decisions, resetting only unattempted unproven decisions except cancellation-deny. Quiesce all consumers and finish active workers before applying the transition; do not create stateless decisions during normalization. Partial DSQL retries use the first dropped column as the checkpoint. Old snapshot-dependent code cannot run after this migration.

Terraform's existing removed blocks forget the historical snapshot bucket/privacy resources with `destroy = false`. Objects/security configuration remain; the obsolete scheduler IAM grant was separately reviewed and retired. Do not empty, import or destroy those retained resources merely to simplify this repository. Physical cleanup requires separate authorization.

The authorized 2026-10-31 Phase 3 cutover preserved 73 approval records, 206 Task identities/states and 33 historical snapshot objects/security settings. Genuine delivery verified automatic allow/deny, clarification and Stop; 18 pre-existing push dead letters remained untouched. This evidence predates the Phase 4 human-review implementation and does not prove live HITL acceptance. Native Slack-client interactions remain unverified.

## Recovery

A committed final decision survives crashes and policy changes. An uncertain confirmation stays held unless canonical provider evidence settles it; never blindly confirm again. For uncertain input sends use the [operator procedure](../../docs/operations.md#recovery). Runtime/policy/publication errors fail closed. An unanswered review invalidated by an outage or policy change is retired; fresh review gets a new application request ID. No approval can reuse an old offer ID from a prior native process.
