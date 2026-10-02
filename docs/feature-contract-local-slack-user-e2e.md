# Feature Contract — Local real Slack user smoke

## Goal

Prove that a mapped sandbox user can mention the deployed agent, receive a reply, and continue the same Slack thread through two completed A2A Tasks.

## Non-goals

CI gating, synthetic Slack ingress, browser automation, Slack login, production token configuration, and coverage of interactive controls.

## User-visible behavior

The local command posts two labelled human messages to a configured sandbox channel and leaves the thread available for inspection. It reports PASS with Slack and application identifiers or FAIL with the first observable broken boundary.

## Inputs

Local `.env` or inherited environment: `SLACK_USER_TOKEN`, `SLACK_BOT_TOKEN`, `E2E_LIVE_SLACK_CHANNEL_ID`, optional `E2E_LIVE_SLACK_TEAM_ID`, and `DSQL_ENDPOINT` with concrete AWS credentials. The deployed ingress receives only the configured `SLACK_USER_BOT_ID` and `SLACK_USER_APP_ID` identifiers. The deployed app must have the human identity mapped and bound-thread replies enabled.

## Outputs / side effects

Slack receives a real user mention and a real same-thread reply. The deployed app creates one application thread and two A2A Tasks. The helper reads Slack replies and the existing DSQL inspector's metadata-only JSON output.

## Invariants

- Only Slack's Web API receives the human token; the helper never calls application ingress or signs Slack events.
- `SLACK_USER_TOKEN` stays in local tooling and never enters production configuration or logs.
- A Slack message carrying `bot_id` is accepted only when its bot and app IDs match the configured pair; only the matched `bot_message` subtype is permitted.
- The helper neither writes DSQL data nor stores transcripts or provider events.
- Only messages attributable to the authenticated bot may satisfy response markers.

## Failure semantics

- Preflight failures stop before a Slack write.
- After a post, a timeout or read/inspection failure leaves the thread intact and reports its identifiers.
- Slack errors contain the API method and safe error code; network errors disclose no credential or request headers.

## Concurrency / idempotency semantics

- Each invocation uses a unique marker and a new Slack root, so concurrent runs do not share a binding.
- A response is counted by distinct Slack message timestamp; repeated reads do not count as duplicate replies.
- The helper never retries a failed `chat.postMessage` after an ambiguous response, avoiding an accidental duplicate human turn.

## Security / authorization constraints

The deployed ingress authenticates Slack's genuine event. Its narrow bot/app exception still requires an event `user` that resolves through the existing DSQL identity mapping and ownership checks. Without both configured IDs, all bot messages remain ignored.

## Persistence constraints

Only the deployed application writes metadata to DSQL. The helper emits no transcript persistence.

## Compatibility constraints

The production Slack adapter, manifest, and existing E2E suites remain unchanged. Terraform passes the non-secret bot/app identifiers only to the Slack ingress Lambda; the user token remains local.

## Verification mapping

| Requirement | Test / verifier |
|---|---|
| API failure and secret-safe errors | Helper unit tests |
| Message normalization, identity, markers, pagination, timeout | Helper unit tests |
| Exact bot/app ingress exception and subtype filtering | Normalizer and signed-ingress tests |
| Inspector validation and boundary classification | Helper unit tests |
| Architecture and regression checks | `./scripts/verify --fast` and `--full` |
| Genuine Slack delivery and deployed Task binding | Opt-in `npm run e2e:slack-user` |

## Live-contract requirements

The opt-in smoke must run against a deployed sandbox to establish real Slack event delivery and provider execution. Local verifiers cannot prove those connections.

## Known limitations

None added by this contract.
