# Optional Slack provisional title excerpts

`SLACK_PREVIEW_ENABLED=false` is the default and enabling it also requires
`PREVIEW_ENABLED=true`. Canonical Slack output and execution do not depend on
either switch. Native Slack-client title replacement remains unverified; keep
the Slack preview disabled when replacement or final cleanup is ambiguous.

## Lifecycle and ownership

The existing CMA observer publishes `/preview/{taskId}`. Web and the bounded
Node 24/Arm64 Slack worker subscribe independently. After the canonical projector
opens a card and releases its claim, it optionally sends an identifier-only hint
to an encrypted FIFO queue. The worker resolves the destination from trusted
Task/surface/projection metadata. Invalid, duplicate and stale hints are discarded.

The worker changes only the existing task title to a sanitized `Draft: …` excerpt
of at most 140 characters. It limits writes to one changed title per two seconds,
forty calls and 120 seconds. Draft text remains in memory and never enters DSQL,
canonical Markdown, queue messages, receipts or logs. Each write requires an
eight-second projection claim; database queries time out after 1.5 seconds and
HTTP after three. Lost claims, rate limits and ambiguous acknowledgments stop
optional work without retrying an uncertain write.

Canonical projection replaces the title at completion, review, cancellation,
failure or rejection and prevents subsequent preview claims. A card closed for
human input is not reopened on continuation; the final response uses the normal
reply path. No speculative Markdown or extra draft message is used as fallback.

## Infrastructure and verification

The worker has a 130-second timeout, 256 MiB memory and concurrency four. IAM
permits consuming its queue, DSQL application access, reading the Slack bot secret
and AppSync connect/subscribe. It has no Anthropic credential, AppSync publish
permission or controller invocation. The application SQL role is shared; code
boundaries are not per-table database grants. Both preview flags must be enabled
for the consumer mapping. There is no new schema, NAT or Slack OAuth scope.

Local tests exercise protocol offsets, duplication, throttling, deadlines,
claim contention, terminal fencing, transport outages, human review and one
observer feeding Web plus Slack. The recorder models replacement by task ID;
it cannot establish real Slack-client rendering.

The originating sandbox's 2026-10-31 rollout was verified with both switches and
mappings disabled. Artifact hashes, Web health, DSQL role association, metadata
stability and allowed/denied IAM simulations were checked. No live Slack write
or worker subscription was performed during that rollout. These historical checks
do not establish enabled-preview acceptance for another deployment.

## Enabling and rollback

After explicit sandbox-write authorization, verify real producer delivery and
IAM subscription success before enabling the Slack consumer separately. Use
API-only Slack delivery with Web subscribed to check one CMA reader, title updates,
one canonical answer, disabled/outage behavior and cleanup. Do not ask a user to
operate Slack or treat API evidence as proof of native client rendering.

Rollback disables the Slack flag/mapping while preserving canonical execution and
the independent Web producer setting. Drafts can be missed entirely; Slack's
ten-minute automatic finalization may prevent late title cleanup. Network ambiguity
precludes exactly-once external writes. Worker time, up to forty Slack calls and
additional AppSync/DSQL/NAT usage add cost. See [limitations](known-limitations.md).
