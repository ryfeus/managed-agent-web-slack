# TypeScript Web gateway rollout

The Node 24 Web Function serves authenticated HTTP and AG-UI SSE through
CloudFront. The private controller remains the only authority for execution and
canonical reconciliation. A separate bounded observer can publish disposable
assistant-text previews through IAM-only AppSync Events. Previews default to off.

Existing installations must follow [the public upgrade checkpoints](upgrading.md).
They retain the Python Web origin during the additive stage, retain its resources
through CloudFront acceptance, and retire them only after target-environment
parity. Fresh installations can deploy the final tree after configuring their own
CMA workspace, secrets, backend and database roles.

## Trust and transport

CloudFront `/api/*` is uncached, forwards viewer cookies, disables compression,
and uses Lambda OAC with regional SigV4 and `always-amz-auth`. Only the configured
distribution receives endpoint invocation permission. Slack and Anthropic webhook
paths retain API Gateway origins. Unsigned direct Web endpoint requests must fail.

The Web role reads only Web authentication secrets, connects to DSQL and subscribes
to AppSync. The observer can read the Anthropic credential and publish to the
preview namespace. Browser code receives neither AppSync credentials nor private
provider identities. Canonical history replaces provisional text before completion.
Bodyless Stop remains authenticated and ownership/origin checked; disconnect never
cancels a Task. History and resume derive from the same fetched Task snapshots.

Artifacts use a private, encrypted, versioned S3 bucket. SAM manages Web Function
resources missing from the pinned Terraform provider. Replaced revisions are
retained; `web_function_rollback_revision` can select an existing tested revision
through a reviewed plan. Inspect actual endpoint revision weights when using it.

## Validation evidence

The originating sandbox's 2026-10-31 acceptance verified cookie authentication,
private A2A/DSQL connectivity, nine Web/Slack continuity steps, and three native
human-review cases against the TypeScript gateway. Two authenticated viewers
received real CMA deltas from one observer/provider send and reconciled to the
same canonical history. Two other turns completed correctly without previews.
Signed Slack interactions prove backend handling, not native client clicks.

The original policy artifact was restored after review testing, previews were
disabled, retired API routes were checked, artifact hashes were matched and a
retained Web revision rollback was exercised. These are historical results from
one configured environment, not acceptance of a new installation or this public
branch. New deployments must perform their own authorized acceptance.

Local tests cover authorization, bodyless actions, consistent history/resume,
AppSync framing, optional CMA delta indexes, stream deadlines, canonical fallback
and observer isolation. Offline SAM/schema validation and mocked Terraform tests
cover IAM, networking, routing, artifacts and revision rollback. Local fakes do
not establish live service contracts. See [testing](testing.md).

## Operational constraints

- The deployed provider may report an inconsistent CloudFront origin-set plan
  when Web revision outputs become unknown. Inspect actual state after a failed
  apply; review the Web revision update separately, then generate a fresh full
  plan. Never blindly retry a stale saved plan.
- Retirement removes old Web routes, permissions, Lambdas, roles, log groups and
  the old bridge artifact object. Deletion guards intentionally reject this in
  the normal wrapper. Review exact resource addresses and preserve DSQL, buckets,
  queues, APIs, stages and networking. A combined removal can produce a dependency
  cycle; use separately reviewed route and Lambda retirement plans if required.
  Recheck the deployed API stage export after snapshot replacement.
- The gateway caps stream lifetime at 840 seconds and disconnects blocked writes
  after five seconds. Clients recover from canonical history/resume. Previews may
  miss the entire response and stop after 120 seconds; they never finish a Task.
- Both connector AZs share one NAT; this is not multi-AZ production HA. Web,
  observer, AppSync and NAT usage add cost. Known clarification UI and dependency
  findings remain documented in [limitations](known-limitations.md).
