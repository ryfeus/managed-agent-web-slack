# Feature Contract — Private A2A cutover

## Goal

Make the Phase 4 Slack control plane reachable inside the deployed dev account and prove a real Slack message completes one A2A Task.

## Non-goals

Migrating Web to A2A, exposing the controller publicly, deleting legacy tables, and adding Slack clarification-response controls.

## User-visible behavior

Existing Slack features continue through private A2A endpoints. Web remains on direct CMA. A failed subscription recovers from current Task state without a duplicate response.

## Inputs

Signed Slack events, private A2A HTTP+JSON requests, and private push envelopes.

## Outputs / side effects

The controller admits Tasks, the sink publishes `A2ATaskUpdated`, and the Slack projector updates one task card per binding.

## Invariants

- Controller and sink URLs are absent from public CloudFront routes.
- The private API accepts traffic only through its designated VPC endpoint.
- DSQL holds identity, routing, claims, and projection metadata, not transcript content.
- Slack never receives controller-private CMA identifiers.

## Failure semantics

Failed A2A admission retries the stable Slack message ID. Idle stream closure falls back to push and `GetTask`. Push failures retry from the durable queue. Neither receipt-status nor source-link failures prevent authorized execution.

## Concurrency / idempotency semantics

Controller admission deduplicates message IDs. Projection item claims deduplicate A2A Messages and human-input requests across stream and push. Only the earliest active Task is cancelled on Stop.

## Security / authorization constraints

The private API has a `SourceVpce` resource policy. Caller Lambdas use a private subnet and an endpoint-restricted security group. Slack ingress remains timestamp-HMAC verified and DSQL ownership checks remain in force.

## Persistence constraints

No new transcript fields are added to DSQL. Pending denial reasons remain in the controller object store.

## Compatibility constraints

Public Slack and Web URLs remain stable. The existing dev remote Terraform state is reused. Web continues using legacy CMA until Phase 5.

## Verification mapping

| Requirement | Check |
|---|---|
| Local behavior and packaging | `./scripts/verify --full` |
| Private routing and no public access | Terraform plan, API configuration, outside-VPC request |
| A2A card, stream, push | In-VPC smoke and live Slack Task |
| Real Slack ingress | Human sandbox message with unique marker |
| Existing Web | Live direct-CMA browser turn |
| Delivery health | Lambda logs and EventBridge/SQS DLQ counts |

## Live-contract requirements

The private API, Lambda Web Adapter stream, real Anthropic provider, installed Slack token, and actual Slack event delivery must be observed in AWS before acceptance.
The signed-ingress suite proves normal in-VPC card resolution, Task subscription,
push delivery, and Web direct-CMA execution. A guarded live fixture forced a
subscription failure after starting a Slack task card; `A2ATaskUpdated` then
recovered the completed Task through `GetTask` and finished that same card with
one response. The Postgres-backed local tracer also proves this recovery behavior
deterministically.
The mapped human account sent marker `HUMAN_A2A_20260927_P4B7` in sandbox channel
`C0BURFLHY3E`. Its Slack event `Ev0C4MURC28M` produced one application thread,
one completed Task `26941694-6e5a-42cc-a872-2178cd9e21a8`, two push receipts,
one completed projection, and no legacy session row. Slack showed `:eyes:` on the
human source and one bot response containing the marker. All four DLQs remained empty.

## Known limitations

Private API streaming has a five-minute idle timeout. Task-based push recovery remains required for longer idle turns.

## Completion checklist

- [x] Full local verifier and reviewed Terraform plan pass.
- [x] Private controller and sink pass through in-VPC Slack Task traffic.
- [x] Live signed-ingress and direct-CMA Web checks pass.
- [x] Forced live stream fallback recovers one existing Slack task card.
- [x] Human Slack delivery and its single completed response are traced.
- [x] DLQs are empty and no duplicate Slack answer is observed in signed ingress.
